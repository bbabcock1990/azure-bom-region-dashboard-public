"""Non-destructive **deep** deployability check via ARM ``validate``.

Some services expose no authoritative per-subscription capability/SKU API for
their zone-redundant tiers (App Service, Redis, Service Bus, Event Hubs). For
those, :mod:`zonal_capability` can only say ``not_verifiable`` and fall back to
the region-level AZ signal. This module closes that gap **without deploying
anything**: it issues an ARM *template validation* request
(``.../deployments/{name}/validate``) which asks Resource Manager to run all the
pre-flight checks a real deployment would — quota, SKU availability, region
offer restrictions — but **creates no resources and incurs no cost**.

Because ``validate`` is resource-group scoped, the caller must supply a resource
group the subscription already owns (configured in Settings). We never create
one implicitly. If no RG is supplied we return ``verdict='no_resource_group'``
so the UI can prompt the user rather than guessing.

Verdicts returned (mirroring :mod:`zonal_capability`):

  * ``available``          — validation passed → the ZR tier pre-flights clean.
  * ``blocked``            — validation failed on a *capacity/quota/region/SKU*
                             signal; ``block_type`` + ``ticket`` say which remedy.
  * ``advisory``           — cannot be proven by validate (e.g. Cosmos zonal
                             capacity only surfaces at real create); we advise
                             + point at the region-access request path.
  * ``unverifiable``       — ARM returned an error unrelated to capacity (our
                             probe template or an auth/permission issue); we do
                             not pretend either way.
  * ``no_resource_group``  — no validation RG configured; UI should prompt.
  * ``not_verifiable``     — the service/tier has no validate template here.

Every ``blocked`` result carries a ``block_type`` and a ``ticket`` hint so the
support-ticket automation can be wired to the right flow:

  * ``block_type='quota'``            → Microsoft.Quota / support quota ticket
                                        (aka.ms/antquotahelp).
  * ``block_type='sku_restriction'``  → zonal/SKU whitelisting request
                                        (aka.ms/azureskunotavailable).
  * ``block_type='region_restriction'`` → region/offer access request.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .bom_services import ARM_BASE, DEFAULT_TIMEOUT_S, _normalize_region, _strip_bearer

log = logging.getLogger(__name__)

VALIDATE_API_VERSION = "2021-04-01"

# ── Catalog (service, tier) → minimal zone-redundant ARM resource ───────────
#
# Each entry is a callable ``(region, name) -> resource dict``. We enable the
# zone-redundant knob for the tier so validation exercises the *AZ* capacity
# path (that is the whole point — a non-ZR SKU would pre-flight fine while the
# ZR one is quota-blocked). Names are random + unique so validate never trips on
# a name collision.

def _serverfarm(sku_name: str, sku_tier: str):
    def build(region: str, name: str) -> Dict[str, Any]:
        return {
            "type": "Microsoft.Web/serverfarms",
            "apiVersion": "2023-12-01",
            "name": name,
            "location": region,
            # capacity >= 3 so the plan must place instances across zones,
            # which is what makes the "Total AZ VMs" quota check fire.
            "sku": {"name": sku_name, "tier": sku_tier, "capacity": 3},
            "properties": {"zoneRedundant": True, "reserved": True},
        }
    return build


def _redis(sku_name: str):
    def build(region: str, name: str) -> Dict[str, Any]:
        return {
            "type": "Microsoft.Cache/Redis",
            "apiVersion": "2023-08-01",
            "name": name,
            "location": region,
            "zones": ["1", "2", "3"],
            "properties": {"sku": {"name": sku_name, "family": "P", "capacity": 1}},
        }
    return build


def _servicebus(region: str, name: str) -> Dict[str, Any]:
    return {
        "type": "Microsoft.ServiceBus/namespaces",
        "apiVersion": "2022-10-01-preview",
        "name": name,
        "location": region,
        "sku": {"name": "Premium", "tier": "Premium", "capacity": 1},
        "properties": {"zoneRedundant": True},
    }


def _eventhub(region: str, name: str) -> Dict[str, Any]:
    return {
        "type": "Microsoft.EventHub/namespaces",
        "apiVersion": "2024-01-01",
        "name": name,
        "location": region,
        "sku": {"name": "Premium", "tier": "Premium", "capacity": 1},
        "properties": {"zoneRedundant": True},
    }


# service name -> {tier_id -> resource-builder}
_VALIDATE_SERVICES: Dict[str, Dict[str, Any]] = {
    "Azure App Service": {
        "premium_v2": _serverfarm("P1v2", "PremiumV2"),
        "premium_v3": _serverfarm("P1v3", "PremiumV3"),
        "isolated_v2": _serverfarm("I1v2", "IsolatedV2"),
    },
    "Azure Cache for Redis": {
        "premium": _redis("Premium"),
    },
    "Azure Service Bus": {
        "premium": _servicebus,
    },
    "Azure Event Hubs": {
        "premium": _eventhub,
        "dedicated": _eventhub,
    },
}

# Services whose zone-redundant guarantee cannot be proven by validate — the
# capacity is only enforced at real create time. We surface an honest advisory
# instead of a false "verified".
_ADVISORY_SERVICES: Dict[str, str] = {
    "Azure Cosmos DB": (
        "Zone redundancy for Cosmos DB is enabled per-region at account creation and its "
        "availability-zone capacity is not guaranteed until deploy time. This cannot be "
        "confirmed without creating an account. If a zone-redundant Cosmos account fails to "
        "provision here, request region access via aka.ms/cosmosdbquota."
    ),
}

# ── Error-signature → block classification ──────────────────────────────────
#
# Substrings (lower-cased) Azure uses in validate failures, mapped to a block
# type + the remedy/ticket flow. Ordered most-specific first.
_QUOTA_MARKERS = (
    "overquota", "over quota", "quota", "current limit", "exceeds", "insufficient",
)
_SKU_MARKERS = (
    "skunotavailable", "notavailableforsubscription", "not available for subscription",
    "capacity restrictions", "not available in zone",
)
_REGION_MARKERS = (
    "not available in this region", "not accepting", "provisioning is restricted",
    "regiondoesnotallowprovisioning", "locationnotavailable", "does not have access",
    "not allow provisioning", "the location is restricted",
)


def _classify(code: str, message: str) -> Optional[Tuple[str, str, str]]:
    """Map an ARM error (code+message) to ``(block_type, ticket, help_url)`` if
    it is a capacity/quota/region blocker, else ``None`` (unrelated error)."""
    blob = f"{code} {message}".lower()
    # Region/offer restriction is the most specific — check first.
    if any(m in blob for m in _REGION_MARKERS):
        return ("region_restriction", "region_access",
                "https://aka.ms/regionaccess")
    if any(m in blob for m in _SKU_MARKERS):
        return ("sku_restriction", "zonal",
                "https://aka.ms/azureskunotavailable")
    if any(m in blob for m in _QUOTA_MARKERS):
        return ("quota", "quota",
                "https://aka.ms/antquotahelp")
    return None


def _flatten_error(err: Any) -> List[Tuple[str, str]]:
    """Collect ``(code, message)`` pairs from a nested ARM error object."""
    out: List[Tuple[str, str]] = []
    if not isinstance(err, dict):
        return out
    code = str(err.get("code") or "")
    msg = str(err.get("message") or "")
    if code or msg:
        out.append((code, msg))
    for d in err.get("details") or []:
        out.extend(_flatten_error(d))
    return out


def service_validate_kind(name: str) -> Optional[str]:
    """Return ``'validate'`` if the service has a validate template, ``'advisory'``
    if it is an advisory-only service, else ``None``."""
    if name in _VALIDATE_SERVICES:
        return "validate"
    if name in _ADVISORY_SERVICES:
        return "advisory"
    return None


def _random_name(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def validate_resource(
    *,
    resource: Dict[str, Any],
    region: str,
    resource_group: str,
    subscription_id: str,
    arm_token: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Dict[str, Any]:
    """Issue a single ARM ``validate`` for ``resource`` and classify the result.

    Returns a dict with at least ``verdict`` and ``message``; ``blocked`` results
    also carry ``block_type``, ``ticket`` and ``help_url``. Never creates
    anything. Degrades to ``unverifiable`` on auth/permission/transport errors."""
    token = _strip_bearer(arm_token)
    dep_name = _random_name("bomcheck")
    url = (f"{ARM_BASE}/subscriptions/{subscription_id}/resourcegroups/"
           f"{resource_group}/providers/Microsoft.Resources/deployments/"
           f"{dep_name}/validate")
    body = {
        "properties": {
            "mode": "Incremental",
            "template": {
                "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
                "contentVersion": "1.0.0.0",
                "resources": [resource],
            },
            "parameters": {},
        }
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=timeout_s, http2=False) as client:
            r = client.post(url, params={"api-version": VALIDATE_API_VERSION},
                            json=body, headers=headers)
    except Exception as ex:  # pragma: no cover - defensive
        log.warning("validate request failed: %r", ex)
        return {"verdict": "unverifiable",
                "message": "Deep validation could not reach Azure Resource Manager."}

    if r.status_code in (401, 403):
        return {"verdict": "unverifiable",
                "message": ("Deep validation was denied (insufficient permission on the "
                            "validation resource group, or the subscription blocks validate).")}
    if r.status_code == 404:
        return {"verdict": "no_resource_group",
                "message": ("The configured validation resource group was not found in this "
                            "subscription. Set an existing resource group under Settings.")}

    # 200 (with or without an inner error), or 4xx carrying an error body.
    data: Dict[str, Any] = {}
    try:
        data = r.json() or {}
    except Exception:
        data = {}

    err = data.get("error")
    if r.status_code < 400 and not err:
        return {"verdict": "available",
                "message": "Pre-flight validation passed — the zone-redundant tier is deployable here."}

    pairs = _flatten_error(err) if err else [("", r.text or "")]
    for code, msg in pairs:
        hit = _classify(code, msg)
        if hit:
            block_type, ticket, help_url = hit
            short = msg.strip().split("\n")[0][:240] or code
            return {"verdict": "blocked", "block_type": block_type, "ticket": ticket,
                    "help_url": help_url,
                    "message": f"Blocked by Azure pre-flight ({block_type}): {short}"}

    # An error we don't recognise as a capacity blocker — most likely a benign
    # probe-template quirk. Be honest rather than claim availability.
    first = pairs[0] if pairs else ("", "")
    detail = (first[1] or first[0] or "unknown").strip().split("\n")[0][:240]
    log.info("validate: unclassified error code=%r msg=%r", first[0], detail)
    return {"verdict": "unverifiable",
            "message": f"Pre-flight returned an unrecognized result: {detail}"}


def evaluate_deep(
    *,
    services: List[Dict[str, Any]],
    region: str,
    resource_group: str,
    subscription_id: str,
    arm_token: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> List[Dict[str, Any]]:
    """Run the deep (validate-based) check for each ``{name, tier}`` selection
    that has a validate template, plus advisory verdicts for advisory-only
    services. Selections without either are returned ``not_verifiable``."""
    region_norm = _normalize_region(region)
    results: List[Dict[str, Any]] = []
    for svc in services or []:
        name = str((svc or {}).get("name") or "")
        tier = str((svc or {}).get("tier") or "")
        kind = service_validate_kind(name)
        base = {"name": name, "tier": tier, "checkable": bool(kind),
                "source": "ARM pre-flight validation (no resources created)"}

        if kind == "advisory":
            results.append({**base, "verdict": "advisory", "block_type": "region_restriction",
                            "ticket": "region_access", "help_url": "https://aka.ms/cosmosdbquota",
                            "message": _ADVISORY_SERVICES[name]})
            continue
        if kind != "validate":
            results.append({**base, "verdict": "not_verifiable",
                            "message": "No deep-validation template for this service."})
            continue

        builder = _VALIDATE_SERVICES[name].get(tier)
        if not builder:
            results.append({**base, "verdict": "not_verifiable",
                            "message": "Selected tier is not zone-redundant."})
            continue

        if not resource_group:
            results.append({**base, "verdict": "no_resource_group",
                            "message": ("Set a validation resource group under Settings to run the "
                                        "deep (non-destructive) deployability check.")})
            continue

        resource = builder(region_norm, _random_name(name.split()[-1][:8].lower() or "bom"))
        verdict = validate_resource(
            resource=resource, region=region_norm, resource_group=resource_group,
            subscription_id=subscription_id, arm_token=arm_token, timeout_s=timeout_s)
        results.append({**base, **verdict})
    return results

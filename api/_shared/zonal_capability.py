"""Live, per-subscription verification that a zone-redundant service **tier**
can actually be deployed in a given region — as opposed to merely asserting
"this region has Availability Zones".

The BOM wizard lets a user pick a tier per service (e.g. Storage ``ZRS``,
Azure SQL ``BusinessCritical``, Managed Disks ``Premium_ZRS``). Some of those
tiers are what *enable* zone redundancy. For the subset of services where Azure
exposes an **authoritative capability/SKU API**, we call it live with the
customer's own subscription token and return a concrete verdict:

  * ``available``    — the ZR SKU/edition is offered **and** not restricted for
                        this subscription in this region → safe to deploy.
  * ``blocked``      — offered in the region generally, but restricted for
                        *this subscription* (``NotAvailableForSubscription`` /
                        no usable zones) → needs an access request.
  * ``unavailable``  — not offered in this region at all.
  * ``not_verifiable`` — no authoritative per-subscription API for this service;
                        the caller falls back to the region-level AZ signal and
                        must label the result as documented, not verified.

Authoritative sources used:

  * Storage account redundancy → ``Microsoft.Storage/skus`` (per-sub SKU list
    with ``locations`` + ``restrictions``).
  * Managed Disks ZRS           → ``Microsoft.Compute/skus`` (disks; per-region
    ``zones`` + ``restrictions``).
  * Azure SQL DB / MI editions  → ``Microsoft.Sql/locations/{loc}/capabilities``
    (per-sub supported editions + ``zoneRedundant`` support + ``status``).

Everything else falls through to ``not_verifiable`` on purpose — we would rather
be honest than pretend a docs-derived flag is a live check.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from .bom_services import (
    ARM_BASE,
    COMPUTE_SKUS_API_VERSION,
    DEFAULT_TIMEOUT_S,
    _normalize_region,
    _strip_bearer,
)

log = logging.getLogger(__name__)

STORAGE_SKUS_API_VERSION = "2023-05-01"
SQL_CAPABILITIES_API_VERSION = "2023-08-01-preview"
FLEX_CAPABILITIES_API_VERSION = "2024-08-01"
# Flexible-Server "capabilities by location" api-version differs per provider.
_FLEX_API_VERSIONS = {
    "Microsoft.DBforPostgreSQL": "2024-08-01",
    "Microsoft.DBforMySQL": "2023-06-30",
}
ELASTICSAN_SKUS_API_VERSION = "2024-05-01"

# ── Catalog tier → authoritative ARM target ─────────────────────────────────
#
# Each checkable service maps its zone-redundant tier ids to the concrete ARM
# SKU name / SQL edition to verify. Tiers that are NOT zone-redundant are
# omitted — the caller only sends us zone-redundant selections, but we defend
# by looking the tier up here anyway.

# service name -> {tier_id -> storage SKU name}
_STORAGE_SERVICES: Dict[str, Dict[str, str]] = {
    "Azure Blob Storage": {
        "zrs": "Standard_ZRS",
        "gzrs": "Standard_GZRS",
        "ragzrs": "Standard_RAGZRS",
    },
    "Azure Data Lake Storage Gen2": {
        "zrs": "Standard_ZRS",
        "gzrs": "Standard_GZRS",
        "ragzrs": "Standard_RAGZRS",
    },
    "Azure Files": {
        "zrs": "Standard_ZRS",
        "gzrs": "Standard_GZRS",
    },
}

# service name -> {tier_id -> disk SKU name}
_DISK_SERVICES: Dict[str, Dict[str, str]] = {
    "Managed Disks (Premium SSD)": {
        "standardssd_zrs": "StandardSSD_ZRS",
        "premium_zrs": "Premium_ZRS",
    },
}

# service name -> {tier_id -> Elastic SAN SKU name}. Same locationInfo/zones
# shape as Compute disks, but under its own resource provider.
_ELASTICSAN_SERVICES: Dict[str, Dict[str, str]] = {
    "Azure Elastic SAN": {"zrs": "Premium_ZRS"},
}

# service name -> {tier_id -> SQL edition name}
_SQL_SERVICES: Dict[str, Dict[str, str]] = {
    "Azure SQL Database": {
        "premium": "Premium",
        "general_purpose": "GeneralPurpose",
        "business_critical": "BusinessCritical",
        "hyperscale": "Hyperscale",
    },
    "Azure SQL Managed Instance": {
        "general_purpose": "GeneralPurpose",
        "business_critical": "BusinessCritical",
    },
}

# Flexible-Server databases (PostgreSQL / MySQL): tier -> compute edition name
# as it appears in the location capabilities feed. Zone-redundant HA is offered
# only on the GeneralPurpose / MemoryOptimized editions — never Burstable.
_FLEX_SERVICES: Dict[str, Dict[str, Any]] = {
    "Azure Database for PostgreSQL": {
        "provider": "Microsoft.DBforPostgreSQL",
        "editions": {"general_purpose": "GeneralPurpose", "memory_optimized": "MemoryOptimized"},
    },
    "Azure Database for MySQL": {
        "provider": "Microsoft.DBforMySQL",
        "editions": {"general_purpose": "GeneralPurpose", "memory_optimized": "MemoryOptimized"},
    },
}


def service_check_kind(name: str) -> Optional[str]:
    """Return ``'storage' | 'disks' | 'elasticsan' | 'sql' | 'flex'`` if the
    service has an authoritative per-subscription zonal check, else ``None``."""
    if name in _STORAGE_SERVICES:
        return "storage"
    if name in _DISK_SERVICES:
        return "disks"
    if name in _ELASTICSAN_SERVICES:
        return "elasticsan"
    if name in _SQL_SERVICES:
        return "sql"
    if name in _FLEX_SERVICES:
        return "flex"
    return None


def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _restriction_blocks_region(restrictions: Any, region_norm: str) -> Optional[str]:
    """Return a reason code string if a restriction blocks ``region_norm``,
    else ``None``. Handles both Location- and Zone-type restrictions."""
    for res in restrictions or []:
        if not isinstance(res, dict):
            continue
        reason = res.get("reasonCode") or res.get("reason") or "Restricted"
        rtype = (res.get("type") or "").lower()
        # Newer shape: restrictionInfo.locations; older: values.
        info = res.get("restrictionInfo") or {}
        locs = info.get("locations") or res.get("values") or []
        norm_locs = {_normalize_region(str(l)) for l in locs}
        if rtype == "location":
            if not locs or region_norm in norm_locs:
                return str(reason)
        # A zone-type restriction that names our region still limits it; the
        # per-SKU zone data below decides whether any zone survives.
    return None


# ── Microsoft.Storage/skus ──────────────────────────────────────────────────

def fetch_storage_sku_state(
    *, arm_token: str, subscription_id: str, region: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Dict[str, Dict[str, Any]]:
    """Return ``{sku_name_lower: {"offered": bool, "restricted": bool,
    "reason": str}}`` for the given region from the per-subscription Storage
    SKU list. ``offered`` means the SKU lists the region; ``restricted`` means a
    subscription restriction blocks it there."""
    token = _strip_bearer(arm_token)
    region_norm = _normalize_region(region)
    url = f"{ARM_BASE}/subscriptions/{subscription_id}/providers/Microsoft.Storage/skus"
    params = {"api-version": STORAGE_SKUS_API_VERSION}
    out: Dict[str, Dict[str, Any]] = {}
    with httpx.Client(timeout=timeout_s, http2=False) as client:
        r = client.get(url, params=params, headers=_headers(token))
        if r.status_code in (401, 403):
            log.warning("storage skus: ARM %d for sub=%s — unverifiable", r.status_code, subscription_id)
            return {}
        r.raise_for_status()
        data = r.json()
    for item in data.get("value") or []:
        name = str(item.get("name") or "")
        if not name:
            continue
        key = name.lower()
        locs = {_normalize_region(str(l)) for l in (item.get("locations") or [])}
        offered_here = region_norm in locs
        reason = _restriction_blocks_region(item.get("restrictions"), region_norm)
        prev = out.get(key)
        entry = {
            "offered": offered_here or (prev or {}).get("offered", False),
            "restricted": bool(reason) or (prev or {}).get("restricted", False),
            "reason": reason or (prev or {}).get("reason", ""),
        }
        out[key] = entry
    return out


# ── Microsoft.Compute/skus (disks) ──────────────────────────────────────────

def fetch_disk_sku_state(
    *, arm_token: str, subscription_id: str, region: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Dict[str, Dict[str, Any]]:
    """Return ``{disk_sku_lower: {"zones": [..], "restricted": bool,
    "reason": str}}`` for the region from ``Microsoft.Compute/skus``."""
    token = _strip_bearer(arm_token)
    region_norm = _normalize_region(region)
    url = f"{ARM_BASE}/subscriptions/{subscription_id}/providers/Microsoft.Compute/skus"
    params = {"api-version": COMPUTE_SKUS_API_VERSION, "$filter": "resourceType eq 'disks'"}
    out: Dict[str, Dict[str, Any]] = {}
    next_url: Optional[str] = url
    with httpx.Client(timeout=timeout_s, http2=False) as client:
        while next_url:
            r = client.get(next_url, params=params if next_url == url else None, headers=_headers(token))
            if r.status_code in (401, 403):
                log.warning("disk skus: ARM %d for sub=%s — unverifiable", r.status_code, subscription_id)
                return {}
            r.raise_for_status()
            data = r.json()
            for item in data.get("value") or []:
                if item.get("resourceType") != "disks":
                    continue
                name = str(item.get("name") or "")
                if not name:
                    continue
                zones: List[str] = []
                for loc_info in item.get("locationInfo") or []:
                    if _normalize_region(str(loc_info.get("location") or "")) == region_norm:
                        zones = [str(z) for z in (loc_info.get("zones") or [])]
                reason = _restriction_blocks_region(item.get("restrictions"), region_norm)
                key = name.lower()
                out[key] = {"zones": sorted(set(zones)), "restricted": bool(reason), "reason": reason or ""}
            next_url = data.get("nextLink") or None
    return out


# ── Microsoft.ElasticSan/skus ───────────────────────────────────────────────

def fetch_elasticsan_sku_state(
    *, arm_token: str, subscription_id: str, region: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Dict[str, Dict[str, Any]]:
    """Return ``{sku_lower: {"zones": [..], "restricted": bool, "reason": str}}``
    for the region from ``Microsoft.ElasticSan/skus``. Restrictions may sit at
    the item level or inside ``locationInfo``; both are honoured. Degrades to
    ``{}`` (→ unverifiable) on any ARM error."""
    token = _strip_bearer(arm_token)
    region_norm = _normalize_region(region)
    url = f"{ARM_BASE}/subscriptions/{subscription_id}/providers/Microsoft.ElasticSan/skus"
    params = {"api-version": ELASTICSAN_SKUS_API_VERSION}
    try:
        with httpx.Client(timeout=timeout_s, http2=False) as client:
            r = client.get(url, params=params, headers=_headers(token))
            if r.status_code >= 400:
                log.warning("elasticsan skus: ARM %d for sub=%s — unverifiable", r.status_code, subscription_id)
                return {}
            data = r.json()
    except Exception as ex:  # pragma: no cover - defensive
        log.warning("elasticsan skus request failed: %r", ex)
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    for item in data.get("value") or []:
        name = str(item.get("name") or "")
        if not name:
            continue
        zones: List[str] = []
        loc_restrictions: List[Any] = []
        for loc_info in item.get("locationInfo") or []:
            if _normalize_region(str(loc_info.get("location") or "")) == region_norm:
                zones = [str(z) for z in (loc_info.get("zones") or [])]
                loc_restrictions = loc_info.get("restrictions") or []
        reason = (_restriction_blocks_region(item.get("restrictions"), region_norm)
                  or _restriction_blocks_region(loc_restrictions, region_norm))
        out[name.lower()] = {"zones": sorted(set(zones)), "restricted": bool(reason), "reason": reason or ""}
    return out


# ── Microsoft.Sql/locations/{loc}/capabilities ──────────────────────────────

def _find_zone_redundant(node: Any) -> bool:
    """Recursively scan a SQL capability subtree for any ``zoneRedundant: true``
    on an available service objective."""
    if isinstance(node, dict):
        if node.get("zoneRedundant") is True:
            status = str(node.get("status") or "Available")
            if status.lower() != "disabled":
                return True
        return any(_find_zone_redundant(v) for v in node.values())
    if isinstance(node, list):
        return any(_find_zone_redundant(v) for v in node)
    return False


def fetch_sql_edition_state(
    *, arm_token: str, subscription_id: str, region: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Dict[str, Dict[str, Any]]:
    """Return ``{edition_name_lower: {"status": str, "zone_redundant": bool}}``
    for the region from the per-subscription SQL capabilities API. Covers both
    single-database editions and managed-instance editions."""
    token = _strip_bearer(arm_token)
    region_norm = _normalize_region(region)
    url = (f"{ARM_BASE}/subscriptions/{subscription_id}/providers/Microsoft.Sql"
           f"/locations/{region_norm}/capabilities")
    params = {"api-version": SQL_CAPABILITIES_API_VERSION}
    with httpx.Client(timeout=timeout_s, http2=False) as client:
        r = client.get(url, params=params, headers=_headers(token))
        if r.status_code in (401, 403):
            log.warning("sql caps: ARM %d for sub=%s — unverifiable", r.status_code, subscription_id)
            return {}
        if r.status_code == 404:
            return {}  # region not offered for SQL on this sub
        r.raise_for_status()
        data = r.json()

    out: Dict[str, Dict[str, Any]] = {}

    def _record_editions(editions: Any) -> None:
        for ed in editions or []:
            if not isinstance(ed, dict):
                continue
            name = str(ed.get("name") or "")
            if not name:
                continue
            key = name.lower()
            status = str(ed.get("status") or "Available")
            zr = _find_zone_redundant(ed)
            prev = out.get(key)
            out[key] = {
                "status": status if not prev else prev["status"],
                "zone_redundant": zr or (prev or {}).get("zone_redundant", False),
            }

    for ver in data.get("supportedServerVersions") or []:
        _record_editions(ver.get("supportedEditions"))
    for ver in data.get("supportedManagedInstanceVersions") or []:
        _record_editions(ver.get("supportedEditions"))
    return out


# ── PostgreSQL / MySQL Flexible Server capabilities ─────────────────────────

_REGION_ZR_KEY = "__region_zr__"


def _flag_to_bool(value: Any) -> Optional[bool]:
    """Interpret Azure's ``zoneRedundantHaSupported`` which may be a bool or a
    string ("Enabled"/"Disabled"). ``None`` means "not stated" → don't block."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("enabled", "true", "yes", "supported"):
            return True
        if v in ("disabled", "false", "no", "notsupported"):
            return False
    return None


def _has_zone_redundant_ha(node: Any) -> bool:
    """Recursively detect a ``ZoneRedundant`` high-availability mode anywhere in
    a capability subtree (``supportedHaMode`` /
    ``supportedHighAvailabilityModes`` lists of strings)."""
    if isinstance(node, str):
        return node.replace(" ", "").replace("_", "").lower() == "zoneredundant"
    if isinstance(node, dict):
        return any(_has_zone_redundant_ha(v) for v in node.values())
    if isinstance(node, list):
        return any(_has_zone_redundant_ha(v) for v in node)
    return False


def _has_ha_info(node: Any) -> bool:
    """True if a capability subtree carries any per-SKU high-availability mode
    listing (``supportedHaMode`` / ``supportedHighAvailabilityModes``).
    PostgreSQL editions carry this (authoritative, per-edition); MySQL editions
    do not (HA is signalled at the zone level instead)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).replace("_", "").lower() in ("supportedhamode", "supportedhighavailabilitymodes"):
                return True
            if _has_ha_info(v):
                return True
        return False
    if isinstance(node, list):
        return any(_has_ha_info(v) for v in node)
    return False


def _iter_flex_editions(node: Any):
    """Yield edition dicts (those carrying a ``name`` and a SKU list) from the
    Flexible Server capability payload, whatever the api-version nesting."""
    if isinstance(node, dict):
        keys = set(node.keys())
        if "name" in node and (
            "supportedServerSkus" in keys or "supportedSkus" in keys
            or "supportedServerVersions" in keys
        ):
            yield node
        for v in node.values():
            yield from _iter_flex_editions(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_flex_editions(v)


def fetch_flex_edition_state(
    *, provider: str, arm_token: str, subscription_id: str, region: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Dict[str, Dict[str, Any]]:
    """Return ``{edition_name_lower: {"zone_redundant": bool}}`` plus a
    ``__region_zr__`` entry for a PostgreSQL/MySQL Flexible Server provider from
    its per-subscription ``locations/{loc}/capabilities`` feed. Degrades to
    ``{}`` (→ unverifiable) on any ARM error so the caller falls back to
    region-AZ.

    The two providers expose zone-redundant HA differently:
      * PostgreSQL — a region-level ``zoneRedundantHaSupported`` flag plus a
        per-SKU ``supportedHaMode`` list under each ``supportedServerEditions``.
      * MySQL — the ``value`` feed is partitioned per zone; each item carries a
        zone-level ``supportedHAMode`` list and a flat
        ``supportedFlexibleServerEditions`` list (no per-SKU HA). Zone-redundant
        HA therefore applies to every offered edition when the item's HA list
        contains ``ZoneRedundant``.
    """
    token = _strip_bearer(arm_token)
    region_norm = _normalize_region(region)
    api_version = _FLEX_API_VERSIONS.get(provider, FLEX_CAPABILITIES_API_VERSION)
    url = (f"{ARM_BASE}/subscriptions/{subscription_id}/providers/{provider}"
           f"/locations/{region_norm}/capabilities")
    params = {"api-version": api_version}
    try:
        with httpx.Client(timeout=timeout_s, http2=False) as client:
            r = client.get(url, params=params, headers=_headers(token))
            if r.status_code >= 400:
                log.warning("flex caps: ARM %d for %s sub=%s — unverifiable",
                            r.status_code, provider, subscription_id)
                return {}
            data = r.json()
    except Exception as ex:  # pragma: no cover - defensive
        log.warning("flex caps request failed for %s: %r", provider, ex)
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    region_zr: Optional[bool] = None
    for item in data.get("value") or [data]:
        if not isinstance(item, dict):
            continue
        # Region-level ZR signal: PostgreSQL's explicit flag, or MySQL's
        # zone-level supportedHAMode list containing "ZoneRedundant".
        flag = _flag_to_bool(item.get("zoneRedundantHaSupported"))
        zone_ha_zr = _has_zone_redundant_ha(
            item.get("supportedHAMode") or item.get("supportedHaMode"))
        if flag is True or zone_ha_zr:
            item_zr: Optional[bool] = True
        elif flag is False:
            item_zr = False
        else:
            item_zr = None
        if item_zr is True:
            region_zr = True
        elif item_zr is False and region_zr is None:
            region_zr = False

        for ed in _iter_flex_editions(item):
            name = str(ed.get("name") or "")
            if not name:
                continue
            key = name.lower()
            # PostgreSQL carries authoritative per-SKU HA modes on the edition;
            # trust those. MySQL editions carry none — fall back to the item's
            # zone-level ZR signal for those.
            if _has_ha_info(ed):
                has_zr = _has_zone_redundant_ha(ed)
            else:
                has_zr = item_zr is True
            prev = out.get(key)
            out[key] = {"zone_redundant": has_zr or (prev or {}).get("zone_redundant", False)}
    out[_REGION_ZR_KEY] = {"value": region_zr}
    return out


# ── Verdict aggregation ─────────────────────────────────────────────────────

_SOURCE_LABEL = {
    "storage": "Microsoft.Storage/skus",
    "disks": "Microsoft.Compute/skus (disks)",
    "elasticsan": "Microsoft.ElasticSan/skus",
    "sql": "Microsoft.Sql capabilities",
    "flex": "Flexible Server capabilities",
}


def _storage_verdict(state: Dict[str, Dict[str, Any]], sku: str) -> Dict[str, Any]:
    entry = state.get(sku.lower())
    if not state:
        return {"verdict": "unverifiable", "message": "Storage SKU list unavailable for this subscription."}
    if not entry or not entry.get("offered"):
        return {"verdict": "unavailable",
                "message": f"{sku} is not offered in this region — zone-redundant storage can't be created here."}
    if entry.get("restricted"):
        return {"verdict": "blocked",
                "message": f"{sku} is restricted for this subscription in the region "
                           f"({entry.get('reason') or 'NotAvailableForSubscription'}) — request access before use."}
    return {"verdict": "available", "message": f"{sku} is offered and unrestricted for this subscription."}


def _disk_verdict(state: Dict[str, Dict[str, Any]], sku: str) -> Dict[str, Any]:
    entry = state.get(sku.lower())
    if not state:
        return {"verdict": "unverifiable", "message": "Disk SKU list unavailable for this subscription."}
    if not entry or not entry.get("zones"):
        if entry and entry.get("restricted"):
            return {"verdict": "blocked",
                    "message": f"{sku} is restricted for this subscription in the region "
                               f"({entry.get('reason') or 'restricted'})."}
        return {"verdict": "unavailable",
                "message": f"{sku} exposes no availability zones in this region — ZRS disks can't be created here."}
    if entry.get("restricted"):
        return {"verdict": "blocked",
                "message": f"{sku} is restricted for this subscription ({entry.get('reason') or 'restricted'})."}
    return {"verdict": "available",
            "message": f"{sku} is offered across {len(entry['zones'])} zone(s) and unrestricted."}


def _elasticsan_verdict(state: Dict[str, Dict[str, Any]], sku: str) -> Dict[str, Any]:
    entry = state.get(sku.lower())
    if not state:
        return {"verdict": "unverifiable", "message": "Elastic SAN SKU list unavailable for this subscription."}
    if not entry or not entry.get("zones"):
        if entry and entry.get("restricted"):
            return {"verdict": "blocked",
                    "message": f"{sku} is restricted for this subscription in the region "
                               f"({entry.get('reason') or 'restricted'})."}
        return {"verdict": "unavailable",
                "message": f"{sku} exposes no availability zones in this region — a zone-redundant "
                           f"Elastic SAN can't be created here."}
    if entry.get("restricted"):
        return {"verdict": "blocked",
                "message": f"{sku} is restricted for this subscription ({entry.get('reason') or 'restricted'})."}
    return {"verdict": "available",
            "message": f"{sku} is offered across {len(entry['zones'])} zone(s) and unrestricted."}


def _sql_verdict(state: Dict[str, Dict[str, Any]], edition: str) -> Dict[str, Any]:
    entry = state.get(edition.lower())
    if not state:
        return {"verdict": "unverifiable", "message": "SQL capabilities unavailable for this subscription/region."}
    if not entry or str(entry.get("status", "")).lower() == "disabled":
        return {"verdict": "unavailable",
                "message": f"The {edition} edition is not available in this region for this subscription."}
    if not entry.get("zone_redundant"):
        return {"verdict": "blocked",
                "message": f"The {edition} edition is available but zone-redundant deployment isn't offered "
                           f"here for this subscription."}
    return {"verdict": "available",
            "message": f"The {edition} edition supports zone-redundant deployment for this subscription."}


def _flex_verdict(state: Dict[str, Dict[str, Any]], edition: str) -> Dict[str, Any]:
    if not state:
        return {"verdict": "unverifiable",
                "message": "Flexible Server capabilities unavailable for this subscription/region."}
    region_zr = (state.get(_REGION_ZR_KEY) or {}).get("value")
    entry = state.get(edition.lower())
    if region_zr is False:
        return {"verdict": "blocked",
                "message": "Zone-redundant HA is not supported in this region for this subscription."}
    if not entry:
        return {"verdict": "unavailable",
                "message": f"The {edition} tier is not offered in this region for this subscription."}
    if not entry.get("zone_redundant"):
        return {"verdict": "blocked",
                "message": f"The {edition} tier is available but no SKU here supports zone-redundant HA "
                           f"for this subscription."}
    return {"verdict": "available",
            "message": f"The {edition} tier supports zone-redundant HA for this subscription."}


def evaluate(
    *,
    services: List[Dict[str, Any]],
    region: str,
    arm_token: str,
    subscription_id: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> List[Dict[str, Any]]:
    """For each ``{name, tier}`` selection that maps to an authoritative check,
    return a live verdict. Selections with no authoritative API are returned
    with ``verdict='not_verifiable'`` so the caller can fall back to region-AZ.

    State is fetched lazily and once per kind (a single ARM round-trip covers
    every SKU/edition for that kind in the region)."""
    region_norm = _normalize_region(region)
    storage_state: Optional[Dict] = None
    disk_state: Optional[Dict] = None
    elasticsan_state: Optional[Dict] = None
    sql_state: Optional[Dict] = None
    flex_state: Dict[str, Optional[Dict]] = {}

    results: List[Dict[str, Any]] = []
    for svc in services or []:
        name = str((svc or {}).get("name") or "")
        tier = str((svc or {}).get("tier") or "")
        kind = service_check_kind(name)
        base = {"name": name, "tier": tier, "checkable": bool(kind), "source": _SOURCE_LABEL.get(kind or "", "")}

        if kind == "storage":
            sku = _STORAGE_SERVICES[name].get(tier)
            if not sku:
                results.append({**base, "verdict": "not_verifiable", "message": "Selected tier is not zone-redundant."})
                continue
            if storage_state is None:
                storage_state = fetch_storage_sku_state(
                    arm_token=arm_token, subscription_id=subscription_id, region=region_norm, timeout_s=timeout_s)
            results.append({**base, "target": sku, **_storage_verdict(storage_state, sku)})
        elif kind == "disks":
            sku = _DISK_SERVICES[name].get(tier)
            if not sku:
                results.append({**base, "verdict": "not_verifiable", "message": "Selected tier is not zone-redundant."})
                continue
            if disk_state is None:
                disk_state = fetch_disk_sku_state(
                    arm_token=arm_token, subscription_id=subscription_id, region=region_norm, timeout_s=timeout_s)
            results.append({**base, "target": sku, **_disk_verdict(disk_state, sku)})
        elif kind == "elasticsan":
            sku = _ELASTICSAN_SERVICES[name].get(tier)
            if not sku:
                results.append({**base, "verdict": "not_verifiable", "message": "Selected tier is not zone-redundant."})
                continue
            if elasticsan_state is None:
                elasticsan_state = fetch_elasticsan_sku_state(
                    arm_token=arm_token, subscription_id=subscription_id, region=region_norm, timeout_s=timeout_s)
            results.append({**base, "target": sku, **_elasticsan_verdict(elasticsan_state, sku)})
        elif kind == "sql":
            edition = _SQL_SERVICES[name].get(tier)
            if not edition:
                results.append({**base, "verdict": "not_verifiable", "message": "Selected tier is not zone-redundant."})
                continue
            if sql_state is None:
                sql_state = fetch_sql_edition_state(
                    arm_token=arm_token, subscription_id=subscription_id, region=region_norm, timeout_s=timeout_s)
            results.append({**base, "target": edition, **_sql_verdict(sql_state, edition)})
        elif kind == "flex":
            spec = _FLEX_SERVICES[name]
            edition = spec["editions"].get(tier)
            if not edition:
                results.append({**base, "verdict": "not_verifiable", "message": "Selected tier is not zone-redundant."})
                continue
            provider = spec["provider"]
            if provider not in flex_state:
                flex_state[provider] = fetch_flex_edition_state(
                    provider=provider, arm_token=arm_token, subscription_id=subscription_id,
                    region=region_norm, timeout_s=timeout_s)
            results.append({**base, "target": edition, **_flex_verdict(flex_state[provider], edition)})
        else:
            results.append({**base, "verdict": "not_verifiable",
                            "message": "No authoritative per-subscription API for this service — "
                                       "relying on region Availability-Zone support."})
    return results

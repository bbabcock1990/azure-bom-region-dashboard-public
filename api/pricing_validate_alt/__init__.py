"""POST /api/pricing/validate-alternatives

Validate that a cheaper size-equivalent SKU the cost model suggested is actually
usable in a given region + subscription. For each candidate family we check, live
against ARM:

  * availability   — is the family offered in the region, and in which AZs?
  * restrictions   — is it region-restricted or zone-restricted for this sub?
  * quota          — is there enough regional vCPU headroom for the required cores?

Request body::

    {
      "subscription_id": "<sub guid>",
      "region": "australiaeast",
      "alternatives": [
        {"family": "Dpsv6", "required_cores": 1000},
        {"family": "Dasv5", "required_cores": 1000}
      ]
    }

``family`` is the core-form label the pricing model emits (e.g. ``Dpsv6``); it is
mapped to the ARM family id ``standardDpsv6Family`` for the lookups (ARM matches
case-insensitively). The response carries a per-family verdict so the UI can turn
the old "verify this yourself" disclaimer into a real ✅ / ⚠️ / ⛔ badge and, when
blocked, deep-link into the quota / zonal-access ticket flows.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

from .._shared import auth, csrf, auth_token
from .._shared import httpfunc as func
from .._shared import pricing, sku_capabilities
from .._shared.arm_sku_availability import fetch_arm_sku_records
from .._shared.arm_skus import fetch_region_capabilities
from .._shared.quota_groups import check_subscription_quota

log = logging.getLogger(__name__)

_MAX_ALTERNATIVES = 12


def _ok(payload: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload), status_code=status, mimetype="application/json"
    )


def _err(code: str, message: str, status: int = 400) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": code, "message": message}),
        status_code=status,
        mimetype="application/json",
    )


def _arm_family_id(core: str) -> str:
    """Core-form label (``Dpsv6``) -> ARM family id (``standardDpsv6Family``).

    ARM's ``Microsoft.Compute/skus`` ``family`` and the usages ``name`` compare
    case-insensitively downstream, so the exact internal casing does not matter.
    """
    return f"standard{str(core).strip()}Family"


def _num(value):
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _verdict_for(
    *,
    offered: bool,
    region_restricted: bool,
    zones: List[bool],
    quota: Dict[str, Any],
    required_cores: int,
) -> Dict[str, Any]:
    """Fold availability + restrictions + quota into a single verdict."""
    zones = zones or [False, False, False]
    available_zone_count = sum(1 for z in zones if z)
    zone_limited = offered and not region_restricted and 0 < available_zone_count < 3

    headroom = _num(quota.get("headroom"))
    quota_known = headroom is not None
    shortfall = None
    quota_enough = None
    if quota_known:
        shortfall = max(0.0, float(required_cores) - headroom)
        quota_enough = shortfall <= 0

    if not offered:
        return {
            "verdict": "unavailable",
            "message": "Not offered in this region — cannot switch here.",
            "zone_limited": False,
            "quota_enough": quota_enough,
            "shortfall": shortfall,
        }
    if region_restricted:
        return {
            "verdict": "restricted",
            "message": "Restricted for this subscription in the region — needs an "
                       "access (zonal/region enablement) request before use.",
            "zone_limited": zone_limited,
            "quota_enough": quota_enough,
            "shortfall": shortfall,
        }
    if available_zone_count == 0:
        return {
            "verdict": "restricted",
            "message": "Offered but every availability zone is restricted — needs a "
                       "zonal-access request before use.",
            "zone_limited": True,
            "quota_enough": quota_enough,
            "shortfall": shortfall,
        }
    if quota_known and not quota_enough:
        need = int(round(shortfall or 0))
        return {
            "verdict": "quota",
            "message": f"Available{' (limited AZs)' if zone_limited else ''}, but "
                       f"quota is short by {need} vCPU — a quota increase is needed.",
            "zone_limited": zone_limited,
            "quota_enough": False,
            "shortfall": shortfall,
        }
    if not quota_known:
        return {
            "verdict": "unknown",
            "message": "Available; quota could not be read for this subscription — "
                       "verify vCPU headroom before switching.",
            "zone_limited": zone_limited,
            "quota_enough": None,
            "shortfall": None,
        }
    msg = "Available and quota is sufficient."
    if zone_limited:
        blocked = [str(i) for i, ok in enumerate(zones, start=1) if not ok]
        msg = f"Available (not in AZ {', '.join(blocked)}) and quota is sufficient."
    return {
        "verdict": "ok",
        "message": msg,
        "zone_limited": zone_limited,
        "quota_enough": True,
        "shortfall": 0.0,
    }


async def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)

    try:
        csrf.assert_safe_origin(req)
    except csrf.OriginError as ex:
        return _err("origin_rejected", str(ex), 403)

    try:
        body = req.get_json()
    except ValueError:
        return _err("bad_json", "Body must be a JSON object.", 400)
    if not isinstance(body, dict):
        return _err("bad_json", "Body must be a JSON object.", 400)

    region = str(body.get("region") or "").strip().lower()
    subscription_id = str(body.get("subscription_id") or "").strip()
    raw_alts = body.get("alternatives")
    if not region:
        return _err("no_region", "Body.region is required.", 400)
    if not isinstance(raw_alts, list) or not raw_alts:
        return _err("no_alternatives", "Body.alternatives must be a non-empty array.", 400)

    alts: List[Dict[str, Any]] = []
    seen: set = set()
    for item in raw_alts[:_MAX_ALTERNATIVES]:
        if not isinstance(item, dict):
            continue
        core = str(item.get("family") or "").strip()
        if not core:
            continue
        key = core.lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            cores = int(float(item.get("required_cores") or 0))
        except (TypeError, ValueError):
            cores = 0
        from_core = pricing._family_core(str(item.get("from_family") or "").strip())
        alts.append({
            "family": core,
            "arm_id": _arm_family_id(core),
            "required_cores": max(0, cores),
            "from_family": from_core,
            "core": pricing._family_core(core).lower(),
        })
    if not alts:
        return _err("no_alternatives", "No valid alternatives supplied.", 400)

    if not subscription_id:
        results = {
            a["family"]: {
                "family": a["family"],
                "verdict": "unknown",
                "message": "Select a subscription to validate availability and quota.",
                "required_cores": a["required_cores"],
            }
            for a in alts
        }
        return _ok({
            "region": region,
            "subscription_id": "",
            "quota_status": "no_subscription",
            "results": results,
        })

    arm_ids = [a["arm_id"] for a in alts]

    token_getter = getattr(
        auth_token, "get_arm_token_for_subscription", auth_token.get_arm_token
    )
    try:
        token_info = await asyncio.to_thread(token_getter, subscription_id)
    except auth_token.AuthError as ex:
        return _err("auth_error", str(ex), 401)
    arm_token = token_info.token

    try:
        avail_rows = await asyncio.to_thread(
            fetch_arm_sku_records,
            arm_token=arm_token,
            subscription_id=subscription_id,
            want_regions=[region],
            want_families=arm_ids,
        )
    except Exception as ex:  # pragma: no cover - defensive
        log.exception("alt availability lookup failed")
        return _err("availability_failed", f"Availability lookup failed: {ex!r}", 502)

    avail_by_family: Dict[str, Dict[str, Any]] = {}
    for row in avail_rows or []:
        avail_by_family[str(row.get("family") or "").lower()] = row

    quota = await asyncio.to_thread(
        check_subscription_quota,
        arm_token,
        subscription_id,
        [region],
        arm_ids,
        timeout_s=15.0,
    )
    quota_region = (quota.get("regions") or {}).get(region, {})
    quota_families = quota_region.get("families") or {}
    quota_status = quota_region.get("status") or quota.get("status") or "unknown"

    # Authoritative capability parity: a cheaper swap must support everything the
    # original BOM size does (temp disk, premium/ultra disk, accelerated
    # networking, encryption-at-host, Hyper-V generation, memory). Best-effort —
    # if the capabilities call fails we leave parity "unknown" rather than block.
    core_index: Dict[str, Any] = {}
    try:
        caps_by_size = await asyncio.to_thread(
            fetch_region_capabilities,
            arm_token=arm_token,
            subscription_id=subscription_id,
            region=region,
            timeout_s=20.0,
        )
        core_index = sku_capabilities.index_by_core(caps_by_size)
    except Exception:  # pragma: no cover - defensive
        log.exception("alt capability lookup failed")
        core_index = {}

    results: Dict[str, Any] = {}
    for a in alts:
        arm_lower = a["arm_id"].lower()
        row = avail_by_family.get(arm_lower)
        prov = (row or {}).get("arm_provenance")
        offered = bool(prov)  # arm_provenance only present when the SKU is offered
        region_restricted = bool(prov.get("region_restricted")) if prov else False
        zones = list((row or {}).get("zones") or [False, False, False])
        available_zones = list((prov or {}).get("available_zones") or [])
        restricted_zones = list((prov or {}).get("restricted_zones") or [])
        sub_restriction_raw = (row or {}).get("sub_restriction_raw") or (
            "SKU not in region" if not offered else "Available"
        )

        q = quota_families.get(a["arm_id"]) or {}
        verdict = _verdict_for(
            offered=offered,
            region_restricted=region_restricted,
            zones=zones,
            quota=q,
            required_cores=a["required_cores"],
        )

        # Capability parity — only meaningful when the SKU is actually offered
        # and we know the original family to compare against. A parity failure
        # takes precedence over quota/zone verdicts: no quota bump makes an
        # incompatible size a valid swap.
        parity = {"status": "unknown", "missing": [], "vcpus": None}
        if offered and a.get("from_family") and core_index:
            parity = sku_capabilities.parity_check(
                core_index, a["from_family"], a["core"]
            )
        v_name = verdict["verdict"]
        v_msg = verdict["message"]
        if offered and parity.get("status") == "incompatible":
            labels = [m["cap"] for m in parity.get("missing") or []]
            v_name = "incompatible"
            v_msg = ("Not capability-equivalent — missing "
                     + ", ".join(labels) + " that the current size has.")

        results[a["family"]] = {
            "family": a["family"],
            "arm_family": a["arm_id"],
            "required_cores": a["required_cores"],
            "verdict": v_name,
            "message": v_msg,
            "offered": offered,
            "region_restricted": region_restricted,
            "zone_limited": verdict.get("zone_limited", False),
            "zones": [bool(z) for z in zones],
            "available_zones": available_zones,
            "restricted_zones": restricted_zones,
            "sub_restriction_raw": sub_restriction_raw,
            "parity": {
                "status": parity.get("status"),
                "missing": parity.get("missing") or [],
                "compared_vcpus": parity.get("vcpus"),
            },
            "quota": {
                "limit": _num(q.get("limit")),
                "usage": _num(q.get("usage")),
                "headroom": _num(q.get("headroom")),
                "shortfall": verdict.get("shortfall"),
                "enough": verdict.get("quota_enough"),
            },
        }

    return _ok({
        "region": region,
        "subscription_id": subscription_id,
        "quota_status": quota_status,
        "results": results,
    })

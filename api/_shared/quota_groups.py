"""Best-effort Azure Quota Groups lookups."""
from __future__ import annotations

import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, Iterable, List, Optional, Tuple

import httpx

log = logging.getLogger(__name__)

ARM_BASE = "https://management.azure.com"
API_VERSION = "2023-06-01-preview"
# Group Quotas live at management-group scope. groupQuotaLimits/{location}
# returns per-family limit + availableLimit (allocatable headroom).
GROUP_QUOTA_API_VERSION = "2025-09-01"
MGMT_GROUP_API_VERSION = "2020-05-01"
GROUP_QUOTA_RP = "Microsoft.Compute"
MAX_MGMT_GROUPS_SCANNED = 25
COMPUTE_USAGES_API_VERSION = "2023-03-01"
DEFAULT_TIMEOUT_S = 20.0
MAX_PARALLEL_REGIONS = 8
MAX_RETRY_ATTEMPTS = 4


def _strip_bearer(token: Optional[str]) -> str:
    text = (token or "").strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    return text


def _norm_region(value) -> Optional[str]:
    s = str(value or "").strip().lower()
    return s or None


def _norm_family(value) -> Optional[str]:
    s = str(value or "").strip()
    return s.lower() if s else None


def _num(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except Exception:
        return None


def _clean_num(value):
    num = _num(value)
    if num is None:
        return None
    return int(num) if float(num).is_integer() else num


def _headroom(limit, usage):
    limit_num = _clean_num(limit)
    usage_num = _clean_num(usage)
    if limit_num is None or usage_num is None:
        return None
    value = float(limit_num) - float(usage_num)
    return int(value) if value.is_integer() else value


def _retry_after_seconds(value: Optional[str]) -> float:
    if not value:
        return 1.0
    try:
        return max(0.0, min(15.0, float(value)))
    except (TypeError, ValueError):
        pass
    try:
        when = parsedate_to_datetime(str(value))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        delay = (when - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, min(15.0, delay))
    except Exception:
        return 1.0


def _get_with_retries(
    client: httpx.Client,
    url: str,
    *,
    params: Optional[Dict[str, str]],
    headers: Dict[str, str],
) -> httpx.Response:
    last = None
    for attempt in range(1, MAX_RETRY_ATTEMPTS + 1):
        resp = client.get(url, params=params, headers=headers)
        last = resp
        if resp.status_code in (401, 403):
            return resp
        if resp.status_code == 429:
            if attempt >= MAX_RETRY_ATTEMPTS:
                return resp
            time.sleep(_retry_after_seconds(resp.headers.get("Retry-After")))
            continue
        if 500 <= resp.status_code < 600:
            if attempt >= MAX_RETRY_ATTEMPTS:
                return resp
            time.sleep((2 ** attempt) + (random.random() * 0.5))
            continue
        return resp
    return last  # pragma: no cover


def _pick_limit(obj: Dict) -> Optional[float]:
    candidates = (
        obj.get("limit"),
        obj.get("quotaLimit"),
        obj.get("allocated"),
        obj.get("allocatedToSubscription"),
        obj.get("value"),
        (obj.get("properties") or {}).get("limit") if isinstance(obj.get("properties"), dict) else None,
    )
    for item in candidates:
        if isinstance(item, dict):
            for key in ("value", "limit", "quotaLimit"):
                val = _num(item.get(key))
                if val is not None:
                    return val
        else:
            val = _num(item)
            if val is not None:
                return val
    return None


def _pick_usage(obj: Dict) -> Optional[float]:
    candidates = (
        obj.get("usage"),
        obj.get("currentUsage"),
        obj.get("consumed"),
        obj.get("utilized"),
        (obj.get("properties") or {}).get("usage") if isinstance(obj.get("properties"), dict) else None,
    )
    for item in candidates:
        if isinstance(item, dict):
            for key in ("value", "usage", "currentUsage"):
                val = _num(item.get(key))
                if val is not None:
                    return val
        else:
            val = _num(item)
            if val is not None:
                return val
    return None


def _pick_family(obj: Dict) -> Optional[str]:
    for key in ("family", "vmFamily", "vmFamilyName", "name"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    props = obj.get("properties")
    if isinstance(props, dict):
        return _pick_family(props)
    return None


def _pick_usage_name(obj: Dict) -> Optional[str]:
    name = obj.get("name")
    if isinstance(name, dict):
        for key in ("value", "localizedValue"):
            value = name.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _walk_family_rows(node, *, region_hint: Optional[str], out: List[Dict]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk_family_rows(item, region_hint=region_hint, out=out)
        return
    if not isinstance(node, dict):
        return

    region = _norm_region(node.get("region") or node.get("location") or region_hint)
    family = _pick_family(node)
    limit = _pick_limit(node)
    usage = _pick_usage(node)
    if family and region and (limit is not None or usage is not None):
        out.append({
            "family": family,
            "region": region,
            "limit": limit,
            "usage": usage,
        })

    for value in node.values():
        if isinstance(value, dict):
            child_region = region or _norm_region(value.get("region") or value.get("location"))
            _walk_family_rows(value, region_hint=child_region, out=out)
        elif isinstance(value, list):
            _walk_family_rows(value, region_hint=region, out=out)


def _list_subscription_usages_for_region(
    client: httpx.Client,
    *,
    subscription_id: str,
    region: str,
    headers: Dict[str, str],
) -> Dict:
    url = (
        f"{ARM_BASE}/subscriptions/{subscription_id}/providers/"
        f"Microsoft.Compute/locations/{region}/usages"
    )
    params = {"api-version": COMPUTE_USAGES_API_VERSION}
    items: List[Dict] = []
    while True:
        resp = _get_with_retries(client, url, params=params, headers=headers)
        if resp.status_code in (401, 403):
            return {
                "status": "no_access",
                "error": f"Subscription quota call returned {resp.status_code}.",
            }
        if resp.status_code == 404:
            return {
                "status": "not_found",
                "error": f"Subscription quota call returned 404 for region {region}.",
            }
        if resp.status_code >= 400:
            return {
                "status": "error",
                "error": (
                    f"Subscription quota call returned {resp.status_code} "
                    f"for region {region}."
                ),
            }
        try:
            payload = resp.json()
        except Exception as ex:
            return {
                "status": "error",
                "error": f"Subscription quota response was not valid JSON: {ex!r}",
            }
        page_items = payload.get("value") if isinstance(payload, dict) else None
        if isinstance(page_items, list):
            items.extend(page_items)
        next_link = payload.get("nextLink") if isinstance(payload, dict) else None
        if not next_link:
            return {"status": "ok", "items": items}
        url = next_link
        params = None


def _parse_subscription_usages(
    items: Iterable[Dict],
    *,
    family_canonical: Dict[str, str],
) -> Dict:
    families: Dict[str, Dict] = {}
    total_regional = None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name_value = _pick_usage_name(item)
        if not name_value:
            continue
        limit = _clean_num(item.get("limit"))
        usage = _clean_num(item.get("currentValue"))
        entry = {
            "limit": limit,
            "usage": usage,
            "headroom": _headroom(limit, usage),
        }
        name_lower = name_value.lower()
        if name_lower == "cores":
            total_regional = entry
        fam_canonical = family_canonical.get(name_lower)
        if fam_canonical:
            families[fam_canonical] = entry
    return {
        "families": families,
        "total_regional": total_regional,
    }


def _discovery_enabled() -> bool:
    """Management-group scanning is heavy and usually 403s for subscription-scoped
    tokens, so it's opt-in. Explicit AZURE_QUOTA_MGMT_GROUP_ID/NAME always work."""
    return str(os.getenv("AZURE_QUOTA_GROUP_DISCOVERY") or "").strip().lower() in ("1", "true", "yes", "on")


def _env_group_quota_targets() -> List[Tuple[str, str]]:
    """Explicit (managementGroupId, groupQuotaName) pairs from configuration.

    Discovery of a subscription's quota group requires management-group reads
    that a subscription-scoped token may not have. Operators can short-circuit
    discovery by setting AZURE_QUOTA_MGMT_GROUP_ID and AZURE_QUOTA_GROUP_NAME
    (comma-separated, positionally paired) so real group limits are used.
    """
    mgs = [s.strip() for s in str(os.getenv("AZURE_QUOTA_MGMT_GROUP_ID") or "").split(",") if s.strip()]
    groups = [s.strip() for s in str(os.getenv("AZURE_QUOTA_GROUP_NAME") or "").split(",") if s.strip()]
    if not mgs or not groups:
        return []
    if len(groups) == 1 and len(mgs) > 1:
        groups = groups * len(mgs)
    if len(mgs) == 1 and len(groups) > 1:
        mgs = mgs * len(groups)
    return list(zip(mgs, groups))


def _parse_group_quota_limits(payload, want_families: set) -> Dict[str, Dict]:
    """Parse a groupQuotaLimits/{location} response into per-family rows.

    Returns ``{family_lower: {family, limit, available, allocated, usage}}`` where
    ``usage = limit - available`` so downstream headroom math (limit - usage)
    yields the group's allocatable ``availableLimit``.
    """
    out: Dict[str, Dict] = {}
    if not isinstance(payload, dict):
        return out
    props = payload.get("properties") if isinstance(payload.get("properties"), dict) else payload
    values = props.get("value") if isinstance(props, dict) else None
    if not isinstance(values, list):
        return out
    for entry in values:
        if not isinstance(entry, dict):
            continue
        ep = entry.get("properties") if isinstance(entry.get("properties"), dict) else entry
        if not isinstance(ep, dict):
            continue
        name = ep.get("name")
        family = None
        if isinstance(name, dict):
            family = name.get("value") or name.get("localizedValue")
        family = family or ep.get("resourceName") or ep.get("family")
        family = _norm_family(family)
        if not family:
            continue
        if want_families and family not in want_families:
            continue
        limit = _clean_num(ep.get("limit"))
        available = _clean_num(ep.get("availableLimit"))
        allocated = None
        if limit is not None and available is not None:
            allocated = limit - available
        else:
            # Fall back to summing per-subscription allocations if present.
            alloc_block = ep.get("allocatedToSubscriptions")
            alloc_values = alloc_block.get("value") if isinstance(alloc_block, dict) else None
            if isinstance(alloc_values, list):
                total = 0.0
                seen = False
                for a in alloc_values:
                    if isinstance(a, dict):
                        q = _num(a.get("quotaAllocated"))
                        if q is not None:
                            total += q
                            seen = True
                if seen:
                    allocated = int(total) if float(total).is_integer() else total
        usage = allocated
        if usage is None and limit is not None and available is not None:
            usage = limit - available
        out[family] = {
            "family": ep.get("resourceName") or family,
            "limit": limit,
            "available": available,
            "allocated": allocated,
            "usage": usage,
        }
    return out


def _get_group_quota_limits(
    client: httpx.Client,
    *,
    management_group_id: str,
    group_quota_name: str,
    region: str,
    headers: Dict[str, str],
) -> Dict:
    """GET groupQuotaLimits for one (group, region); parse per-family limits."""
    url = (
        f"{ARM_BASE}/providers/Microsoft.Management/managementGroups/"
        f"{management_group_id}/providers/Microsoft.Quota/groupQuotas/"
        f"{group_quota_name}/resourceProviders/{GROUP_QUOTA_RP}/"
        f"groupQuotaLimits/{region}"
    )
    params = {"api-version": GROUP_QUOTA_API_VERSION}
    collected: Dict[str, Dict] = {}
    want_all: set = set()
    while True:
        resp = _get_with_retries(client, url, params=params, headers=headers)
        if resp.status_code in (401, 403):
            return {"status": "no_access", "families": {}}
        if resp.status_code == 404:
            return {"status": "no_quota_group", "families": {}}
        if resp.status_code >= 400:
            return {"status": "error", "families": {}, "error": f"HTTP {resp.status_code}"}
        try:
            payload = resp.json()
        except Exception as ex:
            return {"status": "error", "families": {}, "error": f"bad JSON: {ex!r}"}
        collected.update(_parse_group_quota_limits(payload, want_all))
        props = payload.get("properties") if isinstance(payload, dict) else None
        next_link = props.get("nextLink") if isinstance(props, dict) else None
        if not next_link:
            break
        url = next_link
        params = None
    return {"status": "ok", "families": collected}


def _discover_group_quota_targets(
    client: httpx.Client,
    *,
    subscription_id: str,
    headers: Dict[str, str],
) -> List[Tuple[str, str]]:
    """Best-effort: find (managementGroupId, groupQuotaName) pairs the given
    subscription belongs to. Requires management-group + Microsoft.Quota reads;
    returns [] on any missing permission (non-fatal)."""
    targets: List[Tuple[str, str]] = []
    # 1) Enumerate management groups the token can see.
    mg_url = f"{ARM_BASE}/providers/Microsoft.Management/managementGroups"
    try:
        resp = _get_with_retries(
            client, mg_url, params={"api-version": MGMT_GROUP_API_VERSION}, headers=headers
        )
    except Exception:
        return targets
    if resp.status_code >= 400:
        return targets
    try:
        mgs = [m.get("name") for m in (resp.json().get("value") or []) if isinstance(m, dict) and m.get("name")]
    except Exception:
        return targets
    sub_lower = str(subscription_id or "").lower()
    for mg in mgs[:MAX_MGMT_GROUPS_SCANNED]:
        # 2) List group quotas under this management group.
        gq_url = (
            f"{ARM_BASE}/providers/Microsoft.Management/managementGroups/{mg}"
            f"/providers/Microsoft.Quota/groupQuotas"
        )
        try:
            gq_resp = _get_with_retries(
                client, gq_url, params={"api-version": GROUP_QUOTA_API_VERSION}, headers=headers
            )
        except Exception:
            continue
        if gq_resp.status_code >= 400:
            continue
        try:
            groups = [g for g in (gq_resp.json().get("value") or []) if isinstance(g, dict)]
        except Exception:
            continue
        for g in groups:
            gname = g.get("name") or (g.get("properties") or {}).get("displayName")
            if not gname:
                continue
            # 3) Confirm the subscription is a member of this group quota.
            if _subscription_in_group(client, mg, gname, sub_lower, headers):
                targets.append((mg, gname))
    return targets


def _subscription_in_group(
    client: httpx.Client,
    management_group_id: str,
    group_quota_name: str,
    subscription_id_lower: str,
    headers: Dict[str, str],
) -> bool:
    url = (
        f"{ARM_BASE}/providers/Microsoft.Management/managementGroups/"
        f"{management_group_id}/providers/Microsoft.Quota/groupQuotas/"
        f"{group_quota_name}/subscriptions"
    )
    try:
        resp = _get_with_retries(
            client, url, params={"api-version": GROUP_QUOTA_API_VERSION}, headers=headers
        )
    except Exception:
        return False
    if resp.status_code >= 400:
        return False
    try:
        members = resp.json().get("value") or []
    except Exception:
        return False
    for m in members:
        if not isinstance(m, dict):
            continue
        candidate = (
            m.get("name")
            or (m.get("properties") or {}).get("subscriptionId")
            or str(m.get("id") or "").rstrip("/").split("/")[-1]
        )
        if str(candidate or "").lower() == subscription_id_lower:
            return True
    return False


def _group_quotas_via_limits(
    client: httpx.Client,
    *,
    subscription_id: str,
    targets: List[Tuple[str, str]],
    want_regions: set,
    want_families: set,
    headers: Dict[str, str],
) -> List[Dict]:
    """Build the ``groups`` list from real groupQuotaLimits for each target."""
    parsed_groups: List[Dict] = []
    for mg, group in targets:
        for region in sorted(want_regions):
            limits = _get_group_quota_limits(
                client, management_group_id=mg, group_quota_name=group,
                region=region, headers=headers,
            )
            if limits.get("status") != "ok":
                continue
            fam_rows: List[Dict] = []
            for fam_lower, info in (limits.get("families") or {}).items():
                if want_families and fam_lower not in want_families:
                    continue
                fam_rows.append({
                    "family": info.get("family") or fam_lower,
                    "limit": info.get("limit"),
                    "usage": info.get("usage"),
                    "available": info.get("available"),
                })
            if fam_rows:
                parsed_groups.append({
                    "name": group,
                    "management_group_id": mg,
                    "region": region,
                    "families": fam_rows,
                })
    return parsed_groups


def check_quota_groups(
    arm_token,
    subscription_id: str,
    regions: Iterable[str],
    families: Iterable[str],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    management_group_targets: Optional[List[Tuple[str, str]]] = None,
) -> Dict:
    """Return best-effort quota group information for a subscription.

    Group quotas are management-group scoped: the real per-family limits live in
    the ``groupQuotaLimits/{location}`` sub-resource. We resolve the
    subscription's (managementGroup, groupQuota) targets from configuration
    (AZURE_QUOTA_MGMT_GROUP_ID / AZURE_QUOTA_GROUP_NAME) or best-effort
    discovery, fetch real limits, and expose the group's allocatable headroom
    (``availableLimit``) so BOM tier-2 logic can cover a shortfall from the
    group. If no real targets resolve, we fall back to the legacy
    subscription-scoped attempt.

    404/empty means the subscription has no quota groups. 401/403 are treated
    as non-fatal "no access" results.
    """
    clean = _strip_bearer(arm_token)
    want_regions = {_norm_region(r) for r in regions if _norm_region(r)}
    want_families = {_norm_family(f) for f in families if _norm_family(f)}
    result = {
        "subscription_id": subscription_id,
        "has_quota_groups": False,
        "groups": [],
    }
    if not clean:
        result["status"] = "no_access"
        result["error"] = "ARM token unavailable for quota group lookup."
        return result

    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/providers/Microsoft.Quota/groupQuotas?api-version={API_VERSION}"
    )
    headers = {
        "authorization": f"Bearer {clean}",
        "accept": "application/json",
        "user-agent": "azure-bom-region-dashboard/1.0",
    }
    # ── Real group limits via management-group scope (config or discovery) ──
    targets = list(management_group_targets or []) or _env_group_quota_targets()
    if want_regions:
        try:
            with httpx.Client(timeout=timeout_s, http2=False) as _client:
                if not targets:
                    if _discovery_enabled():
                        try:
                            targets = _discover_group_quota_targets(
                                _client, subscription_id=subscription_id, headers=headers
                            )
                        except Exception as ex:
                            log.debug("quota group discovery failed: %r", ex)
                            targets = []
                if targets:
                    parsed_real = _group_quotas_via_limits(
                        _client,
                        subscription_id=subscription_id,
                        targets=targets,
                        want_regions=want_regions,
                        want_families=want_families,
                        headers=headers,
                    )
                    if parsed_real:
                        result["has_quota_groups"] = True
                        result["groups"] = parsed_real
                        result["status"] = "ok"
                        result["targets"] = [
                            {"management_group_id": mg, "group": g} for mg, g in targets
                        ]
                        return result
        except Exception as ex:
            log.debug("group quota limits path failed: %r", ex)

    # ── Legacy subscription-scoped fallback (metadata only) ─────────────────
    try:
        with httpx.Client(timeout=timeout_s, http2=False) as client:
            resp = client.get(url, headers=headers)
    except Exception as ex:
        result["status"] = "error"
        result["error"] = f"Quota Groups call failed: {ex!r}"
        return result

    if resp.status_code == 404:
        result["status"] = "no_quota_group"
        return result
    if resp.status_code in (401, 403):
        result["status"] = "no_access"
        result["error"] = f"Quota Groups call returned {resp.status_code}."
        return result
    if resp.status_code == 400:
        # Common case: Microsoft.Quota provider not registered or groupQuotas
        # not available for this subscription.
        result["status"] = "not_available"
        result["error"] = "Quota Groups not available for this subscription."
        return result
    if resp.status_code >= 400:
        result["status"] = "error"
        result["error"] = f"Quota Groups call returned {resp.status_code}."
        return result

    try:
        payload = resp.json()
    except Exception as ex:
        result["status"] = "error"
        result["error"] = f"Quota Groups response was not valid JSON: {ex!r}"
        return result

    groups_raw = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(groups_raw, list):
        groups_raw = []

    parsed_groups: List[Dict] = []
    for item in groups_raw:
        if not isinstance(item, dict):
            continue
        group_name = (
            item.get("name")
            or item.get("displayName")
            or str(item.get("id") or "").rstrip("/").split("/")[-1]
            or "(unnamed)"
        )
        rows: List[Dict] = []
        props = item.get("properties") if isinstance(item.get("properties"), dict) else item
        _walk_family_rows(props, region_hint=None, out=rows)
        if want_regions:
            rows = [r for r in rows if r.get("region") in want_regions]
        if want_families:
            rows = [r for r in rows if _norm_family(r.get("family")) in want_families]
        by_region: Dict[str, List[Dict]] = {}
        for row in rows:
            by_region.setdefault(row["region"], []).append({
                "family": row["family"],
                "limit": row.get("limit"),
                "usage": row.get("usage"),
            })
        for region, fam_rows in sorted(by_region.items()):
            parsed_groups.append({
                "name": group_name,
                "region": region,
                "families": fam_rows,
            })

    result["has_quota_groups"] = bool(parsed_groups)
    result["groups"] = parsed_groups
    result["status"] = "ok" if parsed_groups else "no_quota_group"
    return result


def check_subscription_quota(
    arm_token,
    subscription_id: str,
    regions: Iterable[str],
    families: Iterable[str],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Dict:
    """Query Microsoft.Compute/locations/{region}/usages for vCPU quota."""
    clean = _strip_bearer(arm_token)
    want_regions = sorted({_norm_region(r) for r in regions if _norm_region(r)})
    family_canonical = {
        _norm_family(f): str(f).strip()
        for f in families
        if _norm_family(f)
    }
    result = {
        "subscription_id": subscription_id,
        "status": "ok",
        "regions": {},
    }
    if not clean:
        result["status"] = "no_access"
        result["error"] = "ARM token unavailable for subscription quota lookup."
        return result
    if not want_regions:
        return result

    headers = {
        "authorization": f"Bearer {clean}",
        "accept": "application/json",
        "user-agent": "azure-bom-region-dashboard/1.0",
    }
    region_results: Dict[str, Dict] = {}
    any_ok = False
    any_no_access = False
    any_error = False
    max_workers = min(MAX_PARALLEL_REGIONS, max(1, len(want_regions)))
    with httpx.Client(timeout=timeout_s, http2=False) as client:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    _list_subscription_usages_for_region,
                    client,
                    subscription_id=subscription_id,
                    region=region,
                    headers=headers,
                ): region
                for region in want_regions
            }
            for fut in as_completed(futures):
                region = futures[fut]
                try:
                    region_info = fut.result()
                except Exception as ex:
                    log.exception(
                        "subscription quota call failed unexpectedly for %s/%s",
                        subscription_id,
                        region,
                    )
                    region_info = {
                        "status": "error",
                        "error": f"Subscription quota call failed: {ex!r}",
                    }
                parsed = {
                    "status": region_info.get("status") or "error",
                    "families": {},
                    "total_regional": None,
                }
                if region_info.get("status") == "ok":
                    any_ok = True
                    parsed.update(
                        _parse_subscription_usages(
                            region_info.get("items") or [],
                            family_canonical=family_canonical,
                        )
                    )
                elif region_info.get("status") == "no_access":
                    any_no_access = True
                else:
                    any_error = True
                if region_info.get("error"):
                    parsed["error"] = region_info["error"]
                region_results[region] = parsed

    if any_ok:
        result["status"] = "ok"
    elif any_no_access and not any_error:
        result["status"] = "no_access"
        result["error"] = "Subscription quota lookup returned no-access for all regions."
    elif any_error:
        result["status"] = "error"
        result["error"] = "Subscription quota lookup failed for all regions."
    result["regions"] = {
        region: region_results[region]
        for region in sorted(region_results)
    }
    return result

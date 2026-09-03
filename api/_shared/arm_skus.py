"""
Per-subscription SKU restrictions overlay using the ARM
`Microsoft.Compute/skus` endpoint.

The global availability feed returns segment-level restrictions
(EA / ANY / MOSP). It does NOT know which subscription is asking, so two
different subs in the same segment get identical results.

This module fills that gap: for the *specific* subscription chosen in the
Run modal, we ask ARM what restrictions Azure has actually applied to that
sub (e.g. "this sub doesn't have quota for Edsv6 in WestUS3" or
"NotAvailableForSubscription in zone 1"). We then overlay those on top of
the availability rows so the dashboard reflects what *that customer* will
actually see.

Auth: caller passes a bearer token issued for
`https://management.azure.com/.default` audience for the subscription's
home tenant. In LOCAL_MODE the launcher mints this via
`az account get-access-token --subscription <sub-id>`.
"""
from __future__ import annotations

import logging
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, Iterable, List, Optional, Tuple

import httpx

log = logging.getLogger(__name__)

ARM_BASE = "https://management.azure.com"
ARM_API_VERSION = "2024-07-01"
DEFAULT_TIMEOUT_S = 60.0
MAX_PARALLEL_REGIONS = 8
MAX_RETRY_ATTEMPTS = 4


class ArmError(Exception):
    """Stable error code we hand back to runs_post for nice UI messages."""

    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _strip_bearer(token: str) -> str:
    t = (token or "").strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return t


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


def _list_skus_for_region(
    client: httpx.Client,
    *,
    subscription_id: str,
    region: str,
    headers: Dict[str, str],
) -> List[dict]:
    """Page through all VM SKUs for one region. Returns the raw `value` list."""
    all_items: List[dict] = []
    url = (
        f"{ARM_BASE}/subscriptions/{subscription_id}/providers/"
        f"Microsoft.Compute/skus"
    )
    params = {
        "api-version": ARM_API_VERSION,
        "$filter": f"location eq '{region}'",
    }
    while True:
        resp = _get_with_retries(client, url, params=params, headers=headers)
        if resp.status_code == 401:
            raise ArmError("arm_token_expired",
                           "ARM rejected the token (401). Re-mint and retry.", 401)
        if resp.status_code == 403:
            raise ArmError("arm_forbidden",
                           f"ARM returned 403 for {subscription_id} in {region}. "
                           "Your account may not have read access on this subscription.",
                           403)
        if resp.status_code == 404:
            # Some regions are unknown to a sub (e.g. the sub isn't enabled
            # for that geography). Treat as empty SKU list — the model will
            # render the region as "no data" naturally.
            return []
        if resp.status_code >= 400:
            raise ArmError("arm_upstream_error",
                           f"ARM Microsoft.Compute/skus returned {resp.status_code} "
                           f"for {region}: {resp.text[:300]}",
                           502)
        body = resp.json()
        all_items.extend(body.get("value") or [])
        next_link = body.get("nextLink")
        if not next_link:
            return all_items
        url = next_link
        params = None  # nextLink already includes everything


def _normalize_region(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "")).lower()


def _aggregate_region(
    items: List[dict],
    *,
    family_canonical: Dict[str, str],
) -> Dict[str, dict]:
    """
    Reduce many per-SKU records (each VM size is a separate SKU) down to one
    record per family that says: is the WHOLE family region-restricted, and
    which zones are restricted across all of its SKUs?
    """
    by_family: Dict[str, dict] = {}
    for sku in items:
        if sku.get("resourceType") != "virtualMachines":
            continue
        family = sku.get("family") or ""
        fam_lower = family.lower()
        if fam_lower not in family_canonical:
            continue
        fam_canonical = family_canonical[fam_lower]

        # Initialize aggregator
        agg = by_family.setdefault(fam_canonical, {
            "family": fam_canonical,
            "region_restricted_all": True,        # AND across SKUs
            "available_zones": set(),
            "restricted_zones": set(),
            "raw_restriction_notes": [],
        })

        sku_region_restricted = False
        sku_zone_restricted: set = set()

        for r in sku.get("restrictions") or []:
            r_type = r.get("type") or ""
            r_reason = r.get("reasonCode") or ""
            if r_type == "Location" and r_reason == "NotAvailableForSubscription":
                sku_region_restricted = True
                agg["raw_restriction_notes"].append(
                    f"{sku.get('name')}: NotAvailableForSubscription"
                )
            elif r_type == "Zone" and r_reason == "NotAvailableForSubscription":
                info = r.get("restrictionInfo") or {}
                for z in info.get("zones") or []:
                    sku_zone_restricted.add(str(z))
                    agg["raw_restriction_notes"].append(
                        f"{sku.get('name')}: zone {z} NotAvailableForSubscription"
                    )

        if not sku_region_restricted:
            # At least one SKU in this family is allowed in the region
            agg["region_restricted_all"] = False
            for li in sku.get("locationInfo") or []:
                for z in li.get("zones") or []:
                    if str(z) not in sku_zone_restricted:
                        agg["available_zones"].add(str(z))

        # If THIS sku had zone restrictions but the region IS available for it,
        # those zones still count as restricted in the aggregate IF no sibling
        # SKU exposed them as available. We track them; final reconciliation
        # below subtracts available_zones from restricted_zones.
        for z in sku_zone_restricted:
            agg["restricted_zones"].add(str(z))

    # Final reconciliation: a zone is restricted only if NO sibling SKU
    # advertises it as available.
    for fam, agg in by_family.items():
        agg["restricted_zones"] = agg["restricted_zones"] - agg["available_zones"]

    return by_family


def fetch_arm_restrictions(
    *,
    arm_token: str,
    subscription_id: str,
    regions: Iterable[str],
    families: Iterable[str],
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_parallel: int = MAX_PARALLEL_REGIONS,
) -> Dict[Tuple[str, str], dict]:
    """
    Returns a dict keyed by (region_short_lower, family_canonical_lower) with
    per-(region, family) restriction state for the chosen subscription.

    Each value:
      {
        "family": <canonical>,
        "region_restricted_all": bool,   # ALL SKUs in family blocked in this region
        "available_zones": set[str],
        "restricted_zones": set[str],
        "raw_restriction_notes": [str, ...],
      }

    Raises ArmError on auth / config / upstream failure. A single bad region
    fails the whole call (rather than silently masking sub restrictions).
    """
    clean = _strip_bearer(arm_token)
    if not clean:
        raise ArmError("arm_missing_token", "ARM bearer token is required.", 400)
    if not subscription_id:
        raise ArmError("arm_missing_sub", "subscription_id is required.", 400)

    region_list = [r.strip() for r in regions if r and r.strip()]
    if not region_list:
        raise ArmError("arm_no_regions", "No regions requested.", 400)

    family_canonical: Dict[str, str] = {}
    for f in families:
        if not f or not f.strip():
            continue
        family_canonical.setdefault(f.strip().lower(), f.strip())
    if not family_canonical:
        raise ArmError("arm_no_families", "No SKU families requested.", 400)

    headers = {
        "authorization": f"Bearer {clean}",
        "accept": "application/json",
        "user-agent": "azure-bom-region-dashboard/1.0",
    }

    # Use a single Client for connection pooling; thread workers share it
    # safely (httpx.Client is thread-safe for concurrent requests).
    out: Dict[Tuple[str, str], dict] = {}
    errors: List[Exception] = []

    with httpx.Client(timeout=timeout_s, http2=False) as client:
        with ThreadPoolExecutor(max_workers=max_parallel) as ex:
            futures = {
                ex.submit(_list_skus_for_region,
                          client,
                          subscription_id=subscription_id,
                          region=r,
                          headers=headers): r
                for r in region_list
            }
            for fut in as_completed(futures):
                region = futures[fut]
                region_short = _normalize_region(region)
                try:
                    items = fut.result()
                except ArmError as ex_:
                    errors.append(ex_)
                    continue
                except Exception as ex_:
                    # Preserve the traceback so a code/schema bug here is
                    # diagnosable rather than an opaque "arm_call_failed".
                    log.exception("ARM call for %s raised an unexpected error", region)
                    errors.append(ArmError("arm_call_failed",
                                           f"ARM call for {region} failed: {ex_!r}",
                                           502))
                    continue
                by_family = _aggregate_region(items, family_canonical=family_canonical)
                for fam_canonical, agg in by_family.items():
                    out[(region_short, fam_canonical.lower())] = agg

                # Make sure missing (region, family) combos still get a row
                # so the overlay can mark them as "ARM had no SKU here".
                seen_fams = {f.lower() for f in by_family.keys()}
                for fam_canonical in family_canonical.values():
                    if fam_canonical.lower() in seen_fams:
                        continue
                    out[(region_short, fam_canonical.lower())] = {
                        "family": fam_canonical,
                        "region_restricted_all": True,
                        "available_zones": set(),
                        "restricted_zones": set(),
                        "raw_restriction_notes": ["ARM returned no SKU rows for this family in this region"],
                    }

    if errors:
        # Surface the first error; details from the rest go in the message.
        first = errors[0]
        extra = ""
        if len(errors) > 1:
            extra = f" (+{len(errors) - 1} more region failures)"
        raise ArmError(first.code, first.message + extra, first.status)

    log.info("ARM overlay: %d (region, family) tuples for sub=%s",
             len(out), subscription_id)
    return out


def fetch_region_capabilities(
    *,
    arm_token: str,
    subscription_id: str,
    region: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Dict[str, Dict[str, str]]:
    """Return ``{armSkuName_lower: {capability_name: value}}`` for a region.

    A single ``Microsoft.Compute/skus`` call returns every VM size in the region
    along with its ``capabilities`` (temp disk size, PremiumIO, accelerated
    networking, encryption-at-host, HyperV generations, memory, vCPUs, ...). We
    use this to verify a recommended size-equivalent actually supports every
    capability the original BOM size does.
    """
    clean = _strip_bearer(arm_token)
    if not clean:
        raise ArmError("arm_missing_token", "ARM bearer token is required.", 400)
    if not subscription_id:
        raise ArmError("arm_missing_sub", "subscription_id is required.", 400)
    if not region or not str(region).strip():
        raise ArmError("arm_no_regions", "No region requested.", 400)

    headers = {
        "authorization": f"Bearer {clean}",
        "accept": "application/json",
        "user-agent": "azure-bom-region-dashboard/1.0",
    }

    out: Dict[str, Dict[str, str]] = {}
    with httpx.Client(timeout=timeout_s, http2=False) as client:
        items = _list_skus_for_region(
            client,
            subscription_id=subscription_id,
            region=str(region).strip(),
            headers=headers,
        )
    for sku in items:
        if sku.get("resourceType") != "virtualMachines":
            continue
        name = str(sku.get("name") or "").strip().lower()
        if not name:
            continue
        caps: Dict[str, str] = {}
        for c in sku.get("capabilities") or []:
            cap_name = c.get("name")
            if cap_name is not None:
                caps[str(cap_name)] = c.get("value")
        out[name] = caps
    return out


def overlay_onto_availability_rows(
    *,
    availability_rows: List[dict],
    arm_overlay: Dict[Tuple[str, str], dict],
) -> List[dict]:
    """
    Mutates each availability row in-place to add per-sub restrictions on top
    of the segment-level ones. Returns the same list for convenience.

    Rules:
      - If ARM says region_restricted_all=True, the row becomes
        sub_restricted=True with reason "Region: NotAvailableForSubscription (ARM)".
      - Otherwise, any zone in restricted_zones gets force-flipped to False.
      - We add an `arm_provenance` block to every row so the UI can show
        "this verdict came from the availability feed + ARM" or "ARM had no data".
    """
    for row in availability_rows:
        region_lower = (row.get("region") or "").lower()
        family_lower = (row.get("family") or "").lower()
        key = (region_lower, family_lower)
        arm = arm_overlay.get(key)
        if arm is None:
            row["arm_provenance"] = {"available": False, "reason": "no_arm_data"}
            continue

        notes = list(arm.get("raw_restriction_notes") or [])
        if arm.get("region_restricted_all"):
            row["sub_restricted"] = True
            row["sub_restriction_raw"] = "Region: NotAvailableForSubscription (ARM)"
            row["zones"] = [False, False, False]
        else:
            restricted = arm.get("restricted_zones") or set()
            if restricted:
                zones = list(row.get("zones") or [True, True, True])
                for z in restricted:
                    try:
                        idx = int(z) - 1
                        if 0 <= idx < len(zones):
                            zones[idx] = False
                    except (TypeError, ValueError):
                        continue
                row["zones"] = zones
                # Update the human-readable reason to reflect the merged view
                blocked = [str(i + 1) for i, ok in enumerate(zones) if not ok]
                if blocked:
                    label = "Zone" if len(blocked) == 1 else "Zones"
                    row["sub_restricted"] = True
                    row["sub_restriction_raw"] = (
                        f"Restricted in {label} {', '.join(blocked)} (ARM)"
                    )

        row["arm_provenance"] = {
            "available": True,
            "region_restricted": bool(arm.get("region_restricted_all")),
            "restricted_zones": sorted(arm.get("restricted_zones") or []),
            "notes": notes[:8],
        }
    return availability_rows

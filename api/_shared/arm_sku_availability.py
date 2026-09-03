"""ARM-backed VM SKU availability for the dashboard.

This replaces the old global availability feed by querying
``Microsoft.Compute/skus`` directly for the chosen subscription/regions/families
and projecting the results into the same row shape pipeline.model expects.
"""
from __future__ import annotations

import os
import re
from typing import Dict, Iterable, List

from . import arm_skus

DEFAULT_TIMEOUT_S = arm_skus.DEFAULT_TIMEOUT_S
MAX_PARALLEL_REGIONS = arm_skus.MAX_PARALLEL_REGIONS


def _friendly_family(family_id: str) -> str:
    m = re.match(r"^standard(.+)Family$", family_id, re.IGNORECASE)
    return f"{m.group(1)} Series" if m else family_id


def _format_sub_restriction(region_restricted: bool, zones: List[bool], notes: List[str]) -> str:
    if region_restricted:
        return "Region: NotAvailableForSubscription"
    blocked = [str(i) for i, ok in enumerate(zones, start=1) if not ok]
    if blocked and notes:
        label = "Zone" if len(blocked) == 1 else "Zones"
        return f"Restricted in {label} {', '.join(blocked)}"
    if notes:
        return notes[0]
    return "Available"


def fetch_arm_sku_records(
    *,
    arm_token: str,
    subscription_id: str,
    want_regions: Iterable[str],
    want_families: Iterable[str],
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_parallel: int = MAX_PARALLEL_REGIONS,
) -> List[Dict]:
    """Return rows matching the availability output schema."""
    overlay = arm_skus.fetch_arm_restrictions(
        arm_token=arm_token,
        subscription_id=subscription_id,
        regions=want_regions,
        families=want_families,
        timeout_s=timeout_s,
        max_parallel=max_parallel,
    )

    want_regions_set = {
        arm_skus._normalize_region(r) for r in want_regions if r and str(r).strip()  # noqa: SLF001
    }
    family_canonical: Dict[str, str] = {}
    for family in want_families:
        if family and str(family).strip():
            family_canonical.setdefault(str(family).strip().lower(), str(family).strip())

    rows: List[Dict] = []
    for region in sorted(want_regions_set):
        for fam_lower, fam_canonical in sorted(family_canonical.items()):
            agg = overlay.get((region, fam_lower))
            if not agg:
                zones = [False, False, False]
                rows.append({
                    "region": region,
                    "family": fam_canonical,
                    "display": "",
                    "zones": zones,
                    "sub_restricted": True,
                    "sub_restriction_raw": "SKU not in region",
                })
                continue

            available_zones = {str(z) for z in (agg.get("available_zones") or set())}
            zones = [str(i) in available_zones for i in range(1, 4)]
            region_restricted = bool(agg.get("region_restricted_all"))
            notes = list(agg.get("raw_restriction_notes") or [])
            is_placeholder = (
                region_restricted
                and not available_zones
                and not (agg.get("restricted_zones") or set())
                and notes == ["ARM returned no SKU rows for this family in this region"]
            )

            if is_placeholder:
                rows.append({
                    "region": region,
                    "family": fam_canonical,
                    "display": "",
                    "zones": [False, False, False],
                    "sub_restricted": True,
                    "sub_restriction_raw": "SKU not in region",
                })
                continue

            if region_restricted:
                zones = [False, False, False]

            reason = _format_sub_restriction(region_restricted, zones, notes)
            rows.append({
                "region": region,
                "family": fam_canonical,
                "display": _friendly_family(fam_canonical),
                "zones": zones,
                "sub_restricted": reason.lower() != "available",
                "sub_restriction_raw": reason,
                "arm_provenance": {
                    "available": True,
                    "region_restricted": region_restricted,
                    "restricted_zones": sorted(agg.get("restricted_zones") or []),
                    "available_zones": sorted(available_zones),
                    "notes": notes[:8],
                },
            })

    return rows


def load_default_regions(data_dir: str) -> List[str]:
    from . import dataset_store
    path = dataset_store.resolve_path("regions_list")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    out = []
    for chunk in re.split(r"[,\r\n]+", text):
        region = chunk.strip().lower()
        if region and region not in out:
            out.append(region)
    return out


def load_default_families(data_dir: str) -> List[str]:
    from . import dataset_store
    path = dataset_store.resolve_path("skus_list")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    out = []
    seen = set()
    for chunk in re.split(r"[,\r\n]+", text):
        family = chunk.strip()
        if not family:
            continue
        key = family.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(family)
    return out

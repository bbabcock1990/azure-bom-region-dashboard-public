"""GET /api/bom/sensitivity?bom_id=<bom_id>[&run_id=<run_id>]"""
from __future__ import annotations

import json
import re
from typing import Dict, List

from .._shared import auth, bom_storage
from .._shared import httpfunc as func
from .._shared import snapshot_store


def _err(code: str, message: str, status: int = 400, **extra) -> func.HttpResponse:
    payload = {"error": code, "message": message}
    payload.update(extra)
    return func.HttpResponse(
        json.dumps(payload),
        status_code=status,
        mimetype="application/json",
    )


def _constraint_id(prefix: str, value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug or prefix


def _impact(count: int, total: int) -> str:
    if total <= 0 or count <= 0:
        return "low"
    ratio = count / total
    if count >= 10 or ratio >= 0.2:
        return "high"
    if count >= 4 or ratio >= 0.08:
        return "medium"
    return "low"


def _region_lookup(snapshot: Dict) -> Dict[str, Dict]:
    regions = {}
    for region in (snapshot or {}).get("regions") or []:
        short = str((region or {}).get("short") or "").strip().lower()
        if short:
            regions[short] = region
    return regions


def _family_pass(region: Dict, requirement: Dict) -> bool:
    zone_detail = region.get("sku_zone_detail") or {}
    primary = list(zone_detail.get(requirement.get("primary_label")) or [])
    alt = list(zone_detail.get(requirement.get("alt_label")) or [])
    return (len(primary) >= 3 and all(primary[:3])) or (len(alt) >= 3 and all(alt[:3]))


def _family_partial(region: Dict, requirement: Dict) -> bool:
    zone_detail = region.get("sku_zone_detail") or {}
    primary = list(zone_detail.get(requirement.get("primary_label")) or [])
    alt = list(zone_detail.get(requirement.get("alt_label")) or [])
    primary_any = any(bool(v) for v in primary)
    alt_any = any(bool(v) for v in alt)
    primary_full = len(primary) >= 3 and all(primary[:3])
    alt_full = len(alt) >= 3 and all(alt[:3])
    return (primary_any or alt_any) and not (primary_full or alt_full)


def _service_names(region: Dict) -> List[str]:
    out = []
    for item in region.get("missing_services") or []:
        name = str((item or {}).get("service") or "").strip()
        if name:
            out.append(name)
    return out


def _has_hard_quota_failure(region: Dict) -> bool:
    return str(region.get("quota_status") or "").strip().lower() == "insufficient"


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)

    bom_id = str(req.params.get("bom_id") or "").strip().lower()
    run_id = str(req.params.get("run_id") or "").strip()
    if not bom_id:
        return _err("missing_bom_id", "Query param 'bom_id' is required.")

    bom = bom_storage.get(bom_id)
    if not bom:
        return _err("not_found", f"No BOM with id {bom_id}.", 404, bom_id=bom_id)

    if run_id:
        run_entity, snapshot = snapshot_store.load_snapshot_by_run_id(run_id)
        if not run_entity or not snapshot:
            return _err("not_found", f"Snapshot {run_id} was not found.", 404, run_id=run_id)
        if str(run_entity.get("PartitionKey") or "").strip().lower() != bom_id:
            return _err("snapshot_scope_mismatch", "Snapshot does not belong to this BOM.", 400)
    else:
        run_entity, snapshot = snapshot_store.load_latest_snapshot_for_bom(bom_id)
        if not run_entity or not snapshot:
            return _err("no_snapshots", "No snapshots exist for this BOM yet.", 404, bom_id=bom_id)

    regions = _region_lookup(snapshot)
    total_regions = len(regions)
    required_services = [
        str((item or {}).get("name") or "").strip()
        for item in bom.get("services") or []
        if str((item or {}).get("name") or "").strip()
    ]
    required_skus = []
    for item in bom.get("required_skus") or []:
        primary_label = str((item or {}).get("primary_label") or (item or {}).get("primary_family") or "").strip()
        alt_label = str((item or {}).get("alt_label") or (item or {}).get("alt_family") or "").strip()
        required_skus.append(
            {
                "id": str((item or {}).get("primary_family") or primary_label or "").strip(),
                "name": primary_label or str((item or {}).get("primary_family") or "").strip(),
                "primary_label": primary_label,
                "alt_label": alt_label,
            }
        )

    constraints = []

    for service in required_services:
        excluded = []
        for key, region in regions.items():
            missing = _service_names(region)
            if service not in missing:
                continue
            if set(missing) != {service}:
                continue
            if region.get("sku_blockers"):
                continue
            if _has_hard_quota_failure(region):
                continue
            excluded.append(key)
        constraints.append(
            {
                "type": "service",
                "name": service,
                "id": _constraint_id("service", service),
                "regions_excluded": len(excluded),
                "excluded_regions": sorted(excluded),
                "impact": _impact(len(excluded), total_regions),
            }
        )

    for requirement in required_skus:
        excluded = []
        for key, region in regions.items():
            if _service_names(region):
                continue
            if _has_hard_quota_failure(region):
                continue
            if _family_pass(region, requirement):
                continue
            others_pass = all(
                _family_pass(region, other)
                for other in required_skus
                if other["id"] != requirement["id"]
            )
            if others_pass:
                excluded.append(key)
        constraints.append(
            {
                "type": "sku_family",
                "name": requirement["name"],
                "id": requirement["id"],
                "regions_excluded": len(excluded),
                "excluded_regions": sorted(excluded),
                "impact": _impact(len(excluded), total_regions),
            }
        )

    zone_excluded = []
    for key, region in regions.items():
        if _service_names(region):
            continue
        if _has_hard_quota_failure(region):
            continue
        partial_hit = any(_family_partial(region, requirement) for requirement in required_skus)
        other_full = all(
            _family_pass(region, requirement) or _family_partial(region, requirement)
            for requirement in required_skus
        )
        if partial_hit and other_full:
            zone_excluded.append(key)
    constraints.append(
        {
            "type": "zone_requirement",
            "name": "3-zone availability",
            "id": "3-zone-availability",
            "regions_excluded": len(zone_excluded),
            "excluded_regions": sorted(zone_excluded),
            "impact": _impact(len(zone_excluded), total_regions),
        }
    )

    constraints.sort(
        key=lambda item: (
            -int(item.get("regions_excluded") or 0),
            str(item.get("type") or ""),
            str(item.get("name") or "").lower(),
        )
    )
    return func.HttpResponse(
        json.dumps({"constraints": constraints}, ensure_ascii=False),
        status_code=200,
        mimetype="application/json",
    )

"""GET /api/snapshots/diff?a=<run_id>&b=<run_id>

Compare two persisted snapshots and summarize what changed by region.
"""
from __future__ import annotations

import json
from typing import Dict, Iterable, List, Tuple

from .._shared import auth
from .._shared import httpfunc as func
from .._shared import snapshot_store

_VERDICT_RANK = {
    "unknown": -1,
    "not_recommended": 0,
    "ready_with_constraints": 1,
    "ready": 2,
}


def _err(code: str, message: str, status: int = 400, **extra) -> func.HttpResponse:
    payload = {"error": code, "message": message}
    payload.update(extra)
    return func.HttpResponse(
        json.dumps(payload),
        status_code=status,
        mimetype="application/json",
    )


def _region_index(snapshot: Dict) -> Dict[str, Dict]:
    out = {}
    for region in (snapshot or {}).get("regions") or []:
        short = str((region or {}).get("short") or "").strip().lower()
        if short:
            out[short] = region
    return out


def _quota_label(status: str) -> str:
    lookup = {
        "sufficient": "sufficient",
        "partial": "partial",
        "insufficient": "insufficient",
        "no_quota_group": "no_quota_group",
        "unknown": "unknown",
    }
    return lookup.get(str(status or "").strip().lower(), "unknown")


def _region_verdict(region: Dict) -> str:
    healthy = str(region.get("deployment_health") or "").strip().lower() == "yes"
    if not healthy and str(region.get("status") or "").strip() == "OK":
        healthy = True
    if not healthy:
        return "not_recommended"
    has_constraints = any(
        [
            bool(region.get("fell_back")),
            bool(region.get("has_zone_restriction")),
            bool(region.get("sku_fallbacks")),
            _quota_label(region.get("quota_status")) not in {"sufficient", "unknown"},
        ]
    )
    return "ready_with_constraints" if has_constraints else "ready"


def _region_score(region: Dict) -> int:
    score = 100
    verdict = _region_verdict(region)
    if verdict == "ready_with_constraints":
        score -= 12
    elif verdict == "not_recommended":
        score -= 45

    score -= min(24, len(region.get("missing_services") or []) * 12)
    score -= min(24, len(region.get("sku_blockers") or []) * 12)
    score -= min(12, len(region.get("sku_fallbacks") or []) * 6)
    if region.get("has_zone_restriction"):
        score -= 6

    quota_status = _quota_label(region.get("quota_status"))
    if quota_status == "partial":
        score -= 8
    elif quota_status == "insufficient":
        score -= 14
    elif quota_status == "no_quota_group":
        score -= 4
    return max(0, min(100, score))


def _service_blockers(region: Dict) -> List[str]:
    blockers = []
    for item in region.get("missing_services") or []:
        service = str((item or {}).get("service") or "").strip()
        detail = str((item or {}).get("detail") or "").strip()
        if service:
            blockers.append(f"service:{service}:{detail}")
    return blockers


def _hard_blockers(region: Dict) -> List[str]:
    blockers = list(_service_blockers(region))
    blockers.extend([f"sku:{item}" for item in (region.get("sku_blockers") or []) if item])
    quota_status = _quota_label(region.get("quota_status"))
    if quota_status == "insufficient":
        blockers.append("quota:insufficient")
    if _region_verdict(region) == "not_recommended" and not blockers:
        blockers.append(f"status:{region.get('status') or 'not_recommended'}")
    return sorted(set(blockers))


def _label_map(*snapshots: Dict) -> Dict[str, str]:
    out = {}
    for snapshot in snapshots:
        for item in ((snapshot or {}).get("meta") or {}).get("skus_resolved") or []:
            primary_family = str((item or {}).get("primary_family") or "").strip()
            alt_family = str((item or {}).get("alt_family") or "").strip()
            if primary_family:
                out[primary_family] = str((item or {}).get("primary_label") or primary_family)
            if alt_family:
                out[alt_family] = str((item or {}).get("alt_label") or alt_family)
    return out


def _zone_changes(before: Iterable[bool], after: Iterable[bool]) -> Tuple[List[int], List[int]]:
    before_list = list(before or [])
    after_list = list(after or [])
    gained = []
    lost = []
    for idx in range(max(len(before_list), len(after_list))):
        b = bool(before_list[idx]) if idx < len(before_list) else False
        a = bool(after_list[idx]) if idx < len(after_list) else False
        if a and not b:
            gained.append(idx + 1)
        elif b and not a:
            lost.append(idx + 1)
    return gained, lost


def _detail_lines(region_before: Dict, region_after: Dict, labels: Dict[str, str]) -> List[str]:
    details: List[str] = []
    before_verdict = _region_verdict(region_before)
    after_verdict = _region_verdict(region_after)
    if before_verdict != after_verdict:
        details.append(
            f"Verdict changed from {before_verdict.replace('_', ' ')} to {after_verdict.replace('_', ' ')}"
        )

    before_quota = _quota_label(region_before.get("quota_status"))
    after_quota = _quota_label(region_after.get("quota_status"))
    if before_quota != after_quota:
        details.append(f"Quota changed from {before_quota.replace('_', ' ')} to {after_quota.replace('_', ' ')}")

    if bool(region_before.get("fell_back")) != bool(region_after.get("fell_back")):
        details.append(
            "Now relying on fallback SKU family"
            if region_after.get("fell_back")
            else "Primary SKU family now satisfies all required zones"
        )

    before_services = {
        str((item or {}).get("service") or "").strip(): str((item or {}).get("detail") or "").strip()
        for item in region_before.get("missing_services") or []
        if (item or {}).get("service")
    }
    after_services = {
        str((item or {}).get("service") or "").strip(): str((item or {}).get("detail") or "").strip()
        for item in region_after.get("missing_services") or []
        if (item or {}).get("service")
    }
    for service in sorted(set(after_services) - set(before_services)):
        details.append(f"{service} is now unavailable")
    for service in sorted(set(before_services) - set(after_services)):
        details.append(f"{service} blocker resolved")

    before_zone = region_before.get("sku_zone_detail") or {}
    after_zone = region_after.get("sku_zone_detail") or {}
    for family in sorted(set(before_zone) | set(after_zone)):
        gained, lost = _zone_changes(before_zone.get(family) or [], after_zone.get(family) or [])
        if not gained and not lost:
            continue
        label = labels.get(family, family)
        if gained:
            details.append(f"{label} gained zone availability in AZ {', '.join(map(str, gained))}")
        if lost:
            details.append(f"{label} lost zone availability in AZ {', '.join(map(str, lost))}")

    before_blockers = set(region_before.get("sku_blockers") or [])
    after_blockers = set(region_after.get("sku_blockers") or [])
    for blocker in sorted(after_blockers - before_blockers):
        details.append(f"New SKU blocker: {blocker}")
    for blocker in sorted(before_blockers - after_blockers):
        details.append(f"Resolved SKU blocker: {blocker}")

    if not details and _region_score(region_before) != _region_score(region_after):
        details.append(f"Score changed from {_region_score(region_before)} to {_region_score(region_after)}")
    return details


def _direction(region_before: Dict, region_after: Dict) -> str:
    before_verdict = _region_verdict(region_before)
    after_verdict = _region_verdict(region_after)
    before_rank = _VERDICT_RANK.get(before_verdict, -1)
    after_rank = _VERDICT_RANK.get(after_verdict, -1)
    if after_rank > before_rank:
        return "improved"
    if after_rank < before_rank:
        return "degraded"
    before_score = _region_score(region_before)
    after_score = _region_score(region_after)
    if after_score > before_score:
        return "improved"
    if after_score < before_score:
        return "degraded"
    return "unchanged"


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)

    run_a = str(req.params.get("a") or "").strip()
    run_b = str(req.params.get("b") or "").strip()
    if not run_a or not run_b:
        return _err("missing_snapshot_ids", "Both query params 'a' and 'b' are required.")
    if run_a == run_b:
        return _err("same_snapshot", "Pick two different snapshots to compare.")

    run_a_entity, snapshot_a = snapshot_store.load_snapshot_by_run_id(run_a)
    run_b_entity, snapshot_b = snapshot_store.load_snapshot_by_run_id(run_b)
    if not run_a_entity or not snapshot_a:
        return _err("not_found", f"Snapshot {run_a} was not found.", 404, run_id=run_a)
    if not run_b_entity or not snapshot_b:
        return _err("not_found", f"Snapshot {run_b} was not found.", 404, run_id=run_b)

    labels = _label_map(snapshot_a, snapshot_b)
    regions_a = _region_index(snapshot_a)
    regions_b = _region_index(snapshot_b)
    common = sorted(set(regions_a) & set(regions_b))

    improved = degraded = unchanged = 0
    new_blockers = 0
    resolved_blockers = 0
    changes = []

    for key in common:
        region_before = regions_a[key]
        region_after = regions_b[key]
        direction = _direction(region_before, region_after)
        details = _detail_lines(region_before, region_after, labels)
        if direction == "improved":
            improved += 1
        elif direction == "degraded":
            degraded += 1
        else:
            unchanged += 1

        before_blockers = set(_hard_blockers(region_before))
        after_blockers = set(_hard_blockers(region_after))
        new_blockers += len(after_blockers - before_blockers)
        resolved_blockers += len(before_blockers - after_blockers)

        if direction == "unchanged" and not details:
            continue
        changes.append(
            {
                "region": region_after.get("short") or region_before.get("short") or key,
                "display": region_after.get("name") or region_before.get("name") or key,
                "direction": direction,
                "score_before": _region_score(region_before),
                "score_after": _region_score(region_after),
                "verdict_before": _region_verdict(region_before),
                "verdict_after": _region_verdict(region_after),
                "details": details,
            }
        )

    order = {"degraded": 0, "improved": 1, "unchanged": 2}
    changes.sort(
        key=lambda item: (
            order.get(item["direction"], 9),
            -abs(int(item["score_after"]) - int(item["score_before"])),
            str(item["display"]).lower(),
        )
    )

    payload = {
        "a_id": run_a,
        "b_id": run_b,
        "a_timestamp": snapshot_store.snapshot_timestamp(run_a_entity, snapshot_a),
        "b_timestamp": snapshot_store.snapshot_timestamp(run_b_entity, snapshot_b),
        "summary": {
            "regions_improved": improved,
            "regions_degraded": degraded,
            "regions_unchanged": unchanged,
            "new_blockers": new_blockers,
            "resolved_blockers": resolved_blockers,
        },
        "changes": changes,
    }
    return func.HttpResponse(
        json.dumps(payload, ensure_ascii=False),
        status_code=200,
        mimetype="application/json",
    )

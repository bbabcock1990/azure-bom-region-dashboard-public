"""GET /api/snapshots[?bom=<bom_id>&limit=50]

Returns the snapshot index for a BOM. The ``bom`` query param scopes results
to a single BOM's run history; ``sub`` is accepted as a legacy alias. If
neither is supplied, returns the most recent across all BOMs.
"""
from __future__ import annotations

import json
import logging
from typing import List

from .._shared import httpfunc as func

from .._shared import auth, storage

log = logging.getLogger(__name__)
DEFAULT_LIMIT = 50
MAX_LIMIT = 500


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)

    # ``bom`` is the current scope key; ``sub`` is kept as a legacy alias
    # (pre-decoupling, runs were partitioned by subscription == bom).
    scope = (req.params.get("bom") or req.params.get("sub") or "").strip()
    try:
        limit = max(1, min(MAX_LIMIT, int(req.params.get("limit") or DEFAULT_LIMIT)))
    except ValueError:
        limit = DEFAULT_LIMIT

    table = storage.get_table_client("runs")
    try:
        if scope:
            entities = list(table.query_entities(query_filter=f"PartitionKey eq '{scope}'"))
        else:
            entities = list(table.list_entities())
    except Exception:
        log.exception("runs list failed")
        entities = []

    # Newest first by RowKey (which begins with ISO ts so sortable)
    entities.sort(key=lambda e: e["RowKey"], reverse=True)
    out: List[dict] = []
    for e in entities[:limit]:
        if e.get("status") != "succeeded":
            continue
        segs_csv = (e.get("customer_segments") or "")
        out.append({
            "run_id": e["RowKey"],
            "bom_id": e["PartitionKey"],
            "subscription_id": e.get("subscription_id") or e["PartitionKey"],
            "started_at": e.get("started_at"),
            "ended_at": e.get("ended_at"),
            "source": e.get("source"),
            "triggered_by_email": e.get("triggered_by_email"),
            "customer_name": e.get("customer_name"),
            "customer_segments": [s for s in segs_csv.split(",") if s] if segs_csv else None,
            "arm_overlay_applied": bool(e.get("arm_overlay_applied")) if "arm_overlay_applied" in e else None,
        })

    return func.HttpResponse(
        json.dumps({"snapshots": out}),
        status_code=200, mimetype="application/json",
    )

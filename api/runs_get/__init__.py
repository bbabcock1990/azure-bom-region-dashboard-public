"""GET /api/runs/{run_id}

Status polling endpoint. Frontend can poll this until status != "running".
"""
from __future__ import annotations

import json
import logging

from .._shared import httpfunc as func

from .._shared import auth, storage

log = logging.getLogger(__name__)


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)

    run_id = req.route_params.get("run_id", "")
    if not run_id:
        return func.HttpResponse(
            json.dumps({"error": "missing_run_id"}), status_code=400, mimetype="application/json",
        )

    table = storage.get_table_client("runs")
    # We don't know the partition (sub_id) up-front, so query.
    try:
        results = list(table.query_entities(query_filter=f"RowKey eq '{run_id}'"))
    except Exception:
        log.exception("runs query failed")
        results = []

    if not results:
        return func.HttpResponse(
            json.dumps({"error": "not_found", "run_id": run_id}),
            status_code=404, mimetype="application/json",
        )
    e = results[0]
    body = {
        "run_id": e["RowKey"],
        "bom_id": e["PartitionKey"],
        "subscription_id": e.get("subscription_id") or e["PartitionKey"],
        "status": e.get("status"),
        "source": e.get("source"),
        "triggered_by_email": e.get("triggered_by_email"),
        "started_at": e.get("started_at"),
        "ended_at": e.get("ended_at"),
        "error": e.get("error"),
        "snapshot_url": f"/api/snapshots/{e['RowKey']}" if e.get("snapshot_blob") else None,
    }
    return func.HttpResponse(
        json.dumps(body), status_code=200, mimetype="application/json",
    )

"""GET /api/snapshots/{run_id}

Streams the snapshot JSON blob to the client. We never expose blob URLs.
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
            json.dumps({"error": "missing_run_id"}),
            status_code=400, mimetype="application/json",
        )

    table = storage.get_table_client("runs")
    try:
        results = list(table.query_entities(query_filter=f"RowKey eq '{run_id}'"))
    except Exception:
        log.exception("runs query failed")
        results = []
    if not results or not results[0].get("snapshot_blob"):
        return func.HttpResponse(
            json.dumps({"error": "not_found", "run_id": run_id}),
            status_code=404, mimetype="application/json",
        )
    blob_name = results[0]["snapshot_blob"]
    container = storage.get_blob_container("snapshots")
    try:
        downloader = container.download_blob(blob_name)
        payload = downloader.readall()
    except Exception:
        log.exception("blob fetch failed: %s", blob_name)
        return func.HttpResponse(
            json.dumps({"error": "blob_missing", "blob": blob_name}),
            status_code=404, mimetype="application/json",
        )
    return func.HttpResponse(
        body=payload,
        status_code=200,
        mimetype="application/json",
    )

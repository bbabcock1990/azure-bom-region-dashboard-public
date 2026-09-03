"""GET /api/snapshots/latest[?bom=<bom_id>]

Convenience: returns the most recent succeeded snapshot blob (full JSON).
If `bom` is supplied, scoped to that BOM (`sub` accepted as a legacy alias).
"""
from __future__ import annotations

import json
import logging

from .._shared import httpfunc as func

from .._shared import auth, storage, snapshot_store

log = logging.getLogger(__name__)


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)

    scope = (req.params.get("bom") or req.params.get("sub") or "").strip()
    table = storage.get_table_client("runs")
    try:
        if scope:
            entities = list(table.query_entities(query_filter=f"PartitionKey eq '{scope}'"))
        else:
            entities = list(table.list_entities())
    except Exception:
        log.exception("runs list failed")
        entities = []

    succeeded = [e for e in entities if e.get("status") == "succeeded" and e.get("snapshot_blob")]
    if not succeeded:
        return func.HttpResponse(
            json.dumps({"error": "no_snapshots"}),
            status_code=404, mimetype="application/json",
        )
    succeeded.sort(key=lambda e: e["RowKey"], reverse=True)
    blob_name = succeeded[0]["snapshot_blob"]
    container = storage.get_blob_container("snapshots")
    try:
        payload = container.download_blob(blob_name).readall()
    except Exception:
        log.exception("blob fetch failed: %s", blob_name)
        return func.HttpResponse(
            json.dumps({"error": "blob_missing"}),
            status_code=404, mimetype="application/json",
        )
    payload = snapshot_store.backfill_meta_timestamp(payload, succeeded[0])
    return func.HttpResponse(
        body=payload, status_code=200, mimetype="application/json",
    )

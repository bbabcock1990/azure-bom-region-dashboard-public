"""GET /api/snapshots/export — download every snapshot as a single .zip.

In the hosted (multi-customer) deployment snapshots live in the customer's own
in-memory/browser-backed store, not on a folder anyone can open — so "open the
folder" is meaningless there. This endpoint is the portable equivalent: it
bundles the current user's snapshot JSON blobs (plus a small ``index.json``
manifest) into a zip the browser downloads. Read-only.
"""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from datetime import datetime, timezone

from .._shared import httpfunc as func
from .._shared import auth, storage, snapshot_store, bom_storage

log = logging.getLogger(__name__)

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(name: str) -> str:
    return _SAFE.sub("_", str(name or "")).strip("_") or "snapshot"


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)

    table = storage.get_table_client("runs")
    try:
        entities = list(table.list_entities())
    except Exception:
        log.exception("runs list failed for export")
        entities = []
    entities.sort(key=lambda e: e.get("RowKey", ""), reverse=True)

    container = storage.get_blob_container("snapshots")

    # BOM definitions (the left-panel "Bills of Materials" list) live in a
    # separate table from the run history. Include them so an import restores
    # the BOMs themselves, not just their analysis snapshots.
    try:
        bom_entities = list(
            storage.get_table_client(bom_storage.TABLE_NAME).list_entities()
        )
    except Exception:
        log.exception("bom list failed for export")
        bom_entities = []

    manifest = []
    buf = io.BytesIO()
    written = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for e in entities:
            if e.get("status") != "succeeded" or not e.get("snapshot_blob"):
                continue
            blob_name = e["snapshot_blob"]
            try:
                payload = container.download_blob(blob_name).readall()
            except Exception:
                log.warning("skipping missing snapshot blob %s", blob_name)
                continue
            payload = snapshot_store.backfill_meta_timestamp(payload, e)
            run_id = e.get("RowKey") or _safe(blob_name)
            customer = _safe(e.get("customer_name") or e.get("PartitionKey") or "bom")
            arcname = f"snapshots/{customer}/{_safe(run_id)}.json"
            zf.writestr(arcname, payload)
            written += 1
            manifest.append({
                "run_id": run_id,
                "bom_id": e.get("PartitionKey"),
                "subscription_id": e.get("subscription_id"),
                "customer_name": e.get("customer_name"),
                "customer_segments": e.get("customer_segments"),
                "source": e.get("source"),
                "triggered_by_email": e.get("triggered_by_email"),
                "arm_overlay_applied": e.get("arm_overlay_applied"),
                "started_at": e.get("started_at"),
                "ended_at": e.get("ended_at"),
                "file": arcname,
            })
        zf.writestr("index.json", json.dumps({
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "count": written,
            "snapshots": manifest,
            "boms": bom_entities,
        }, indent=2))

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    filename = f"bom-snapshots-{stamp}.zip"
    return func.HttpResponse(
        body=buf.getvalue(),
        status_code=200,
        mimetype="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )

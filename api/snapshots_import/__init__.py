"""POST /api/snapshots/import — restore snapshots from an exported .zip.

Accepts the archive produced by ``GET /api/snapshots/export`` (multipart field
``file``) and replays each snapshot back into the current user's store: it
uploads the snapshot JSON blob and upserts the matching ``runs`` table entity so
the run reappears in history. It reads the ``index.json`` manifest for run
metadata and falls back to the snapshot payload's ``meta`` (and the file name)
for anything the manifest doesn't carry — so archives from older exports still
import. Idempotent: importing the same run twice overwrites in place.

This only ever writes to the caller's own store; it never touches Azure.
"""
from __future__ import annotations

import io
import json
import logging
import re
import zipfile

from .._shared import httpfunc as func
from .._shared import auth, csrf, storage, activity_log

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 50 * 1024 * 1024
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe(name: str) -> str:
    return _SAFE.sub("_", str(name or "")).strip("_")


def _err(code: str, message: str, status: int = 400) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": code, "message": message}),
        status_code=status, mimetype="application/json",
    )


def _subscription_from_payload(payload: bytes) -> str:
    try:
        meta = (json.loads(payload) or {}).get("meta") or {}
        return str(meta.get("subscription_id") or "")
    except Exception:
        return ""


def _import_one(zf, entry, container, table):
    """Restore a single snapshot described by a manifest entry (or a synthesized
    one). Returns True on success."""
    arcname = entry.get("file")
    if not arcname:
        return False
    try:
        payload = zf.read(arcname)
    except KeyError:
        return False
    if not payload:
        return False

    run_id = _safe(entry.get("run_id") or "")
    bom_id = _safe(entry.get("bom_id") or "")
    if not run_id:
        # Derive from the file name: snapshots/<customer>/<run_id>.json
        stem = arcname.rsplit("/", 1)[-1]
        run_id = _safe(stem[:-5] if stem.endswith(".json") else stem)
    if not bom_id:
        parts = arcname.split("/")
        bom_id = _safe(parts[1]) if len(parts) >= 3 else run_id
    if not run_id or not bom_id:
        return False

    blob_name = f"{bom_id}/{run_id}.json"
    try:
        container.upload_blob(name=blob_name, data=payload, overwrite=True)
    except Exception:
        log.warning("import: blob upload failed for %s", blob_name, exc_info=True)
        return False

    subscription_id = entry.get("subscription_id") or _subscription_from_payload(payload)
    ent = {
        "PartitionKey": bom_id,
        "RowKey": run_id,
        "status": "succeeded",
        "snapshot_blob": blob_name,
        "started_at": entry.get("started_at") or "",
        "ended_at": entry.get("ended_at") or "",
        "subscription_id": subscription_id or bom_id,
        "source": entry.get("source") or "import",
    }
    if entry.get("customer_name") is not None:
        ent["customer_name"] = entry.get("customer_name")
    if entry.get("customer_segments") is not None:
        ent["customer_segments"] = entry.get("customer_segments")
    if entry.get("triggered_by_email") is not None:
        ent["triggered_by_email"] = entry.get("triggered_by_email")
    if entry.get("arm_overlay_applied") is not None:
        ent["arm_overlay_applied"] = entry.get("arm_overlay_applied")
    try:
        table.upsert_entity(ent)
    except Exception:
        log.warning("import: runs upsert failed for %s/%s", bom_id, run_id, exc_info=True)
        return False
    return True


def main(req: func.HttpRequest) -> func.HttpResponse:
    principal = auth.get_local_user(req)
    try:
        csrf.assert_safe_origin(req)
    except csrf.OriginError as ex:
        return _err("origin_rejected", str(ex), 403)

    try:
        files = req.files
    except Exception as ex:
        return _err("bad_request", f"Could not parse multipart body: {ex}", 400)
    f = files.get("file")
    if f is None:
        return _err("missing_file", "Upload the exported .zip in the 'file' field.", 400)
    blob = f.read()
    if not blob:
        return _err("missing_file", "Uploaded file is empty.", 400)
    if len(blob) > MAX_FILE_BYTES:
        return _err("file_too_large", f"File exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB.", 413)

    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except Exception as ex:
        return _err("bad_zip", f"Could not open the file as a .zip: {ex}", 400)

    names = set(zf.namelist())
    entries = []
    if "index.json" in names:
        try:
            manifest = json.loads(zf.read("index.json"))
            entries = list(manifest.get("snapshots") or [])
        except Exception:
            entries = []
    if not entries:
        # No usable manifest — synthesize one entry per snapshot JSON file.
        entries = [{"file": n} for n in sorted(names)
                   if n.startswith("snapshots/") and n.endswith(".json")]

    if not entries:
        return _err("no_snapshots", "No snapshots found in the archive.", 400)

    container = storage.get_blob_container("snapshots")
    table = storage.get_table_client("runs")
    imported = 0
    for entry in entries:
        if _import_one(zf, entry, container, table):
            imported += 1
    skipped = len(entries) - imported

    activity_log.record(
        event_type="snapshots_import",
        actor_email=getattr(principal, "email", None),
        api_scope="local",
        message=f"Imported {imported} snapshot(s) from archive ({skipped} skipped)",
    )
    return func.HttpResponse(
        json.dumps({"ok": True, "imported": imported, "skipped": skipped}),
        status_code=200, mimetype="application/json",
    )

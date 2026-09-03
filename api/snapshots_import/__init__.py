"""POST /api/snapshots/import — restore snapshots from an exported .zip.

Accepts the archive produced by ``GET /api/snapshots/export`` (multipart field
``file``) and replays each snapshot back into the current user's store: it
uploads the snapshot JSON blob and upserts the matching ``runs`` table entity so
the run reappears in history, and restores the BOM definitions (the left-panel
"Bills of Materials" list) from the archive's ``boms`` manifest. It reads the ``index.json`` manifest for run
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
from .._shared import auth, csrf, storage, activity_log, bom_storage

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


def _restore_bom_entities(bom_entities, bom_table) -> int:
    """Restore BOM definitions (the left-panel list) from raw exported table
    entities. Each entity already carries PartitionKey/RowKey plus the flat
    ``*_json`` columns, so a direct upsert round-trips it losslessly."""
    restored = 0
    for ent in bom_entities or []:
        if not isinstance(ent, dict):
            continue
        pk = ent.get("PartitionKey") or bom_storage.PARTITION
        rk = ent.get("RowKey")
        if not rk:
            continue
        row = dict(ent)
        row["PartitionKey"] = pk
        row["RowKey"] = rk
        try:
            bom_table.upsert_entity(row, mode="replace")
            restored += 1
        except Exception:
            log.warning("import: bom upsert failed for %s", rk, exc_info=True)
    return restored


def _reconstruct_boms_from_snapshots(zf, entries, actor_email) -> int:
    """Best-effort fallback for archives exported before BOM definitions were
    included: rebuild a minimal BOM per unique bom_id from each snapshot's
    ``meta`` block so at least the BOM reappears in the list."""
    seen = set()
    restored = 0
    for entry in entries:
        arcname = entry.get("file")
        if not arcname:
            continue
        try:
            payload = zf.read(arcname)
        except KeyError:
            continue
        try:
            meta = (json.loads(payload) or {}).get("meta") or {}
        except Exception:
            continue

        bom_id = _safe(entry.get("bom_id") or "")
        if not bom_id:
            parts = arcname.split("/")
            bom_id = _safe(parts[1]) if len(parts) >= 3 else ""
        if not bom_id or bom_id in seen:
            continue

        sub_id = meta.get("subscription_id") or entry.get("subscription_id") or bom_id
        sub_ids = meta.get("subscription_ids") or ([sub_id] if sub_id else None)
        segments = meta.get("customer_segments")
        if isinstance(segments, (list, tuple)):
            segments = ",".join(str(s) for s in segments)
        services = meta.get("services") or []
        required_skus = meta.get("skus_resolved") or []
        common = dict(
            subscription_id=str(sub_id),
            subscription_ids=[str(s) for s in sub_ids] if sub_ids else None,
            tag=None,
            customer_name=meta.get("customer_name"),
            customer_segments=segments,
            updated_by=actor_email or "import",
        )
        try:
            bom_storage.upsert(
                bom_id, required_skus=required_skus, services=services, **common
            )
        except bom_storage.BomStorageError:
            # Catalog drift (e.g. a renamed service) must not lose the BOM —
            # fall back to a bare BOM so it still reappears in the list.
            try:
                bom_storage.upsert(
                    bom_id, required_skus=[], services=[], **common
                )
            except Exception:
                log.warning("import: bom reconstruct failed for %s", bom_id, exc_info=True)
                continue
        except Exception:
            log.warning("import: bom reconstruct failed for %s", bom_id, exc_info=True)
            continue
        seen.add(bom_id)
        restored += 1
    return restored


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
    bom_entities = []
    if "index.json" in names:
        try:
            manifest = json.loads(zf.read("index.json"))
            entries = list(manifest.get("snapshots") or [])
            bom_entities = list(manifest.get("boms") or [])
        except Exception:
            entries = []
            bom_entities = []
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

    # Restore the BOM definitions so the left-panel list repopulates. Prefer the
    # exported entities; for older archives that predate BOM export, rebuild a
    # minimal BOM from each snapshot's meta as a best-effort fallback.
    actor_email = getattr(principal, "email", None)
    bom_table = storage.get_table_client(bom_storage.TABLE_NAME)
    boms_restored = _restore_bom_entities(bom_entities, bom_table)
    if not boms_restored:
        boms_restored = _reconstruct_boms_from_snapshots(zf, entries, actor_email)

    activity_log.record(
        event_type="snapshots_import",
        actor_email=actor_email,
        api_scope="local",
        message=(f"Imported {imported} snapshot(s) and {boms_restored} BOM(s) "
                 f"from archive ({skipped} skipped)"),
    )
    return func.HttpResponse(
        json.dumps({"ok": True, "imported": imported, "skipped": skipped,
                    "boms": boms_restored}),
        status_code=200, mimetype="application/json",
    )

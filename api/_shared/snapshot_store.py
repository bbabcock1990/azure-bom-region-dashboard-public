from __future__ import annotations

import json
import logging
import os
import re
from typing import Dict, List, Optional, Tuple

from . import storage

log = logging.getLogger(__name__)

_RUN_ID_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z)")

# Keep at most this many succeeded snapshots per BOM on disk. Snapshots are
# 300-500KB JSON files that accumulate on every run; without pruning a customer
# laptop slowly fills up. Override via env for special cases.
SNAPSHOT_RETENTION = max(1, int(os.getenv("SNAPSHOT_RETENTION", "15") or 15))


def get_run_entity(run_id: str) -> Optional[Dict]:
    run_id = str(run_id or "").strip()
    if not run_id:
        return None
    table = storage.get_table_client("runs")
    try:
        rows = list(table.query_entities(query_filter=f"RowKey eq '{run_id}'"))
    except Exception:
        log.exception("runs query failed for %s", run_id)
        return None
    return rows[0] if rows else None


def get_latest_succeeded_run(bom_id: str) -> Optional[Dict]:
    bom_id = str(bom_id or "").strip()
    if not bom_id:
        return None
    table = storage.get_table_client("runs")
    try:
        rows = list(table.query_entities(query_filter=f"PartitionKey eq '{bom_id}'"))
    except Exception:
        log.exception("runs list failed for bom %s", bom_id)
        return None
    succeeded = [row for row in rows if row.get("status") == "succeeded" and row.get("snapshot_blob")]
    if not succeeded:
        return None
    succeeded.sort(key=lambda row: row.get("RowKey") or "", reverse=True)
    return succeeded[0]


def load_snapshot_from_run_entity(run_entity: Dict) -> Tuple[Optional[Dict], Optional[bytes]]:
    if not run_entity or not run_entity.get("snapshot_blob"):
        return None, None
    blob_name = run_entity["snapshot_blob"]
    container = storage.get_blob_container("snapshots")
    try:
        payload = container.download_blob(blob_name).readall()
        return json.loads(payload.decode("utf-8")), payload
    except FileNotFoundError:
        log.warning("snapshot blob missing: %s", blob_name)
    except Exception:
        log.exception("snapshot blob load failed: %s", blob_name)
    return None, None


def load_snapshot_by_run_id(run_id: str) -> Tuple[Optional[Dict], Optional[Dict]]:
    run_entity = get_run_entity(run_id)
    if not run_entity:
        return None, None
    snapshot, _ = load_snapshot_from_run_entity(run_entity)
    return run_entity, snapshot


def load_latest_snapshot_for_bom(bom_id: str) -> Tuple[Optional[Dict], Optional[Dict]]:
    run_entity = get_latest_succeeded_run(bom_id)
    if not run_entity:
        return None, None
    snapshot, _ = load_snapshot_from_run_entity(run_entity)
    return run_entity, snapshot


def snapshot_timestamp(run_entity: Optional[Dict], snapshot: Optional[Dict]) -> Optional[str]:
    meta = (snapshot or {}).get("meta") if isinstance(snapshot, dict) else {}
    if isinstance(meta, dict):
        for key in ("compiled_at", "ended_at", "started_at"):
            value = meta.get(key)
            if value:
                return str(value)
    if run_entity:
        for key in ("ended_at", "started_at"):
            value = run_entity.get(key)
            if value:
                return str(value)
        run_id = str(run_entity.get("RowKey") or "")
        match = _RUN_ID_TS_RE.match(run_id)
        if match:
            return match.group(1).replace("T", " ").replace("Z", " UTC")
    return None


def backfill_meta_timestamp(payload: bytes, run_entity: Optional[Dict]) -> bytes:
    """Ensure the streamed snapshot JSON carries ``meta.compiled_at`` so the UI
    can show freshness. Older snapshots were persisted without a timestamp in
    ``meta``; derive one from the run entity (ended_at/started_at/run-id) and
    inject it. On any parse issue the original bytes are returned unchanged."""
    try:
        snapshot = json.loads(payload)
    except Exception:
        return payload
    if not isinstance(snapshot, dict):
        return payload
    meta = snapshot.get("meta")
    if not isinstance(meta, dict):
        return payload
    if meta.get("compiled_at"):
        return payload
    ts = snapshot_timestamp(run_entity, snapshot)
    if not ts:
        return payload
    meta["compiled_at"] = ts
    try:
        return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except Exception:
        return payload


def prune_snapshots(bom_id: str, keep: Optional[int] = None) -> int:
    """Delete succeeded runs (and their snapshot blobs) beyond the newest
    ``keep`` for a BOM. Best-effort; returns the number of snapshots removed.

    Called after a successful run so on-disk snapshot JSON does not grow
    without bound on a customer laptop.
    """
    keep = SNAPSHOT_RETENTION if keep is None else max(1, int(keep))
    bom_id = str(bom_id or "").strip()
    if not bom_id:
        return 0
    table = storage.get_table_client("runs")
    try:
        rows = list(table.query_entities(query_filter=f"PartitionKey eq '{bom_id}'"))
    except Exception:
        log.exception("prune_snapshots: run list failed for %s", bom_id)
        return 0
    succeeded = [r for r in rows if r.get("status") == "succeeded" and r.get("snapshot_blob")]
    succeeded.sort(key=lambda r: r.get("RowKey") or "", reverse=True)
    stale = succeeded[keep:]
    if not stale:
        return 0
    container = storage.get_blob_container("snapshots")
    removed = 0
    for run in stale:
        blob_name = run.get("snapshot_blob")
        try:
            if blob_name:
                container.delete_blob(blob_name)
        except Exception:
            log.debug("prune_snapshots: could not delete blob %s", blob_name)
        try:
            table.delete_entity(run.get("PartitionKey"), run.get("RowKey"))
            removed += 1
        except Exception:
            log.debug("prune_snapshots: could not delete run row %s", run.get("RowKey"))
    if removed:
        log.info("prune_snapshots: removed %d stale snapshot(s) for bom %s", removed, bom_id)
    return removed

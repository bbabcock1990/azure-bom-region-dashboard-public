from __future__ import annotations

import json
import logging
import re
from typing import Dict, Optional, Tuple

from . import storage

log = logging.getLogger(__name__)

_RUN_ID_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z)")


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

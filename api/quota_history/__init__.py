"""GET/POST /api/quota/history

Persists quota increase request history in the local SQLite database
so that request status (approved/failed/pending) survives page refreshes.

GET  — returns all quota request history entries (optionally filtered by subscription_id and bom_id)
POST — upserts a quota request history entry
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from typing import Any, List, Optional

from .._shared import activity_log, auth, storage
from .._shared import httpfunc as func

log = logging.getLogger(__name__)

_TABLE = "quota_request_history"
_init_lock = threading.Lock()
_initialized = False


def _ensure_quota_table() -> None:
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        conn = storage._connect()
        try:
            existing = conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (_TABLE,),
            ).fetchone()
            if existing:
                columns = conn.execute(f'PRAGMA table_info("{_TABLE}")').fetchall()
                column_names = [str(row[1]) for row in columns]
                pk_columns = [str(row[1]) for row in sorted(columns, key=lambda row: int(row[5] or 0)) if int(row[5] or 0) > 0]
                if "bom_id" not in column_names or pk_columns != [
                    "region",
                    "family",
                    "subscription_id",
                    "bom_id",
                ]:
                    conn.execute(f'DROP TABLE IF EXISTS "{_TABLE}"')
            conn.execute(
                f"""CREATE TABLE IF NOT EXISTS "{_TABLE}" (
                    region TEXT NOT NULL,
                    family TEXT NOT NULL,
                    subscription_id TEXT NOT NULL,
                    bom_id TEXT NOT NULL,
                    subscription_name TEXT DEFAULT '',
                    requested_limit INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    message TEXT DEFAULT '',
                    request_id TEXT DEFAULT '',
                    requested_at INTEGER NOT NULL,
                    completed_at INTEGER,
                    provisioning_state TEXT DEFAULT '',
                    PRIMARY KEY (region, family, subscription_id, bom_id)
                )"""
            )
            conn.execute(
                f'CREATE INDEX IF NOT EXISTS "{_TABLE}_status" ON "{_TABLE}" (status)'
            )
            _initialized = True
        finally:
            conn.close()


def _err(code: str, message: str, status: int = 400) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": code, "message": message}),
        status_code=status,
        mimetype="application/json",
    )


def _ok(payload: Any, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload), status_code=status, mimetype="application/json"
    )


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "region": row[0],
        "family": row[1],
        "subscription_id": row[2],
        "bom_id": row[3],
        "subscription_name": row[4] or "",
        "requested_limit": row[5],
        "status": row[6],
        "message": row[7] or "",
        "request_id": row[8] or "",
        "requested_at": row[9],
        "completed_at": row[10],
        "provisioning_state": row[11] or "",
    }


def _get_all(subscription_id: Optional[str] = None, bom_id: Optional[str] = None) -> List[dict]:
    _ensure_quota_table()
    conn = storage._connect()
    try:
        if subscription_id and bom_id:
            rows = conn.execute(
                f'SELECT * FROM "{_TABLE}" WHERE subscription_id = ? AND bom_id = ? ORDER BY requested_at DESC',
                (subscription_id, bom_id),
            ).fetchall()
        elif subscription_id:
            rows = conn.execute(
                f'SELECT * FROM "{_TABLE}" WHERE subscription_id = ? ORDER BY requested_at DESC',
                (subscription_id,),
            ).fetchall()
        elif bom_id:
            rows = conn.execute(
                f'SELECT * FROM "{_TABLE}" WHERE bom_id = ? ORDER BY requested_at DESC',
                (bom_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                f'SELECT * FROM "{_TABLE}" ORDER BY requested_at DESC'
            ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def _upsert(entry: dict) -> None:
    _ensure_quota_table()
    conn = storage._connect()
    try:
        conn.execute(
            f"""INSERT INTO "{_TABLE}"
                (region, family, subscription_id, bom_id, subscription_name,
                 requested_limit, status, message, request_id,
                 requested_at, completed_at, provisioning_state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(region, family, subscription_id, bom_id) DO UPDATE SET
                requested_limit = excluded.requested_limit,
                status = excluded.status,
                message = excluded.message,
                request_id = excluded.request_id,
                requested_at = excluded.requested_at,
                completed_at = excluded.completed_at,
                provisioning_state = excluded.provisioning_state,
                subscription_name = excluded.subscription_name
            """,
            (
                entry["region"],
                entry["family"],
                entry["subscription_id"],
                entry["bom_id"],
                entry.get("subscription_name", ""),
                entry["requested_limit"],
                entry.get("status", "pending"),
                entry.get("message", ""),
                entry.get("request_id", ""),
                entry["requested_at"],
                entry.get("completed_at"),
                entry.get("provisioning_state", ""),
            ),
        )
    finally:
        conn.close()


def main(req: func.HttpRequest) -> func.HttpResponse:
    principal = auth.get_local_user(req)

    if req.method and req.method.upper() == "POST":
        try:
            body = req.get_json()
        except ValueError:
            activity_log.record(
                "quota_history_save",
                actor_email=principal.email,
                actor_oid=principal.oid,
                api_scope="local",
                status="error",
                message="Quota history save rejected: invalid JSON body",
            )
            return _err("bad_json", "Body must be a JSON object.", 400)

        if not isinstance(body, dict):
            activity_log.record(
                "quota_history_save",
                actor_email=principal.email,
                actor_oid=principal.oid,
                api_scope="local",
                status="error",
                message="Quota history save rejected: body was not an object",
            )
            return _err("bad_json", "Body must be a JSON object.", 400)

        required = ("region", "family", "subscription_id", "bom_id", "requested_limit", "requested_at")
        missing = [f for f in required if not body.get(f)]
        if missing:
            activity_log.record(
                "quota_history_save",
                actor_email=principal.email,
                actor_oid=principal.oid,
                subscription_id=body.get("subscription_id"),
                api_scope="local",
                status="error",
                message=f"Quota history save rejected: missing {', '.join(missing)}",
                details={"bom_id": body.get("bom_id"), "region": body.get("region"), "family": body.get("family")},
            )
            return _err("missing_fields", f"Missing required fields: {', '.join(missing)}", 400)

        try:
            body["requested_limit"] = int(body["requested_limit"])
            body["requested_at"] = int(body["requested_at"])
        except (TypeError, ValueError):
            activity_log.record(
                "quota_history_save",
                actor_email=principal.email,
                actor_oid=principal.oid,
                subscription_id=body.get("subscription_id"),
                api_scope="local",
                status="error",
                message="Quota history save rejected: invalid numeric values",
                details={"bom_id": body.get("bom_id"), "region": body.get("region"), "family": body.get("family")},
            )
            return _err("bad_value", "requested_limit and requested_at must be integers.", 400)

        _upsert(body)
        activity_log.record(
            "quota_history_save",
            actor_email=principal.email,
            actor_oid=principal.oid,
            subscription_id=body.get("subscription_id"),
            api_scope="local",
            status="ok",
            message=f"Saved quota history for {body.get('family')} in {body.get('region')}",
            details={
                "bom_id": body.get("bom_id"),
                "region": body.get("region"),
                "family": body.get("family"),
                "requested_limit": body.get("requested_limit"),
                "status": body.get("status", "pending"),
            },
        )
        return _ok({"status": "saved"})

    # GET
    subscription_id = req.params.get("subscription_id")
    bom_id = req.params.get("bom_id")
    entries = _get_all(subscription_id, bom_id)
    return _ok({"entries": entries})

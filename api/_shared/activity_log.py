"""
Activity log — best-effort audit trail backed by SQLite locally.
Records every ARM / analysis lifecycle event and user actions
(quota increases, etc.) so the operator can verify that the right
subscriptions and APIs are being queried.

Design choices worth knowing:

* Recording is **best-effort**. ``record()`` never raises; storage hiccups
  must not break the run that's being logged.
* PartitionKey = ``YYYY-MM-DD`` (UTC) of the event. Keeps partitions
  small and makes "show me the last N days" trivial.
* RowKey = ``f"{reverse_tick}_{uuid_short}"`` so RowKey-ASC iteration
  inside a partition = newest first.
* ``api_scope`` field on every entry: ``"subscription"`` for ARM,
  ``"local"`` for analysis/lifecycle/user-action entries.
  This is critical context — ARM is a per-subscription API.
* ``details`` is sanitized **defensively** inside ``record()`` — keys
  matching token/auth/secret/password/cookie/credential are masked
  regardless of what the caller passed. Don't rely on caller discipline.
* ``details_json`` is truncated to ~28KB (Azure Table per-property
  limit is 64KB UTF-16 ≈ 32KB UTF-8, leaving headroom). When truncated
  we store a JSON envelope with ``"truncated": true`` and a preview so
  consumers never have to parse half-written JSON.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from . import storage

log = logging.getLogger(__name__)

TABLE_NAME = "activitylog"

# Azure Table Storage caps a single string property at ~64KB UTF-16; in
# practice ~32KB UTF-8 is safe. Leave headroom for the wrapper JSON.
_DETAILS_BYTE_BUDGET = 28 * 1024

# Tick-based reverse ordering: subtracting current ms-since-epoch from
# a fixed ceiling gives a string that sorts ASC = newest-first. The
# ceiling must be safely above current ms-since-epoch (~1.75e12 in 2026)
# so the result is always positive and right-justifies cleanly under
# zero-padding. 1e15 ms = year 33658 — well past any realistic use.
_REVERSE_TICKS_CEILING = 10 ** 15

_MESSAGE_MAX_CHARS = 240

# Keys whose values we always redact inside `details`. Case-insensitive
# substring match. Defensive — the caller may forget.
_SENSITIVE_KEY_RE = re.compile(
    r"token|authorization|auth_header|secret|password|cookie|credential|api[_-]?key|bearer",
    re.IGNORECASE,
)

# Pre-fingerprinted values look like `prefix…(len=N)`. When _redact sees a
# value already in this shape under a sensitive key, it passes it through
# rather than over-redacting it to "<redacted>". This lets callers
# *opt in* to a diagnostic fingerprint via token_fingerprint() while
# preserving the default-deny posture for raw secrets.
_FINGERPRINT_MARKER_RE = re.compile(r"\u2026\(len=\d+\)$")

# Event-type allow-list. Anything else is accepted but logged at debug
# level so we notice typos in call sites.
KNOWN_EVENT_TYPES = frozenset({
    "analysis_start",
    "analysis_complete",
    "analysis_failed",
    "arm_call_start",
    "arm_call_ok",
    "arm_call_error",
    "arm_call_skipped",
    "snapshot_loaded",
    "subscription_switch",
    "log_cleared",
    "quota_request_start",
    "quota_request_ok",
    "quota_request_failed",
    "quota_status_check",
    "auth_signin",
    "auth_signin_ok",
    "auth_signin_error",
    "auth_signout",
    "bom_import",
    "subscription_metadata_update",
    "subscription_metadata_delete",
    "bom_service_add",
    "bom_service_remove",
    "bom_region_add",
    "bom_region_remove",
    "quota_history_save",
    "donor_quota_scan",
})


# ---------------------------------------------------------------- helpers

def token_fingerprint(token: Optional[str]) -> str:
    """A safe-to-log shape for a bearer token: prefix + length.

    Never returns the raw token. Returns ``"(none)"`` for empty input.
    """
    if not token:
        return "(none)"
    clean = token.strip()
    if clean.lower().startswith("bearer "):
        clean = clean[7:].strip()
    prefix = clean[:8]
    return f"{prefix}…(len={len(clean)})"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _redact(value: Any, _depth: int = 0) -> Any:
    """Recursively scrub sensitive-looking values out of `details`.

    Default-deny: when a key matches ``_SENSITIVE_KEY_RE`` we replace
    its value with the literal string ``"<redacted>"``. Partial-reveal
    fingerprints are NOT generated here — callers who want a stable
    diagnostic fingerprint must call ``token_fingerprint()`` explicitly
    and pass the result through. We recognize already-fingerprinted
    strings (``prefix…(len=N)``) and pass them through unchanged so the
    explicit-fingerprint path keeps working.
    """
    if _depth > 6:
        return "<truncated:depth>"
    if isinstance(value, Mapping):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _SENSITIVE_KEY_RE.search(k):
                if isinstance(v, str) and v and _FINGERPRINT_MARKER_RE.search(v):
                    out[k] = v  # already-safe diagnostic fingerprint
                else:
                    out[k] = "<redacted>"
            else:
                out[k] = _redact(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v, _depth + 1) for v in value]
    return value


def _encode_details(details: Optional[Mapping[str, Any]]) -> Optional[str]:
    if not details:
        return None
    try:
        safe = _redact(dict(details))
        encoded = json.dumps(safe, ensure_ascii=False, default=str,
                             separators=(",", ":"))
    except (TypeError, ValueError) as ex:
        return json.dumps({"_encode_error": repr(ex)[:200]})

    raw = encoded.encode("utf-8")
    if len(raw) <= _DETAILS_BYTE_BUDGET:
        return encoded

    # Byte-aware truncation; produce a valid JSON envelope so consumers
    # never see half-written data.
    keep = max(_DETAILS_BYTE_BUDGET - 256, 1024)
    preview_bytes = raw[:keep]
    # Round to a UTF-8 boundary so the preview decodes cleanly.
    while preview_bytes and (preview_bytes[-1] & 0xC0) == 0x80:
        preview_bytes = preview_bytes[:-1]
    preview = preview_bytes.decode("utf-8", errors="ignore")
    envelope = {
        "truncated": True,
        "original_bytes": len(raw),
        "preview": preview,
    }
    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))


def _make_keys(now: datetime) -> Dict[str, str]:
    pk = now.strftime("%Y-%m-%d")
    ticks = int(now.timestamp() * 1000)  # ms precision; plenty for ordering
    rev = _REVERSE_TICKS_CEILING - ticks
    rk = f"{rev:020d}_{uuid.uuid4().hex[:12]}"
    return {"PartitionKey": pk, "RowKey": rk}


# ---------------------------------------------------------------- record

def record(
    event_type: str,
    *,
    actor_email: Optional[str] = None,
    actor_oid: Optional[str] = None,
    subscription_id: Optional[str] = None,
    run_id: Optional[str] = None,
    status: str = "info",
    api_scope: Optional[str] = None,
    message: Optional[str] = None,
    duration_ms: Optional[int] = None,
    details: Optional[Mapping[str, Any]] = None,
) -> None:
    """Append a single event. Never raises — storage hiccups must not
    break the run that's being logged.

    Args:
        event_type: short code, see ``KNOWN_EVENT_TYPES``.
        actor_email/actor_oid: who triggered the event.
        subscription_id: associated subscription (may be the analysis
            subscription even for global-scope events, for correlation).
        run_id: analysis run correlation ID.
        status: ``"info"|"ok"|"error"``.
        api_scope: ``"global"`` (global availability feed), ``"subscription"``
            (ARM), or ``"local"`` (analysis lifecycle). Set this on every
            external API event so consumers know what the sub_id really means.
        message: short human-readable summary, truncated to
            ``_MESSAGE_MAX_CHARS``.
        duration_ms: wall-clock duration in milliseconds.
        details: arbitrary dict; sensitive keys are redacted automatically.
    """
    try:
        if event_type not in KNOWN_EVENT_TYPES:
            log.debug("activity_log: unknown event_type %r", event_type)

        now = _now_utc()
        entity = _make_keys(now)
        entity["timestamp_iso"] = now.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        entity["event_type"] = event_type
        entity["status"] = (status or "info")[:16]
        if actor_email:
            entity["actor_email"] = actor_email[:120]
        if actor_oid:
            entity["actor_oid"] = actor_oid[:64]
        if subscription_id:
            entity["subscription_id"] = subscription_id[:64]
        if run_id:
            entity["run_id"] = run_id[:64]
        if api_scope:
            entity["api_scope"] = api_scope[:32]
        if message:
            entity["message"] = message[:_MESSAGE_MAX_CHARS]
        if duration_ms is not None:
            try:
                entity["duration_ms"] = int(duration_ms)
            except (TypeError, ValueError):
                pass
        encoded = _encode_details(details)
        if encoded is not None:
            entity["details_json"] = encoded

        table = storage.get_table_client(TABLE_NAME)
        table.create_entity(entity=entity)
    except Exception:
        # Logging the storage failure is fine, but never propagate.
        log.exception("activity_log.record failed (event_type=%s)", event_type)


# ---------------------------------------------------------------- query

def _normalize_timestamp_iso(value: Any) -> Optional[str]:
    """Canonicalize a stored timestamp back to ``YYYY-MM-DDTHH:MM:SSZ``.

    The azure-data-tables Python SDK auto-deserializes string properties
    that look like ISO 8601 timestamps into ``datetime`` objects (and
    JSON serialization would then render them in the runtime's local
    timezone). We coerce back to a UTC ISO string so the UI always shows
    the timestamp in the same canonical form regardless of who's
    reading it.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0) \
                 .strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def _entity_to_dict(e: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "id": f"{e.get('PartitionKey')}/{e.get('RowKey')}",
        "timestamp_iso": _normalize_timestamp_iso(e.get("timestamp_iso")),
        "event_type": e.get("event_type"),
        "status": e.get("status"),
        "actor_email": e.get("actor_email"),
        "actor_oid": e.get("actor_oid"),
        "subscription_id": e.get("subscription_id"),
        "run_id": e.get("run_id"),
        "api_scope": e.get("api_scope"),
        "message": e.get("message"),
        "duration_ms": e.get("duration_ms"),
        "details_json": e.get("details_json"),
    }


def _recent_partitions(max_days: int) -> List[str]:
    today = _now_utc().date()
    return [(today - timedelta(days=d)).isoformat() for d in range(max_days)]


def query(
    *,
    limit: int = 200,
    max_days: int = 7,
    subscription_id: Optional[str] = None,
    event_type: Optional[str] = None,
    actor_oid: Optional[str] = None,
    actor_email: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return recent events, newest-first, with in-Python filtering.

    We push the PartitionKey filter into the table query (one partition
    at a time, newest first) and stop as soon as we have ``limit``
    matching rows. With daily partitions this is fast at expected scale
    (~hundreds of events/day on this local dashboard).
    """
    limit = max(1, min(int(limit or 0), 1000))
    max_days = max(1, min(int(max_days or 0), 90))

    out: List[Dict[str, Any]] = []
    try:
        table = storage.get_table_client(TABLE_NAME)
    except Exception:
        log.exception("activity_log.query: could not open table")
        return out

    for pk in _recent_partitions(max_days):
        if len(out) >= limit:
            break
        try:
            entities = table.query_entities(
                query_filter="PartitionKey eq @pk",
                parameters={"pk": pk},
            )
        except Exception:
            log.exception("activity_log.query: partition %s failed", pk)
            continue
        # Within a partition RowKey ASC = newest-first thanks to the
        # reverse-tick prefix.
        for e in entities:
            if subscription_id and e.get("subscription_id") != subscription_id:
                continue
            if event_type and e.get("event_type") != event_type:
                continue
            if actor_oid and e.get("actor_oid") != actor_oid:
                continue
            if actor_email and (e.get("actor_email") or "").lower() != actor_email.lower():
                continue
            out.append(_entity_to_dict(e))
            if len(out) >= limit:
                break
    return out


# ---------------------------------------------------------------- clear

def clear() -> Dict[str, Any]:
    """Drop and recreate the table.

    Trade-off: this is fast and simple but isn't atomic against
    concurrent writes — a record() call happening right now may either
    land in the doomed table or the new one. Acceptable for a manual
    admin "Clear log" button with a confirm dialog.

    Returns a small summary dict for the API response.
    """
    deleted = False
    try:
        service = storage.get_table_service()
        try:
            service.delete_table(TABLE_NAME)
            deleted = True
        except Exception:
            log.exception("activity_log.clear: delete_table failed (continuing)")
        # Recreate eagerly so the next record() call doesn't race
        # against table creation.
        try:
            service.create_table(TABLE_NAME)
        except Exception:
            # Already exists is fine. Other errors will surface on the
            # next record() call as usual.
            log.debug("activity_log.clear: create_table noop/race")
        return {"deleted": deleted, "table": TABLE_NAME}
    except Exception as ex:
        log.exception("activity_log.clear: unexpected failure")
        return {"deleted": deleted, "table": TABLE_NAME, "error": repr(ex)[:200]}

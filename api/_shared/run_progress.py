"""In-process progress tracking for long-running compile runs.

Single-process, local-only. Survives across HTTP calls inside the same
Python worker but is lost on worker restart. The frontend treats
``found=False`` as "progress unavailable — run still in flight" rather
than as failure, so this is acceptable for the dashboard's intended
local-only use.

Threading
---------
The compile path runs in the Functions sync worker thread but long-running
region fan-out work may spawn its own ThreadPoolExecutor workers, each
calling ``increment()``. All public functions hold
a module-level ``threading.Lock`` so concurrent updates are race-free.

Progress shape
--------------
A progress record (returned by :func:`get`) looks like::

    {
        "found": True,
        "progress_token": "...",
        "actor_oid": "00000000-...",   # binds the record to the user
        "status": "running" | "succeeded" | "failed",
        "phases": ["bom_load", "arm_sku_availability", "finalize"],
        "current_phase_index": 2,
        "current_phase_label": "Building model",
        "completed": 12,
        "total": 38,
        "percent": 60,
        "phase_started_at_iso": "2026-05-19T18:00:00Z",
        "started_at_iso": "2026-05-19T17:58:30Z",
        "last_update_at_iso": "2026-05-19T18:01:42Z",
        "elapsed_seconds": 192,
        "phase_elapsed_seconds": 102,
        "eta_seconds": 188,         # only set when completed >= 4
        "run_id": "2026-05-19T18-00-00Z-...",   # set on success
        "error_code": None,
        "error_message": None,
    }
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

_LOCK = threading.Lock()
_PROGRESS: Dict[str, Dict[str, Any]] = {}
_MAX_RECORDS = 50

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# ETA gating thresholds (rubber-duck recommended): don't estimate from
# pathologically-small samples.
_ETA_MIN_COMPLETED = 4
_ETA_MIN_ELAPSED_S = 15.0


def is_valid_token(s: str) -> bool:
    return bool(s) and bool(_UUID_RE.match(s.strip()))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _evict_if_needed_locked() -> None:
    """Caller must hold the lock. Evict oldest records by last_update_at."""
    if len(_PROGRESS) <= _MAX_RECORDS:
        return
    # Sort by last_update_ts ascending; drop the oldest.
    sortable: List[Tuple[float, str]] = sorted(
        ((rec.get("last_update_ts") or 0.0, tok) for tok, rec in _PROGRESS.items()),
        key=lambda x: x[0],
    )
    overflow = len(_PROGRESS) - _MAX_RECORDS
    for _ts, tok in sortable[:overflow]:
        _PROGRESS.pop(tok, None)


def start(
    token: str,
    *,
    actor_oid: str,
    phases: List[str],
) -> None:
    """Register a new progress record. Must be called once at run start."""
    if not is_valid_token(token):
        return  # silently ignore — tokens must be valid UUIDs
    now_ts = time.time()
    now_iso = _now_iso()
    rec: Dict[str, Any] = {
        "progress_token": token,
        "actor_oid": actor_oid or "",
        "status": "running",
        "phases": list(phases or ["work"]),
        "current_phase_index": 0,
        "current_phase_label": (phases[0] if phases else "work"),
        "completed": 0,
        "total": None,
        "percent": 0,
        "started_at_iso": now_iso,
        "started_ts": now_ts,
        "phase_started_at_iso": now_iso,
        "phase_started_ts": now_ts,
        "last_update_at_iso": now_iso,
        "last_update_ts": now_ts,
        "run_id": None,
        "error_code": None,
        "error_message": None,
    }
    with _LOCK:
        _PROGRESS[token] = rec
        _evict_if_needed_locked()


def set_phase(
    token: str,
    phase_index: int,
    *,
    label: Optional[str] = None,
    total: Optional[int] = None,
) -> None:
    """Move to a new phase. Resets ``completed`` to 0; ``total`` may be None
    if the phase isn't itemized (just a single-step phase).
    """
    if not is_valid_token(token):
        return
    now_ts = time.time()
    now_iso = _now_iso()
    with _LOCK:
        rec = _PROGRESS.get(token)
        if rec is None or rec.get("status") != "running":
            return
        # Monotonic-phase guard: refuse to go backward.
        if phase_index < int(rec.get("current_phase_index") or 0):
            return
        phases = rec.get("phases") or []
        if label is None and 0 <= phase_index < len(phases):
            label = phases[phase_index]
        rec["current_phase_index"] = phase_index
        rec["current_phase_label"] = label or f"phase {phase_index}"
        rec["completed"] = 0
        rec["total"] = total
        rec["phase_started_at_iso"] = now_iso
        rec["phase_started_ts"] = now_ts
        rec["last_update_at_iso"] = now_iso
        rec["last_update_ts"] = now_ts
        # Overall percent: floor of (completed phases / total phases) when
        # we move into a new phase, then refined as the phase progresses.
        rec["percent"] = _compute_percent_locked(rec)


def increment(token: str, *, n: int = 1) -> None:
    """Increment ``completed`` by ``n`` (typically called from worker
    threads after each sub-task finishes)."""
    if not is_valid_token(token):
        return
    now_ts = time.time()
    now_iso = _now_iso()
    with _LOCK:
        rec = _PROGRESS.get(token)
        if rec is None or rec.get("status") != "running":
            return
        new_completed = int(rec.get("completed") or 0) + int(n)
        rec["completed"] = new_completed
        rec["last_update_at_iso"] = now_iso
        rec["last_update_ts"] = now_ts
        # Monotonic-percent guard: never let percent regress within the
        # same phase.
        new_pct = _compute_percent_locked(rec)
        if new_pct >= int(rec.get("percent") or 0):
            rec["percent"] = new_pct


def set_progress(token: str, completed: int, total: Optional[int]) -> None:
    """Absolute set of ``completed`` / ``total`` (used as the callback
    handed into a per-region worker fan-out)."""
    if not is_valid_token(token):
        return
    now_ts = time.time()
    now_iso = _now_iso()
    with _LOCK:
        rec = _PROGRESS.get(token)
        if rec is None or rec.get("status") != "running":
            return
        # Monotonic guards.
        prev_completed = int(rec.get("completed") or 0)
        if completed < prev_completed:
            return
        rec["completed"] = int(completed)
        if total is not None:
            rec["total"] = int(total)
        rec["last_update_at_iso"] = now_iso
        rec["last_update_ts"] = now_ts
        new_pct = _compute_percent_locked(rec)
        if new_pct >= int(rec.get("percent") or 0):
            rec["percent"] = new_pct


def complete(
    token: str,
    *,
    status: str,
    run_id: Optional[str] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> None:
    """Mark a run as finished. ``status`` is "succeeded" or "failed".
    Idempotent — once succeeded, won't be downgraded by a later failed call.
    """
    if not is_valid_token(token):
        return
    if status not in ("succeeded", "failed"):
        return
    now_ts = time.time()
    now_iso = _now_iso()
    with _LOCK:
        rec = _PROGRESS.get(token)
        if rec is None:
            return
        # Don't overwrite a prior success with a later failure (e.g. a
        # finally-block recording failure after main path already
        # recorded success).
        if rec.get("status") == "succeeded" and status == "failed":
            return
        rec["status"] = status
        rec["last_update_at_iso"] = now_iso
        rec["last_update_ts"] = now_ts
        if status == "succeeded":
            rec["percent"] = 100
            rec["completed"] = rec.get("total") or rec.get("completed") or 0
        if run_id is not None:
            rec["run_id"] = run_id
        if error_code is not None:
            rec["error_code"] = error_code
        if error_message is not None:
            rec["error_message"] = error_message


def get(token: str, *, requesting_actor_oid: str, is_admin: bool = False) -> Dict[str, Any]:
    """Returns a JSON-serializable view. If token unknown, returns
    ``{found: False}``. Enforces actor binding: a non-admin requester
    whose oid does not match ``actor_oid`` is treated as "not found"
    (don't even reveal that the token exists).
    """
    if not is_valid_token(token):
        return {"found": False, "reason": "invalid_token"}
    with _LOCK:
        rec = _PROGRESS.get(token)
        if rec is None:
            return {"found": False}
        if not is_admin and rec.get("actor_oid") and rec.get("actor_oid") != requesting_actor_oid:
            return {"found": False, "reason": "actor_mismatch"}
        snapshot = dict(rec)
    # Compute live elapsed and ETA outside the lock.
    now_ts = time.time()
    started_ts = snapshot.get("started_ts") or now_ts
    phase_started_ts = snapshot.get("phase_started_ts") or started_ts
    elapsed = max(0.0, now_ts - started_ts)
    phase_elapsed = max(0.0, now_ts - phase_started_ts)
    snapshot["elapsed_seconds"] = int(elapsed)
    snapshot["phase_elapsed_seconds"] = int(phase_elapsed)
    eta = None
    completed = int(snapshot.get("completed") or 0)
    total = snapshot.get("total")
    if (snapshot.get("status") == "running"
            and isinstance(total, int) and total > 0
            and completed >= _ETA_MIN_COMPLETED
            and phase_elapsed >= _ETA_MIN_ELAPSED_S):
        rate = completed / phase_elapsed  # items per second
        remaining = max(0, total - completed)
        eta = int(remaining / rate) if rate > 0 else None
    snapshot["eta_seconds"] = eta
    snapshot["found"] = True
    # Drop internal-only timestamp epochs from the wire payload.
    snapshot.pop("started_ts", None)
    snapshot.pop("phase_started_ts", None)
    snapshot.pop("last_update_ts", None)
    return snapshot


def reset_for_tests() -> None:
    """Test helper — wipe the store."""
    with _LOCK:
        _PROGRESS.clear()


def _compute_percent_locked(rec: Dict[str, Any]) -> int:
    """Compute overall percent based on phase index + intra-phase progress.
    Each phase contributes ``100/n_phases``%; partial progress within a
    phase contributes proportionally if ``total`` is set.
    """
    phases = rec.get("phases") or []
    n_phases = max(1, len(phases))
    phase_idx = max(0, min(int(rec.get("current_phase_index") or 0), n_phases - 1))
    per_phase = 100.0 / n_phases
    base = per_phase * phase_idx
    total = rec.get("total")
    completed = int(rec.get("completed") or 0)
    if isinstance(total, int) and total > 0:
        intra = per_phase * min(1.0, completed / total)
    else:
        intra = 0.0
    return int(min(99, base + intra))  # cap at 99% until complete() sets 100

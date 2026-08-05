"""Tests for the in-process run progress tracker."""
from __future__ import annotations

import time
import uuid

import pytest

from api._shared import run_progress as rp


@pytest.fixture(autouse=True)
def _reset():
    rp.reset_for_tests()
    yield
    rp.reset_for_tests()


def _tok() -> str:
    return str(uuid.uuid4())


# ---- is_valid_token ----------------------------------------------------

def test_is_valid_token_accepts_uuids():
    assert rp.is_valid_token(str(uuid.uuid4()))
    assert rp.is_valid_token("AAAAAAAA-BBBB-1111-2222-CCCCCCCCCCCC")


def test_is_valid_token_rejects_garbage():
    assert not rp.is_valid_token("")
    assert not rp.is_valid_token("not-a-uuid")
    assert not rp.is_valid_token("12345678-1234-1234-1234-1234567890")  # too short
    assert not rp.is_valid_token("'; DROP TABLE foo --")


# ---- start ------------------------------------------------------------

def test_start_with_invalid_token_is_noop():
    rp.start("not-a-uuid", actor_oid="oid1", phases=["a", "b"])
    # No way to observe state since we never created it; just ensure no
    # crash and that get() returns not found.
    out = rp.get("not-a-uuid", requesting_actor_oid="oid1")
    assert out["found"] is False


def test_start_initializes_record_with_phase_0():
    t = _tok()
    rp.start(t, actor_oid="oid1", phases=["bom", "lr", "ss", "arm"])
    out = rp.get(t, requesting_actor_oid="oid1")
    assert out["found"] is True
    assert out["status"] == "running"
    assert out["phases"] == ["bom", "lr", "ss", "arm"]
    assert out["current_phase_index"] == 0
    assert out["current_phase_label"] == "bom"
    assert out["completed"] == 0
    assert out["total"] is None
    assert out["percent"] == 0
    assert out["actor_oid"] == "oid1"
    assert out["run_id"] is None


# ---- set_phase --------------------------------------------------------

def test_set_phase_updates_index_and_label_and_total():
    t = _tok()
    rp.start(t, actor_oid="oid1", phases=["a", "b", "c", "d"])
    rp.set_phase(t, 2, label="working hard", total=38)
    out = rp.get(t, requesting_actor_oid="oid1")
    assert out["current_phase_index"] == 2
    assert out["current_phase_label"] == "working hard"
    assert out["total"] == 38
    # Percent should be at least 50 (2 of 4 phases done) but less than
    # 75 (next phase boundary).
    assert 50 <= out["percent"] < 75


def test_set_phase_uses_phase_name_when_label_missing():
    t = _tok()
    rp.start(t, actor_oid="oid1", phases=["alpha", "beta", "gamma"])
    rp.set_phase(t, 1)
    out = rp.get(t, requesting_actor_oid="oid1")
    assert out["current_phase_label"] == "beta"


def test_set_phase_refuses_to_go_backward():
    t = _tok()
    rp.start(t, actor_oid="oid1", phases=["a", "b", "c"])
    rp.set_phase(t, 2, label="c")
    rp.set_phase(t, 1, label="back to b")
    out = rp.get(t, requesting_actor_oid="oid1")
    assert out["current_phase_index"] == 2
    assert out["current_phase_label"] == "c"


# ---- increment / set_progress -----------------------------------------

def test_increment_advances_completed_and_percent():
    t = _tok()
    rp.start(t, actor_oid="oid1", phases=["a", "b"])
    rp.set_phase(t, 0, label="a", total=10)
    rp.increment(t, n=5)
    out = rp.get(t, requesting_actor_oid="oid1")
    assert out["completed"] == 5
    # Phase 0 of 2 = 0%; partial intra-phase: 50% × 50% = 25%
    assert 20 <= out["percent"] <= 30


def test_set_progress_absolute_set():
    t = _tok()
    rp.start(t, actor_oid="oid1", phases=["a"])
    rp.set_phase(t, 0, label="a", total=38)
    rp.set_progress(t, 19, 38)
    out = rp.get(t, requesting_actor_oid="oid1")
    assert out["completed"] == 19
    assert out["total"] == 38
    # 1 phase, intra-phase 50% → ~50%, capped at 99.
    assert 40 <= out["percent"] <= 60


def test_set_progress_refuses_to_regress_completed():
    t = _tok()
    rp.start(t, actor_oid="oid1", phases=["a"])
    rp.set_phase(t, 0, label="a", total=38)
    rp.set_progress(t, 30, 38)
    rp.set_progress(t, 10, 38)  # racey late callback
    out = rp.get(t, requesting_actor_oid="oid1")
    assert out["completed"] == 30


def test_percent_capped_at_99_until_complete():
    t = _tok()
    rp.start(t, actor_oid="oid1", phases=["a"])
    rp.set_phase(t, 0, label="a", total=10)
    rp.set_progress(t, 10, 10)
    out = rp.get(t, requesting_actor_oid="oid1")
    assert out["percent"] == 99  # never 100 until complete()


# ---- complete ----------------------------------------------------------

def test_complete_succeeded_sets_status_and_percent():
    t = _tok()
    rp.start(t, actor_oid="oid1", phases=["a"])
    rp.complete(t, status="succeeded", run_id="run-xyz")
    out = rp.get(t, requesting_actor_oid="oid1")
    assert out["status"] == "succeeded"
    assert out["percent"] == 100
    assert out["run_id"] == "run-xyz"


def test_complete_failed_carries_error_fields():
    t = _tok()
    rp.start(t, actor_oid="oid1", phases=["a"])
    rp.complete(t, status="failed", error_code="bad_stuff",
                error_message="something broke")
    out = rp.get(t, requesting_actor_oid="oid1")
    assert out["status"] == "failed"
    assert out["error_code"] == "bad_stuff"
    assert out["error_message"] == "something broke"


def test_complete_failed_does_not_downgrade_succeeded():
    """If a finally block fires complete(failed) after we already
    recorded success, the success must stick — otherwise long-tail
    cleanup errors look like the run failed when it didn't."""
    t = _tok()
    rp.start(t, actor_oid="oid1", phases=["a"])
    rp.complete(t, status="succeeded", run_id="run-xyz")
    rp.complete(t, status="failed", error_code="late_cleanup_oops")
    out = rp.get(t, requesting_actor_oid="oid1")
    assert out["status"] == "succeeded"
    assert out["run_id"] == "run-xyz"


def test_complete_with_invalid_status_is_noop():
    t = _tok()
    rp.start(t, actor_oid="oid1", phases=["a"])
    rp.complete(t, status="canceled")
    out = rp.get(t, requesting_actor_oid="oid1")
    assert out["status"] == "running"


# ---- get / actor binding ----------------------------------------------

def test_get_returns_not_found_for_unknown_token():
    t = _tok()
    out = rp.get(t, requesting_actor_oid="oid1")
    assert out["found"] is False


def test_get_blocks_non_owner_non_admin():
    t = _tok()
    rp.start(t, actor_oid="owner-oid", phases=["a"])
    out = rp.get(t, requesting_actor_oid="other-oid")
    assert out["found"] is False
    assert out.get("reason") == "actor_mismatch"


def test_get_allows_admin_to_see_other_users_progress():
    t = _tok()
    rp.start(t, actor_oid="owner-oid", phases=["a"])
    out = rp.get(t, requesting_actor_oid="admin-oid", is_admin=True)
    assert out["found"] is True
    assert out["actor_oid"] == "owner-oid"


def test_get_returns_invalid_token_reason_for_garbage():
    out = rp.get("not-a-uuid", requesting_actor_oid="oid1")
    assert out["found"] is False
    assert out.get("reason") == "invalid_token"


# ---- ETA gating -------------------------------------------------------

def test_eta_hidden_when_too_few_completed():
    t = _tok()
    rp.start(t, actor_oid="oid1", phases=["a"])
    rp.set_phase(t, 0, label="a", total=38)
    rp.set_progress(t, 2, 38)  # below _ETA_MIN_COMPLETED=4
    time.sleep(0.05)
    out = rp.get(t, requesting_actor_oid="oid1")
    assert out["eta_seconds"] is None


def test_eta_appears_after_enough_samples_and_elapsed():
    t = _tok()
    rp.start(t, actor_oid="oid1", phases=["a"])
    rp.set_phase(t, 0, label="a", total=38)
    # Backdate phase_started_ts by 20s and post >=4 completions so both
    # gates pass.
    with rp._LOCK:
        rec = rp._PROGRESS[t]
        rec["phase_started_ts"] = time.time() - 20
        rec["started_ts"] = time.time() - 20
    rp.set_progress(t, 4, 38)
    out = rp.get(t, requesting_actor_oid="oid1")
    assert out["eta_seconds"] is not None
    # rate = 4/20 = 0.2 items/s, remaining = 34, eta ~= 170s
    assert 100 <= out["eta_seconds"] <= 250


def test_eta_none_after_complete():
    t = _tok()
    rp.start(t, actor_oid="oid1", phases=["a"])
    rp.set_phase(t, 0, label="a", total=38)
    rp.complete(t, status="succeeded", run_id="x")
    out = rp.get(t, requesting_actor_oid="oid1")
    assert out["eta_seconds"] is None


# ---- LRU eviction -----------------------------------------------------

def test_lru_eviction_caps_record_count():
    # Force capacity small to keep the test fast.
    rp._MAX_RECORDS  # sanity check the constant exists
    tokens = []
    for _ in range(rp._MAX_RECORDS + 10):
        t = _tok()
        rp.start(t, actor_oid="oid1", phases=["a"])
        # Stagger last_update_ts so the oldest are evicted first.
        with rp._LOCK:
            rec = rp._PROGRESS.get(t)
            if rec:
                rec["last_update_ts"] = time.time()
                time.sleep(0.001)
        tokens.append(t)
    with rp._LOCK:
        assert len(rp._PROGRESS) <= rp._MAX_RECORDS
    # The OLDEST tokens should be gone.
    out_first = rp.get(tokens[0], requesting_actor_oid="oid1")
    out_last = rp.get(tokens[-1], requesting_actor_oid="oid1")
    assert out_first["found"] is False
    assert out_last["found"] is True


# ---- elapsed_seconds --------------------------------------------------

def test_elapsed_seconds_advances():
    t = _tok()
    rp.start(t, actor_oid="oid1", phases=["a"])
    with rp._LOCK:
        rp._PROGRESS[t]["started_ts"] = time.time() - 12
    out = rp.get(t, requesting_actor_oid="oid1")
    assert out["elapsed_seconds"] >= 10

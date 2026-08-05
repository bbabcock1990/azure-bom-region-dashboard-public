"""Unit tests for activity_log — no network, no real Table Storage."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from _shared import activity_log


# ---- token_fingerprint -----------------------------------------------------

def test_token_fingerprint_empty():
    assert activity_log.token_fingerprint(None) == "(none)"
    assert activity_log.token_fingerprint("") == "(none)"


def test_token_fingerprint_normal():
    fp = activity_log.token_fingerprint("eyJhbGciOiJIUzI1NiJ9.payload.sig")
    assert fp.startswith("eyJhbGci")
    assert "(len=" in fp
    # Never echoes the full token
    assert "payload" not in fp
    assert "sig" not in fp


def test_token_fingerprint_strips_bearer_prefix():
    fp = activity_log.token_fingerprint("Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
    assert fp.startswith("eyJhbGci")
    # Length should be the bare token, not "Bearer "+token
    assert "(len=32)" in fp


# ---- _redact ---------------------------------------------------------------

def test_redact_token_keys():
    data = {
        "endpoint": "https://example/api",
        "arm_token": "super-secret-jwt",
        "Authorization": "Bearer abc123",
        "nested": {"api_key": "k", "value": 1},
        "items": [{"region": "eastus"}, {"region": "westus"}],
    }
    out = activity_log._redact(data)
    assert out["endpoint"] == "https://example/api"
    # Default-deny: sensitive-keyed values get full <redacted>, NOT a prefix.
    assert out["arm_token"] == "<redacted>"
    assert out["Authorization"] == "<redacted>"
    # Raw token values never appear in the redacted payload.
    serialized = json.dumps(out)
    assert "super-secret-jwt" not in serialized
    assert "abc123" not in serialized
    assert out["nested"]["api_key"] == "<redacted>"
    assert out["nested"]["value"] == 1  # untouched
    # Non-sensitive-keyed list of dicts is descended into normally.
    assert out["items"][0]["region"] == "eastus"
    assert out["items"][1]["region"] == "westus"


def test_redact_redacts_whole_container_under_sensitive_key():
    """If you name a container with a sensitive substring, the SAFEST
    choice is to redact it wholesale — don't recurse and risk leaking
    a nested value because the parent's semantics implied secrecy."""
    out = activity_log._redact({
        "secret_payload": {"nested": "should-not-appear"},
        "credentials_list": [{"username": "u", "password": "p"}],
    })
    assert out["secret_payload"] == "<redacted>"
    assert out["credentials_list"] == "<redacted>"
    j = json.dumps(out)
    assert "should-not-appear" not in j
    assert "username" not in j
    assert "password" not in j


def test_redact_passes_through_pre_fingerprinted_values():
    """Caller may opt in to a diagnostic fingerprint by calling
    token_fingerprint() explicitly; _redact must preserve such values
    so the explicit-fingerprint path keeps working."""
    fp = activity_log.token_fingerprint("eyJhbGciOiJIUzI1NiJ9.payload.sig")
    out = activity_log._redact({"arm_token": fp})
    assert out["arm_token"] == fp  # passed through, not over-redacted


def test_redact_case_insensitive_substring():
    out = activity_log._redact({
        "ARM_TOKEN": "x",
        "myPassword": "y",
        "session_cookie": "z",
        "client_credential_id": "c",
        "normal_field": "ok",
    })
    assert out["normal_field"] == "ok"
    for k in ("ARM_TOKEN", "myPassword", "session_cookie", "client_credential_id"):
        assert out[k] == "<redacted>"


def test_redact_depth_guard():
    # Build a 10-deep structure; redact bails at depth 6 with a marker.
    deep = current = {}
    for _ in range(10):
        current["next"] = {}
        current = current["next"]
    current["leaf"] = "value"
    out = activity_log._redact(deep)
    s = json.dumps(out)
    assert "<truncated:depth>" in s


# ---- _encode_details -------------------------------------------------------

def test_encode_details_none():
    assert activity_log._encode_details(None) is None
    assert activity_log._encode_details({}) is None


def test_encode_details_normal_roundtrip():
    encoded = activity_log._encode_details({"a": 1, "b": [1, 2, 3]})
    parsed = json.loads(encoded)
    assert parsed == {"a": 1, "b": [1, 2, 3]}


def test_encode_details_truncation_preserves_valid_json():
    huge = {"x": "A" * (50 * 1024)}
    encoded = activity_log._encode_details(huge)
    parsed = json.loads(encoded)  # MUST still be valid JSON
    assert parsed["truncated"] is True
    assert "preview" in parsed
    assert parsed["original_bytes"] > 50_000


def test_encode_details_handles_non_serializable():
    class X:
        def __repr__(self):
            return "<X-instance>"
    encoded = activity_log._encode_details({"obj": X()})
    parsed = json.loads(encoded)
    # default=str makes it serializable
    assert "X-instance" in encoded or "obj" in parsed


# ---- record() best-effort behavior -----------------------------------------

def test_record_swallows_storage_errors(monkeypatch):
    """record() must NEVER raise — a storage hiccup must not break a run."""
    def boom(*args, **kwargs):
        raise RuntimeError("simulated table storage outage")
    monkeypatch.setattr(activity_log.storage, "get_table_client", boom)

    # Should return None silently.
    result = activity_log.record(
        "analysis_start",
        actor_email="t@example.com",
        subscription_id="sub-1",
        message="test",
    )
    assert result is None


def test_record_passes_redacted_entity_to_table(monkeypatch):
    captured = {}
    fake_table = MagicMock()
    fake_table.create_entity = MagicMock(
        side_effect=lambda entity: captured.setdefault("entity", entity)
    )
    monkeypatch.setattr(activity_log.storage, "get_table_client",
                        lambda name: fake_table)

    activity_log.record(
        "arm_call_start",
        actor_email="t@example.com",
        subscription_id="00000000-0000-0000-0000-000000000001",
        run_id="run-abc",
        api_scope="global",
        message="Calling ARM",
        details={
            "arm_token": "raw-secret-jwt",  # leaked-by-caller scenario
            "token_fp": activity_log.token_fingerprint("eyJhbGciOiJIUzI1NiJ9.payload.sig"),
            "regions": ["eastus"],
        },
    )
    e = captured["entity"]
    assert e["event_type"] == "arm_call_start"
    assert e["api_scope"] == "global"
    assert e["subscription_id"] == "00000000-0000-0000-0000-000000000001"
    assert e["run_id"] == "run-abc"
    assert "PartitionKey" in e and "RowKey" in e
    # PartitionKey is a YYYY-MM-DD date
    assert len(e["PartitionKey"]) == 10 and e["PartitionKey"][4] == "-"
    # Raw token must NEVER appear in the persisted entity.
    assert "raw-secret-jwt" not in json.dumps(e)
    # Pre-fingerprinted diagnostic value still flows through.
    assert "eyJhbGci" in e["details_json"]
    # Non-sensitive fields preserved.
    assert "eastus" in e["details_json"]


def test_record_caps_message_length(monkeypatch):
    captured = {}
    fake_table = MagicMock()
    fake_table.create_entity = MagicMock(
        side_effect=lambda entity: captured.setdefault("entity", entity)
    )
    monkeypatch.setattr(activity_log.storage, "get_table_client",
                        lambda name: fake_table)

    long_msg = "x" * 10_000
    activity_log.record("analysis_start", message=long_msg)
    assert len(captured["entity"]["message"]) <= activity_log._MESSAGE_MAX_CHARS


def test_record_rowkey_orders_newest_first(monkeypatch):
    """RowKey ASC within a partition should sort newest-first."""
    captured = []
    fake_table = MagicMock()
    fake_table.create_entity = MagicMock(side_effect=lambda entity: captured.append(entity))
    monkeypatch.setattr(activity_log.storage, "get_table_client",
                        lambda name: fake_table)

    import time as _t
    activity_log.record("analysis_start", message="first")
    _t.sleep(0.01)
    activity_log.record("analysis_complete", message="second")

    # Same partition (same day)
    assert captured[0]["PartitionKey"] == captured[1]["PartitionKey"]
    # ASC string sort of RowKey should put the SECOND (newer) row first
    by_rowkey = sorted(captured, key=lambda e: e["RowKey"])
    assert by_rowkey[0]["message"] == "second"
    assert by_rowkey[1]["message"] == "first"


# ---- query() filtering -----------------------------------------------------

def _make_fake_table(entities):
    fake_table = MagicMock()

    def query_entities(query_filter=None, parameters=None):
        pk = (parameters or {}).get("pk")
        return [e for e in entities if e["PartitionKey"] == pk]

    fake_table.query_entities = query_entities
    return fake_table


def test_query_applies_filters(monkeypatch):
    today = activity_log._now_utc().strftime("%Y-%m-%d")
    entities = [
        {"PartitionKey": today, "RowKey": "001", "timestamp_iso": "2026-05-19T10:00:00",
         "event_type": "arm_call_start", "actor_oid": "oid-a",
         "subscription_id": "sub-1", "message": "a"},
        {"PartitionKey": today, "RowKey": "002", "timestamp_iso": "2026-05-19T10:01:00",
         "event_type": "arm_call_ok", "actor_oid": "oid-a",
         "subscription_id": "sub-1", "message": "b"},
        {"PartitionKey": today, "RowKey": "003", "timestamp_iso": "2026-05-19T10:02:00",
         "event_type": "arm_call_start", "actor_oid": "oid-b",
         "subscription_id": "sub-2", "message": "c"},
    ]
    fake = _make_fake_table(entities)
    monkeypatch.setattr(activity_log.storage, "get_table_client", lambda name: fake)

    by_sub = activity_log.query(subscription_id="sub-1", max_days=1)
    assert {e["message"] for e in by_sub} == {"a", "b"}

    by_event = activity_log.query(event_type="arm_call_start", max_days=1)
    assert {e["message"] for e in by_event} == {"a", "c"}

    by_actor = activity_log.query(actor_oid="oid-a", max_days=1)
    assert {e["message"] for e in by_actor} == {"a", "b"}

    combined = activity_log.query(actor_oid="oid-a", event_type="arm_call_ok",
                                  subscription_id="sub-1", max_days=1)
    assert [e["message"] for e in combined] == ["b"]


def test_query_respects_limit(monkeypatch):
    today = activity_log._now_utc().strftime("%Y-%m-%d")
    entities = [
        {"PartitionKey": today, "RowKey": f"{i:03d}", "timestamp_iso": "2026-05-19T10:00:00",
         "event_type": "analysis_start", "actor_oid": "oid-a",
         "subscription_id": "sub-1", "message": f"m{i}"}
        for i in range(50)
    ]
    fake = _make_fake_table(entities)
    monkeypatch.setattr(activity_log.storage, "get_table_client", lambda name: fake)
    out = activity_log.query(limit=10, max_days=1)
    assert len(out) == 10


def test_query_handles_storage_open_failure(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("azurite down")
    monkeypatch.setattr(activity_log.storage, "get_table_client", boom)
    assert activity_log.query() == []


# ---- timestamp normalization -----------------------------------------------
# The azure-data-tables Python SDK auto-deserializes ISO 8601 string
# properties into ``datetime`` objects, which would then JSON-serialize in
# the runtime's *local* timezone. Guard against that by normalizing on read.

def test_normalize_timestamp_handles_naive_datetime():
    # Naive datetimes (treated as UTC by our normalizer)
    dt = datetime(2026, 5, 19, 16, 35, 34)
    assert activity_log._normalize_timestamp_iso(dt) == "2026-05-19T16:35:34Z"


def test_normalize_timestamp_handles_aware_datetime_in_local_tz():
    # Simulate what the SDK gives us: a tz-aware datetime in a non-UTC offset.
    from datetime import timedelta
    cdt = timezone(timedelta(hours=-5))
    dt = datetime(2026, 5, 19, 11, 35, 34, tzinfo=cdt)  # 11:35 CDT == 16:35 UTC
    assert activity_log._normalize_timestamp_iso(dt) == "2026-05-19T16:35:34Z"


def test_normalize_timestamp_passes_string_through():
    # If storage gave us a plain string, leave it alone.
    assert (
        activity_log._normalize_timestamp_iso("2026-05-19T16:35:34Z")
        == "2026-05-19T16:35:34Z"
    )


def test_normalize_timestamp_handles_none():
    assert activity_log._normalize_timestamp_iso(None) is None

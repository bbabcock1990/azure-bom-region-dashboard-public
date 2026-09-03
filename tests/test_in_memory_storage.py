"""Tests for the zero-disk in-memory storage backend + browser state sync.

In the hosted stateless deployment the server writes NOTHING to disk: each
signed-in customer gets an isolated in-memory store keyed by their user key,
and the durable copy lives in their browser (exported/imported as JSON). These
tests cover the in-memory backend, per-user isolation, and export/import
round-trip fidelity (tables + blobs).
"""
from __future__ import annotations

import os

import pytest

from api._shared import auth_token
from api._shared import storage


@pytest.fixture(autouse=True)
def _mem(monkeypatch):
    monkeypatch.setenv("IN_MEMORY_STORAGE", "true")
    # Reset module-level in-memory stores between tests.
    for c in list(storage._mem_conns.values()):
        try:
            c.close()
        except Exception:
            pass
    storage._mem_conns.clear()
    storage._mem_blobs.clear()
    storage._mem_last_seen.clear()
    storage._last_evict_ts = 0.0
    auth_token.reset_for_tests()
    yield
    for c in list(storage._mem_conns.values()):
        try:
            c.close()
        except Exception:
            pass
    storage._mem_conns.clear()
    storage._mem_blobs.clear()
    storage._mem_last_seen.clear()
    storage._last_evict_ts = 0.0
    auth_token.reset_for_tests()


def _bind(user_key):
    auth_token.set_request_context(arm_token=None, user_key=user_key)


def test_in_memory_writes_no_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    _bind("alice@example.com")
    t = storage.get_table_client("boms")
    t.upsert_entity({"PartitionKey": "bom", "RowKey": "1", "name": "Alice"})
    storage.get_blob_container("snapshots").upload_blob(
        "run1.json", '{"x":1}', overwrite=True
    )
    auth_token.clear_request_context()
    # No app.db and no blob files should have been written under the root.
    assert not (tmp_path / "app.db").exists()
    for _root, _dirs, files in os.walk(tmp_path):
        assert not any(f.endswith(".json") for f in files)


def test_per_user_isolation():
    _bind("alice@example.com")
    storage.get_table_client("boms").upsert_entity(
        {"PartitionKey": "bom", "RowKey": "1", "name": "Alice"}
    )
    auth_token.clear_request_context()

    _bind("bob@example.com")
    assert storage.get_table_client("boms").list_entities() == []
    auth_token.clear_request_context()

    _bind("alice@example.com")
    rows = storage.get_table_client("boms").list_entities()
    assert len(rows) == 1 and rows[0]["name"] == "Alice"
    auth_token.clear_request_context()


def test_export_import_round_trip():
    _bind("alice@example.com")
    storage.get_table_client("boms").upsert_entity(
        {"PartitionKey": "bom", "RowKey": "1", "name": "Alice", "skus": 3}
    )
    storage.get_blob_container("snapshots").upload_blob(
        "run1/data.json", '{"hello":"world"}', overwrite=True
    )
    doc = storage.export_state()
    auth_token.clear_request_context()

    assert doc["v"] == 1
    assert "tbl_boms" in doc["tables"]
    assert doc["blobs"]["snapshots"]["run1/data.json"]

    # A different user imports Alice's exported document into their own store.
    _bind("bob@example.com")
    summary = storage.import_state(doc)
    assert summary["rows"] == 1 and summary["blobs"] == 1
    rows = storage.get_table_client("boms").list_entities()
    assert len(rows) == 1 and rows[0]["skus"] == 3
    blob = storage.get_blob_container("snapshots").download_blob("run1/data.json").readall()
    assert blob == b'{"hello":"world"}'
    auth_token.clear_request_context()


def test_import_replaces_existing_state():
    _bind("alice@example.com")
    tbl = storage.get_table_client("boms")
    tbl.upsert_entity({"PartitionKey": "bom", "RowKey": "old", "name": "Old"})
    doc = storage.export_state()
    # Mutate, then import the earlier snapshot — the new row must be gone.
    tbl.upsert_entity({"PartitionKey": "bom", "RowKey": "new", "name": "New"})
    assert len(tbl.list_entities()) == 2
    storage.import_state(doc)
    rows = storage.get_table_client("boms").list_entities()
    assert [r["RowKey"] for r in rows] == ["old"]
    auth_token.clear_request_context()


def test_clear_state():
    _bind("alice@example.com")
    storage.get_table_client("boms").upsert_entity(
        {"PartitionKey": "bom", "RowKey": "1", "name": "Alice"}
    )
    storage.clear_state()
    assert storage.get_table_client("boms").list_entities() == []
    auth_token.clear_request_context()


def test_import_rejects_bad_table_names():
    _bind("alice@example.com")
    doc = {
        "v": 1,
        "tables": {"DROP TABLE x": [["p", "r", "{}"]], "tbl_ok": [["p", "r", "{}"]]},
        "blobs": {},
    }
    summary = storage.import_state(doc)
    assert summary["tables"] == 1  # only tbl_ok accepted
    auth_token.clear_request_context()


def test_lru_cap_bounds_memory(monkeypatch):
    # Cap RAM to 2 customer stores; a third sign-in evicts the least-recent.
    monkeypatch.setenv("IN_MEMORY_MAX_USERS", "2")
    for name in ("u1", "u2", "u3", "u4"):
        _bind(name)
        storage.get_table_client("boms").upsert_entity(
            {"PartitionKey": "bom", "RowKey": "1", "name": name}
        )
        auth_token.clear_request_context()
    # Never more than the cap of live connections.
    assert len(storage._mem_conns) <= 2
    # The most-recent user survives; the oldest was evicted (non-destructive —
    # it would re-hydrate from that user's browser on their next visit).
    _bind("u4")
    assert storage.get_table_client("boms").list_entities()[0]["name"] == "u4"
    auth_token.clear_request_context()
    _bind("u1")
    assert storage.get_table_client("boms").list_entities() == []
    auth_token.clear_request_context()


def test_idle_ttl_evicts_stale_store(monkeypatch):
    monkeypatch.setenv("IN_MEMORY_TTL_SECONDS", "1")
    _bind("stale@example.com")
    storage.get_table_client("boms").upsert_entity(
        {"PartitionKey": "bom", "RowKey": "1", "name": "Stale"}
    )
    auth_token.clear_request_context()
    # Age the last-seen stamp beyond the TTL and force the throttled sweep.
    storage._mem_last_seen["stale@example.com"] = 0.0
    storage._last_evict_ts = 0.0
    _bind("active@example.com")
    storage.get_table_client("boms").list_entities()  # triggers the sweep
    auth_token.clear_request_context()
    assert "stale@example.com" not in storage._mem_conns

"""Tests for GET/POST /api/bom/zonal-verifications (verify-all persistence).

These lock in the round-trip: a POST persists per-region live-verification
results keyed by run_id + subscription, and a subsequent GET returns them.
The scan itself is read-only; this endpoint only stores/returns the results.
"""
from fastapi.testclient import TestClient


RUN = "2024-01-01T00-00-00Z-abcd"
SUB = "11111111-2222-3333-4444-555555555555"


def _results():
    return {
        "eastus": {
            "map": {"Azure Blob Storage||zrs": {"verdict": "available", "name": "Azure Blob Storage", "tier": "zrs"}},
            "ts": "2024-01-01 00:00 UTC",
        }
    }


def test_get_empty_when_nothing_persisted(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)
    from server.app import app

    client = TestClient(app)
    res = client.get(f"/api/bom/zonal-verifications?run_id={RUN}&subscription_id={SUB}")
    assert res.status_code == 200
    assert res.json() == {"results": {}}


def test_post_then_get_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)
    from server.app import app

    client = TestClient(app)
    post = client.post(
        "/api/bom/zonal-verifications",
        json={"run_id": RUN, "subscription_id": SUB, "results": _results()},
    )
    assert post.status_code == 200
    assert post.json() == {"ok": True, "regions": 1}

    got = client.get(f"/api/bom/zonal-verifications?run_id={RUN}&subscription_id={SUB}")
    assert got.status_code == 200
    assert got.json()["results"] == _results()


def test_get_requires_both_params(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)
    from server.app import app

    client = TestClient(app)
    res = client.get(f"/api/bom/zonal-verifications?run_id={RUN}")
    assert res.status_code == 400


def test_post_rejects_non_object_results(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)
    from server.app import app

    client = TestClient(app)
    res = client.post(
        "/api/bom/zonal-verifications",
        json={"run_id": RUN, "subscription_id": SUB, "results": ["not", "an", "object"]},
    )
    assert res.status_code == 400


def test_scoped_by_subscription(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)
    from server.app import app

    client = TestClient(app)
    client.post(
        "/api/bom/zonal-verifications",
        json={"run_id": RUN, "subscription_id": SUB, "results": _results()},
    )
    other = client.get(f"/api/bom/zonal-verifications?run_id={RUN}&subscription_id=99999999-0000-0000-0000-000000000000")
    assert other.status_code == 200
    assert other.json() == {"results": {}}

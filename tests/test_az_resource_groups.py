"""Tests for GET/POST /api/az/resource-groups (list + idempotent create).

httpx is mocked so no ARM call is made. The create endpoint is the single
write this feature performs; these tests lock in the idempotent GET-then-PUT
behaviour and input validation.
"""
import httpx
from fastapi.testclient import TestClient


def _install_fake_client(monkeypatch, mod, *, get_status, get_payload,
                         put_status=201, put_payload=None):
    calls = {"gets": [], "puts": []}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, *, params=None, headers=None):
            calls["gets"].append(url)
            return httpx.Response(get_status, json=get_payload,
                                  request=httpx.Request("GET", url))

        def put(self, url, *, params=None, headers=None, json=None):
            calls["puts"].append({"url": url, "body": json})
            return httpx.Response(put_status, json=put_payload or {},
                                  request=httpx.Request("PUT", url))

    monkeypatch.setattr(mod.httpx, "Client", FakeClient)
    return calls


def _patch_token(monkeypatch, mod):
    class _TokenInfo:
        token = "token-123"
    monkeypatch.setattr(mod.auth_token, "get_arm_token",
                        lambda subscription_id: _TokenInfo(), raising=False)


SUB = "11111111-2222-3333-4444-555555555555"


def test_list_resource_groups(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)
    from api import az_resource_groups as mod
    from server.app import app

    _patch_token(monkeypatch, mod)
    _install_fake_client(
        monkeypatch, mod, get_status=200,
        get_payload={"value": [
            {"name": "beta-rg", "location": "westus"},
            {"name": "alpha-rg", "location": "eastus"},
        ]},
    )
    client = TestClient(app)
    res = client.get(f"/api/az/resource-groups?subscription_id={SUB}")
    assert res.status_code == 200
    body = res.json()
    # sorted case-insensitively by name
    assert [g["name"] for g in body["resource_groups"]] == ["alpha-rg", "beta-rg"]


def test_create_resource_group_new(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)
    from api import az_resource_groups as mod
    from server.app import app

    _patch_token(monkeypatch, mod)
    calls = _install_fake_client(
        monkeypatch, mod, get_status=404, get_payload={},
        put_status=201, put_payload={"name": "rg-bom-validation", "location": "eastus"},
    )
    client = TestClient(app)
    res = client.post("/api/az/resource-groups",
                      json={"subscription_id": SUB, "name": "rg-bom-validation", "location": "eastus"})
    assert res.status_code == 201
    body = res.json()
    assert body["created"] is True
    assert body["name"] == "rg-bom-validation"
    assert body["location"] == "eastus"
    assert len(calls["puts"]) == 1
    assert calls["puts"][0]["body"] == {"location": "eastus"}


def test_create_resource_group_idempotent_when_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)
    from api import az_resource_groups as mod
    from server.app import app

    _patch_token(monkeypatch, mod)
    calls = _install_fake_client(
        monkeypatch, mod, get_status=200,
        get_payload={"name": "rg-bom-validation", "location": "westus3"},
    )
    client = TestClient(app)
    res = client.post("/api/az/resource-groups",
                      json={"subscription_id": SUB, "name": "rg-bom-validation", "location": "eastus"})
    assert res.status_code == 200
    body = res.json()
    assert body["created"] is False
    # existing location is preserved, no PUT issued
    assert body["location"] == "westus3"
    assert calls["puts"] == []


def test_create_resource_group_rejects_bad_name(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)
    from api import az_resource_groups as mod
    from server.app import app

    _patch_token(monkeypatch, mod)
    _install_fake_client(monkeypatch, mod, get_status=404, get_payload={})
    client = TestClient(app)
    res = client.post("/api/az/resource-groups",
                      json={"subscription_id": SUB, "name": "bad name!", "location": "eastus"})
    assert res.status_code == 400
    assert res.json()["error"] == "bad_name"


def test_create_resource_group_requires_location(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)
    from api import az_resource_groups as mod
    from server.app import app

    _patch_token(monkeypatch, mod)
    _install_fake_client(monkeypatch, mod, get_status=404, get_payload={})
    client = TestClient(app)
    res = client.post("/api/az/resource-groups",
                      json={"subscription_id": SUB, "name": "rg-bom-validation"})
    assert res.status_code == 400
    assert res.json()["error"] == "no_location"

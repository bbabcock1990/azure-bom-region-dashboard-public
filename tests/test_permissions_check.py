"""Tests for GET /api/permissions/check.

httpx is mocked so no ARM call is made. These lock in the RBAC wildcard
evaluation (a Reader-style role satisfies the required read capabilities but not
the optional writes) and the honest error when the caller can't read their own
permissions.
"""
import httpx
from fastapi.testclient import TestClient

SUB = "11111111-2222-3333-4444-555555555555"


def _install_fake_client(monkeypatch, mod, *, status, payload):
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, *, params=None, headers=None):
            return httpx.Response(status, json=payload,
                                  request=httpx.Request("GET", url))

    monkeypatch.setattr(mod.httpx, "Client", FakeClient)


def _patch_token(monkeypatch, mod):
    class _TokenInfo:
        token = "token-123"
    monkeypatch.setattr(mod.auth_token, "get_arm_token",
                        lambda subscription_id: _TokenInfo(), raising=False)


def test_reader_role_satisfies_required_not_optional(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)
    from api import permissions_check as mod
    from server.app import app

    _patch_token(monkeypatch, mod)
    # Reader = */read only.
    _install_fake_client(monkeypatch, mod, status=200,
                         payload={"value": [{"actions": ["*/read"], "notActions": []}]})
    client = TestClient(app)
    res = client.get(f"/api/permissions/check?subscription_id={SUB}")
    assert res.status_code == 200
    body = res.json()
    caps = {c["key"]: c for c in body["capabilities"]}
    # required reads granted
    assert caps["list_subscriptions"]["granted"] is True
    assert caps["read_skus"]["granted"] is True
    assert caps["read_usage"]["granted"] is True
    # optional writes NOT granted by a read-only role
    assert caps["create_resource_group"]["granted"] is False
    assert caps["manage_support_tickets"]["granted"] is False  # needs write + read
    assert caps["register_providers"]["granted"] is False       # register/action, not */read
    assert body["summary"]["all_required_ok"] is True
    assert body["summary"]["optional_ok"] < body["summary"]["optional_total"]


def test_owner_wildcard_grants_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)
    from api import permissions_check as mod
    from server.app import app

    _patch_token(monkeypatch, mod)
    _install_fake_client(monkeypatch, mod, status=200,
                         payload={"value": [{"actions": ["*"], "notActions": []}]})
    client = TestClient(app)
    body = client.get(f"/api/permissions/check?subscription_id={SUB}").json()
    assert all(c["granted"] for c in body["capabilities"])
    assert body["summary"]["all_required_ok"] is True
    assert body["summary"]["optional_ok"] == body["summary"]["optional_total"]


def test_notactions_subtracts_capability(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)
    from api import permissions_check as mod
    from server.app import app

    _patch_token(monkeypatch, mod)
    # Owner-ish but Support tickets explicitly excluded.
    _install_fake_client(
        monkeypatch, mod, status=200,
        payload={"value": [{"actions": ["*"], "notActions": ["Microsoft.Support/*"]}]})
    client = TestClient(app)
    body = client.get(f"/api/permissions/check?subscription_id={SUB}").json()
    caps = {c["key"]: c for c in body["capabilities"]}
    assert caps["manage_support_tickets"]["granted"] is False
    assert caps["read_skus"]["granted"] is True


def test_forbidden_when_permissions_unreadable(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)
    from api import permissions_check as mod
    from server.app import app

    _patch_token(monkeypatch, mod)
    _install_fake_client(monkeypatch, mod, status=403, payload={})
    client = TestClient(app)
    res = client.get(f"/api/permissions/check?subscription_id={SUB}")
    assert res.status_code == 403
    assert res.json()["error"] == "forbidden"


def test_missing_subscription_id(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)
    from server.app import app

    client = TestClient(app)
    res = client.get("/api/permissions/check")
    assert res.status_code == 400
    assert res.json()["error"] == "no_subscription"

import httpx
from fastapi.testclient import TestClient


def _install_fake_client(monkeypatch, mod, *, status, payload):
    calls = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            calls["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, *, params=None, headers=None):
            calls["url"] = url
            calls["params"] = params
            calls["headers"] = headers
            return httpx.Response(
                status,
                json=payload,
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(mod.httpx, "AsyncClient", FakeAsyncClient)
    return calls


def test_register_provider_starts_registration(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)

    from api import register_provider as mod
    from server.app import app

    class _TokenInfo:
        token = "token-123"

    monkeypatch.setattr(
        mod.auth_token, "get_arm_token", lambda subscription_id: _TokenInfo(),
        raising=False,
    )
    calls = _install_fake_client(
        monkeypatch, mod, status=200,
        payload={"registrationState": "Registering",
                 "namespace": "Microsoft.ContainerStorage"},
    )

    client = TestClient(app)
    res = client.post(
        "/api/providers/register",
        json={
            "subscription_id": "11111111-2222-3333-4444-555555555555",
            "provider": "Microsoft.ContainerStorage",
        },
    )

    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "registering"
    assert body["provider"] == "Microsoft.ContainerStorage"
    assert body["registration_state"] == "Registering"
    assert calls["params"] == {"api-version": "2021-04-01"}
    assert calls["url"].endswith(
        "/subscriptions/11111111-2222-3333-4444-555555555555/providers/"
        "Microsoft.ContainerStorage/register"
    )
    assert calls["headers"]["Authorization"] == "Bearer token-123"


def test_register_provider_403_returns_cli_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)

    from api import register_provider as mod
    from server.app import app

    class _TokenInfo:
        token = "token-123"

    monkeypatch.setattr(
        mod.auth_token, "get_arm_token", lambda subscription_id: _TokenInfo(),
        raising=False,
    )
    _install_fake_client(
        monkeypatch, mod, status=403,
        payload={"error": {"code": "AuthorizationFailed",
                           "message": "does not have authorization"}},
    )

    client = TestClient(app)
    res = client.post(
        "/api/providers/register",
        json={
            "subscription_id": "11111111-2222-3333-4444-555555555555",
            "provider": "Microsoft.ContainerStorage",
        },
    )

    assert res.status_code == 403
    body = res.json()
    assert body["error"] == "forbidden"
    assert body["provider"] == "Microsoft.ContainerStorage"
    assert body["cli_command"] == (
        "az provider register --namespace Microsoft.ContainerStorage "
        "--subscription 11111111-2222-3333-4444-555555555555"
    )


def test_register_provider_rejects_bad_namespace(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)

    from server.app import app

    client = TestClient(app)
    res = client.post(
        "/api/providers/register",
        json={
            "subscription_id": "11111111-2222-3333-4444-555555555555",
            "provider": "not-a-namespace",
        },
    )
    assert res.status_code == 400
    assert res.json()["error"] == "bad_provider"

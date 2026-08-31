import httpx
from fastapi.testclient import TestClient


def test_quota_increase_endpoint_submits_request(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)

    from api import quota_increase as quota_mod
    from server.app import app

    class _TokenInfo:
        token = "token-123"

    monkeypatch.setattr(quota_mod.auth_token, "get_arm_token", lambda subscription_id: _TokenInfo())

    calls = {}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            calls["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def put(self, url, *, params=None, headers=None, json=None):
            calls["url"] = url
            calls["params"] = params
            calls["headers"] = headers
            calls["json"] = json
            return httpx.Response(
                202,
                json={"name": "quota-op-123", "properties": {"provisioningState": "Accepted"}},
                headers={"x-ms-request-id": "req-789"},
                request=httpx.Request("PUT", url),
            )

    monkeypatch.setattr(quota_mod.httpx, "AsyncClient", FakeAsyncClient)

    client = TestClient(app)
    res = client.post(
        "/api/quota/request-increase",
        json={
            "subscription_id": "11111111-2222-3333-4444-555555555555",
            "region": "eastus",
            "family": "standardDav6Family",
            "new_limit": 500,
        },
    )

    assert res.status_code == 202
    body = res.json()
    assert body["status"] == "pending"
    assert body["request_id"] == "req-789"
    assert body["azure_status_code"] == 202
    assert calls["params"] == {"api-version": "2023-09-01"}
    assert calls["json"]["properties"]["limit"]["value"] == 500
    assert calls["json"]["properties"]["name"]["value"] == "standardDav6Family"
    assert calls["headers"]["Authorization"] == "Bearer token-123"
    assert calls["url"].endswith(
        "/subscriptions/11111111-2222-3333-4444-555555555555/providers/"
        "Microsoft.Compute/locations/eastus/providers/Microsoft.Quota/"
        "quotas/standardDav6Family"
    )


def test_quota_increase_endpoint_surfaces_arm_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)

    from api import quota_increase as quota_mod
    from server.app import app

    class _TokenInfo:
        token = "token-123"

    monkeypatch.setattr(quota_mod.auth_token, "get_arm_token", lambda subscription_id: _TokenInfo())

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def put(self, url, *, params=None, headers=None, json=None):
            return httpx.Response(
                400,
                json={"error": {"message": "Family is not requestable."}},
                request=httpx.Request("PUT", url),
            )

    monkeypatch.setattr(quota_mod.httpx, "AsyncClient", lambda *args, **kwargs: FakeAsyncClient())

    client = TestClient(app)
    res = client.post(
        "/api/quota/request-increase",
        json={
            "subscription_id": "11111111-2222-3333-4444-555555555555",
            "region": "eastus",
            "family": "standardDav6Family",
            "new_limit": 500,
        },
    )

    assert res.status_code == 400
    body = res.json()
    assert body["error"] == "quota_request_failed"
    assert body["message"] == "Family is not requestable."
    assert body["azure_status_code"] == 400

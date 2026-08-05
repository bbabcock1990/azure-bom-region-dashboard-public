import httpx
from fastapi.testclient import TestClient


def test_quota_request_status_reports_pending(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))

    from api import quota_request_status as quota_status_mod
    from server.app import app

    class _TokenInfo:
        token = "token-123"

    monkeypatch.setattr(
        quota_status_mod.auth_token,
        "get_arm_token",
        lambda subscription_id: _TokenInfo(),
    )

    calls = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            calls["client_kwargs"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, *, params=None, headers=None):
            calls["url"] = url
            calls["params"] = params
            calls["headers"] = headers
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "name": {"value": "standardDav6Family"},
                            "limit": 120,
                            "currentValue": 40,
                        }
                    ]
                },
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(quota_status_mod.httpx, "Client", FakeClient)

    client = TestClient(app)
    res = client.get(
        "/api/quota/request-status",
        params={
            "subscription_id": "11111111-2222-3333-4444-555555555555",
            "region": "eastus",
            "family": "standardDav6Family",
            "requested_limit": 200,
        },
    )

    assert res.status_code == 200
    assert res.json() == {
        "status": "pending",
        "current_limit": 120,
        "requested_limit": 200,
    }
    assert calls["params"] == {"api-version": "2023-09-01"}
    assert calls["headers"]["Authorization"] == "Bearer token-123"
    assert calls["url"].endswith(
        "/subscriptions/11111111-2222-3333-4444-555555555555/providers/"
        "Microsoft.Compute/locations/eastus/usages"
    )


def test_quota_request_status_reports_unknown_when_family_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))

    from api import quota_request_status as quota_status_mod
    from server.app import app

    class _TokenInfo:
        token = "token-123"

    monkeypatch.setattr(
        quota_status_mod.auth_token,
        "get_arm_token",
        lambda subscription_id: _TokenInfo(),
    )

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def get(self, url, *, params=None, headers=None):
            return httpx.Response(
                200,
                json={"value": [{"name": {"value": "cores"}, "limit": 500, "currentValue": 100}]},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(quota_status_mod.httpx, "Client", lambda *args, **kwargs: FakeClient())

    client = TestClient(app)
    res = client.get(
        "/api/quota/request-status",
        params={
            "subscription_id": "11111111-2222-3333-4444-555555555555",
            "region": "eastus",
            "family": "standardDav6Family",
            "requested_limit": 200,
        },
    )

    assert res.status_code == 200
    assert res.json() == {
        "status": "unknown",
        "current_limit": None,
        "requested_limit": 200,
    }

import json
from unittest.mock import MagicMock, patch

from _shared import quota_groups


def _resp(status, body, headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    r.text = json.dumps(body)
    r.headers = headers or {}
    return r


def test_check_subscription_quota_parses_usage_rows():
    eastus = {
        "value": [
            {
                "currentValue": 20,
                "limit": 100,
                "name": {"value": "standardDav6Family"},
                "unit": "Count",
            },
            {
                "currentValue": 50,
                "limit": 350,
                "name": {"value": "cores"},
                "unit": "Count",
            },
        ],
    }
    westus3 = {
        "value": [
            {
                "currentValue": 10,
                "limit": 80,
                "name": {"value": "standardDASv5Family"},
                "unit": "Count",
            },
        ],
    }
    with patch("_shared.quota_groups.httpx.Client") as MC:
        client = MC.return_value.__enter__.return_value

        def fake_get(url, params=None, headers=None):
            if "/locations/eastus/usages" in url:
                return _resp(200, eastus)
            return _resp(200, westus3)

        client.get.side_effect = fake_get
        out = quota_groups.check_subscription_quota(
            "token",
            "sub-1",
            ["eastus", "westus3"],
            ["standardDav6Family", "standardDASv5Family"],
        )

    assert out["status"] == "ok"
    assert out["regions"]["eastus"]["families"]["standardDav6Family"]["headroom"] == 80
    assert out["regions"]["eastus"]["total_regional"]["headroom"] == 300
    assert out["regions"]["westus3"]["families"]["standardDASv5Family"]["usage"] == 10


def test_check_subscription_quota_retries_429(monkeypatch):
    responses = iter([
        _resp(429, {"error": "slow down"}, headers={"Retry-After": "0"}),
        _resp(200, {
            "value": [{
                "currentValue": 5,
                "limit": 25,
                "name": {"value": "standardDav6Family"},
            }],
        }),
    ])
    monkeypatch.setattr(quota_groups.time, "sleep", lambda *_args, **_kwargs: None)
    with patch("_shared.quota_groups.httpx.Client") as MC:
        client = MC.return_value.__enter__.return_value
        client.get.side_effect = lambda *args, **kwargs: next(responses)
        out = quota_groups.check_subscription_quota(
            "token",
            "sub-1",
            ["eastus"],
            ["standardDav6Family"],
        )

    assert out["status"] == "ok"
    assert client.get.call_count == 2
    assert out["regions"]["eastus"]["families"]["standardDav6Family"]["headroom"] == 20

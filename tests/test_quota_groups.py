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


# ─── group quota limits (management-group scoped) ────────────────────────────

# Trimmed from the documented ListGroupQuotaLimits-Compute example.
_GROUP_LIMITS_BODY = {
    "name": "westus",
    "type": "Microsoft.Quota/groupQuotas/groupQuotaLimits",
    "properties": {
        "provisioningState": "Succeeded",
        "value": [
            {
                "properties": {
                    "name": {"localizedValue": "standard DDv4 Family vCPUs", "value": "standardddv4family"},
                    "allocatedToSubscriptions": {"value": [
                        {"quotaAllocated": 20, "subscriptionId": "00000000-0000-0000-0000-000000000000"},
                        {"quotaAllocated": 30, "subscriptionId": "a0000000-0000-0000-0000-000000000000"},
                    ]},
                    "availableLimit": 50,
                    "limit": 100,
                    "resourceName": "standardddv4family",
                    "unit": "count",
                }
            }
        ],
    },
}


def test_parse_group_quota_limits_maps_available_to_headroom():
    out = quota_groups._parse_group_quota_limits(_GROUP_LIMITS_BODY, set())
    assert "standardddv4family" in out
    row = out["standardddv4family"]
    assert row["limit"] == 100
    assert row["available"] == 50
    # usage = limit - available so downstream headroom (limit - usage) == available
    assert row["usage"] == 50
    assert (row["limit"] - row["usage"]) == row["available"]


def test_parse_group_quota_limits_sums_allocations_when_available_missing():
    body = {"properties": {"value": [{"properties": {
        "name": {"value": "standardddv4family"},
        "allocatedToSubscriptions": {"value": [
            {"quotaAllocated": 20}, {"quotaAllocated": 30},
        ]},
        "limit": 100,
        "resourceName": "standardddv4family",
    }}]}}
    out = quota_groups._parse_group_quota_limits(body, set())
    assert out["standardddv4family"]["allocated"] == 50


def test_env_group_quota_targets(monkeypatch):
    monkeypatch.setenv("AZURE_QUOTA_MGMT_GROUP_ID", "mg1")
    monkeypatch.setenv("AZURE_QUOTA_GROUP_NAME", "groupquota1")
    assert quota_groups._env_group_quota_targets() == [("mg1", "groupquota1")]
    monkeypatch.setenv("AZURE_QUOTA_MGMT_GROUP_ID", "mg1")
    monkeypatch.setenv("AZURE_QUOTA_GROUP_NAME", "")
    assert quota_groups._env_group_quota_targets() == []


def test_check_quota_groups_uses_real_limits_when_targets_given():
    with patch("_shared.quota_groups.httpx.Client") as MC:
        client = MC.return_value.__enter__.return_value

        def fake_get(url, params=None, headers=None):
            if "/groupQuotaLimits/westus" in url:
                return _resp(200, _GROUP_LIMITS_BODY)
            return _resp(404, {})

        client.get.side_effect = fake_get
        out = quota_groups.check_quota_groups(
            "token", "sub-1", ["westus"], ["standardddv4family"],
            management_group_targets=[("mg1", "groupquota1")],
        )

    assert out["status"] == "ok"
    assert out["has_quota_groups"] is True
    grp = out["groups"][0]
    assert grp["name"] == "groupquota1"
    assert grp["region"] == "westus"
    fam = grp["families"][0]
    assert fam["family"] == "standardddv4family"
    assert (fam["limit"] - fam["usage"]) == 50  # allocatable group headroom


def test_check_quota_groups_falls_back_to_legacy_when_no_targets():
    # No env targets, discovery + limits find nothing → legacy 404 → no_quota_group.
    with patch("_shared.quota_groups.httpx.Client") as MC:
        client = MC.return_value.__enter__.return_value
        client.get.side_effect = lambda *a, **k: _resp(404, {})
        out = quota_groups.check_quota_groups(
            "token", "sub-1", ["westus"], ["standardddv4family"],
        )
    assert out["status"] == "no_quota_group"
    assert out["has_quota_groups"] is False

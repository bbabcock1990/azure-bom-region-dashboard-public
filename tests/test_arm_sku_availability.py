"""Unit tests for ARM-backed SKU availability — no network."""
import re

import httpx
import pytest
import respx

from _shared import arm_sku_availability as availability
from _shared import arm_skus


def _arm_response_for(region: str):
    region = region.lower()
    if region == "eastus":
        return {
            "value": [
                {
                    "resourceType": "virtualMachines",
                    "family": "standardDav6Family",
                    "name": "Standard_D2av6",
                    "locationInfo": [{"zones": ["1", "2", "3"]}],
                    "restrictions": [],
                },
                {
                    "resourceType": "virtualMachines",
                    "family": "standardDASv5Family",
                    "name": "Standard_D2as_v5",
                    "locationInfo": [{"zones": ["1", "2", "3"]}],
                    "restrictions": [
                        {
                            "type": "Zone",
                            "reasonCode": "NotAvailableForSubscription",
                            "restrictionInfo": {"zones": ["1"]},
                        }
                    ],
                },
            ]
        }
    if region == "westeurope":
        return {
            "value": [
                {
                    "resourceType": "virtualMachines",
                    "family": "standardDav6Family",
                    "name": "Standard_D2av6",
                    "locationInfo": [{"zones": ["1", "2", "3"]}],
                    "restrictions": [
                        {
                            "type": "Location",
                            "reasonCode": "NotAvailableForSubscription",
                        }
                    ],
                },
            ]
        }
    return {"value": []}


@respx.mock
def test_arm_sku_availability_full_round_trip():
    route = respx.get(re.compile(r"https://management\.azure\.com/.*/Microsoft\.Compute/skus"))

    def handler(request):
        region = request.url.params.get("$filter", "").split("'")[1]
        return httpx.Response(200, json=_arm_response_for(region))

    route.mock(side_effect=handler)

    rows = availability.fetch_arm_sku_records(
        arm_token="fake.token.here",
        subscription_id="sub-123",
        want_regions=["eastus", "westeurope"],
        want_families=["standardDav6Family", "standardDASv5Family"],
    )
    by = {(r["region"], r["family"]): r for r in rows}

    eus_v6 = by[("eastus", "standardDav6Family")]
    assert eus_v6["zones"] == [True, True, True]
    assert eus_v6["sub_restricted"] is False
    assert eus_v6["sub_restriction_raw"] == "Available"

    weu_v6 = by[("westeurope", "standardDav6Family")]
    assert weu_v6["zones"] == [False, False, False]
    assert weu_v6["sub_restricted"] is True
    assert weu_v6["sub_restriction_raw"].startswith("Region:")

    eus_v5 = by[("eastus", "standardDASv5Family")]
    assert eus_v5["zones"] == [False, True, True]
    assert eus_v5["sub_restricted"] is True
    assert eus_v5["sub_restriction_raw"] == "Restricted in Zone 1"

    weu_v5 = by[("westeurope", "standardDASv5Family")]
    assert weu_v5["zones"] == [False, False, False]
    assert weu_v5["sub_restriction_raw"] == "SKU not in region"


@respx.mock
def test_arm_sku_token_expired_maps_to_stable_code():
    respx.get(re.compile(r"https://management\.azure\.com/.*/Microsoft\.Compute/skus")).mock(
        return_value=httpx.Response(401, json={"error": "expired"})
    )
    with pytest.raises(arm_skus.ArmError) as exc:
        availability.fetch_arm_sku_records(
            arm_token="fake",
            subscription_id="sub-123",
            want_regions=["eastus"],
            want_families=["standardDav6Family"],
        )
    assert exc.value.code == "arm_token_expired"
    assert exc.value.status == 401


@respx.mock
def test_arm_sku_retries_429_then_succeeds(monkeypatch):
    calls = {"count": 0}
    sleeps = []

    def handler(request):
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "30"})
        region = request.url.params.get("$filter", "").split("'")[1]
        return httpx.Response(200, json=_arm_response_for(region))

    monkeypatch.setattr(arm_skus.time, "sleep", lambda seconds: sleeps.append(seconds))
    respx.get(re.compile(r"https://management\.azure\.com/.*/Microsoft\.Compute/skus")).mock(
        side_effect=handler
    )

    rows = availability.fetch_arm_sku_records(
        arm_token="fake",
        subscription_id="sub-123",
        want_regions=["eastus"],
        want_families=["standardDav6Family"],
    )

    assert rows
    assert calls["count"] == 2
    assert sleeps == [15.0]


@respx.mock
def test_arm_sku_retries_5xx_then_succeeds(monkeypatch):
    calls = {"count": 0}
    sleeps = []

    def handler(request):
        calls["count"] += 1
        if calls["count"] < 3:
            return httpx.Response(500, text="try again")
        region = request.url.params.get("$filter", "").split("'")[1]
        return httpx.Response(200, json=_arm_response_for(region))

    monkeypatch.setattr(arm_skus.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(arm_skus.random, "random", lambda: 0.0)
    respx.get(re.compile(r"https://management\.azure\.com/.*/Microsoft\.Compute/skus")).mock(
        side_effect=handler
    )

    rows = availability.fetch_arm_sku_records(
        arm_token="fake",
        subscription_id="sub-123",
        want_regions=["eastus"],
        want_families=["standardDav6Family"],
    )

    assert rows
    assert calls["count"] == 3
    assert sleeps == [2, 4]


@respx.mock
def test_arm_sku_does_not_retry_forbidden(monkeypatch):
    sleeps = []
    monkeypatch.setattr(arm_skus.time, "sleep", lambda seconds: sleeps.append(seconds))
    respx.get(re.compile(r"https://management\.azure\.com/.*/Microsoft\.Compute/skus")).mock(
        return_value=httpx.Response(403, text="forbidden")
    )

    with pytest.raises(arm_skus.ArmError) as exc:
        availability.fetch_arm_sku_records(
            arm_token="fake",
            subscription_id="sub-123",
            want_regions=["eastus"],
            want_families=["standardDav6Family"],
        )

    assert exc.value.code == "arm_forbidden"
    assert sleeps == []


def test_friendly_family():
    assert availability._friendly_family("standardDav6Family") == "Dav6 Series"
    assert availability._friendly_family("other") == "other"

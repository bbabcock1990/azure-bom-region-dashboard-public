"""Unit tests for live zone-redundancy capability verification
(``api/_shared/zonal_capability.py``).

Covers the pure ARM-response parsers and verdict folding with mocked httpx
responses — no network. The service→check mapping and the recursive
zone-redundant scanner are also exercised.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_API = os.path.join(_ROOT, "api")
if _API not in sys.path:
    sys.path.insert(0, _API)

import httpx  # noqa: E402
import pytest  # noqa: E402

from _shared import zonal_capability as zc  # noqa: E402


# ------------------------------------------------------------- service mapping

def test_service_check_kind_maps_known_services():
    assert zc.service_check_kind("Azure Blob Storage") == "storage"
    assert zc.service_check_kind("Azure Files") == "storage"
    assert zc.service_check_kind("Managed Disks (Premium SSD)") == "disks"
    assert zc.service_check_kind("Azure SQL Database") == "sql"
    assert zc.service_check_kind("Azure SQL Managed Instance") == "sql"


def test_service_check_kind_none_for_documented_only():
    assert zc.service_check_kind("Azure Firewall") is None
    assert zc.service_check_kind("Azure Cache for Redis") is None
    assert zc.service_check_kind("") is None


# ------------------------------------------------------- _find_zone_redundant

def test_find_zone_redundant_true_when_available_slo():
    edition = {
        "name": "BusinessCritical", "status": "Available",
        "supportedServiceLevelObjectives": [
            {"name": "BC_Gen5_2", "zoneRedundant": True, "status": "Available"},
        ],
    }
    assert zc._find_zone_redundant(edition) is True


def test_find_zone_redundant_ignores_disabled_zr():
    edition = {
        "name": "GeneralPurpose",
        "supportedServiceLevelObjectives": [
            {"name": "GP_Gen5_2", "zoneRedundant": True, "status": "Disabled"},
        ],
    }
    assert zc._find_zone_redundant(edition) is False


def test_find_zone_redundant_false_when_absent():
    edition = {"name": "Basic", "supportedServiceLevelObjectives": [{"name": "Basic"}]}
    assert zc._find_zone_redundant(edition) is False


# --------------------------------------------------- restriction interpretation

def test_restriction_blocks_region_location_type():
    restrictions = [{
        "type": "Location", "reasonCode": "NotAvailableForSubscription",
        "restrictionInfo": {"locations": ["eastus"]},
    }]
    assert zc._restriction_blocks_region(restrictions, "eastus") == "NotAvailableForSubscription"
    assert zc._restriction_blocks_region(restrictions, "westus") is None


def test_restriction_blocks_region_legacy_values_shape():
    restrictions = [{"type": "Location", "reasonCode": "QuotaId", "values": ["westeurope"]}]
    assert zc._restriction_blocks_region(restrictions, "westeurope") == "QuotaId"


# -------------------------------------------------------------- storage verdict

def test_storage_verdict_available():
    state = {"standard_zrs": {"offered": True, "restricted": False, "reason": ""}}
    v = zc._storage_verdict(state, "Standard_ZRS")
    assert v["verdict"] == "available"


def test_storage_verdict_blocked_when_restricted():
    state = {"standard_zrs": {"offered": True, "restricted": True, "reason": "NotAvailableForSubscription"}}
    v = zc._storage_verdict(state, "Standard_ZRS")
    assert v["verdict"] == "blocked"
    assert "NotAvailableForSubscription" in v["message"]


def test_storage_verdict_unavailable_when_not_offered():
    state = {"standard_lrs": {"offered": True, "restricted": False, "reason": ""}}
    assert zc._storage_verdict(state, "Standard_ZRS")["verdict"] == "unavailable"


def test_storage_verdict_unverifiable_when_empty_state():
    assert zc._storage_verdict({}, "Standard_ZRS")["verdict"] == "unverifiable"


# ----------------------------------------------------------------- disk verdict

def test_disk_verdict_available_with_zones():
    state = {"premium_zrs": {"zones": ["1", "2", "3"], "restricted": False, "reason": ""}}
    assert zc._disk_verdict(state, "Premium_ZRS")["verdict"] == "available"


def test_disk_verdict_unavailable_without_zones():
    state = {"premium_zrs": {"zones": [], "restricted": False, "reason": ""}}
    assert zc._disk_verdict(state, "Premium_ZRS")["verdict"] == "unavailable"


def test_disk_verdict_blocked_when_restricted():
    state = {"premium_zrs": {"zones": ["1"], "restricted": True, "reason": "NotAvailableForSubscription"}}
    assert zc._disk_verdict(state, "Premium_ZRS")["verdict"] == "blocked"


# ------------------------------------------------------------------ sql verdict

def test_sql_verdict_available_when_zone_redundant():
    state = {"businesscritical": {"status": "Available", "zone_redundant": True}}
    assert zc._sql_verdict(state, "BusinessCritical")["verdict"] == "available"


def test_sql_verdict_blocked_when_edition_present_but_no_zr():
    state = {"generalpurpose": {"status": "Available", "zone_redundant": False}}
    assert zc._sql_verdict(state, "GeneralPurpose")["verdict"] == "blocked"


def test_sql_verdict_unavailable_when_missing_or_disabled():
    assert zc._sql_verdict({"premium": {"status": "Disabled", "zone_redundant": True}}, "Premium")["verdict"] == "unavailable"
    assert zc._sql_verdict({"premium": {"status": "Available", "zone_redundant": True}}, "Hyperscale")["verdict"] == "unavailable"


def test_sql_verdict_blocked_when_region_access_disabled():
    # Top-level location status "Disabled" is the portal's "your subscription does
    # not have access to create a server in this region" — no edition is deployable
    # even if it reports zone-redundant support.
    state = {
        zc._SQL_REGION_KEY: {"status": "Disabled", "reason": "Region not enabled for this subscription."},
        "hyperscale": {"status": "Available", "zone_redundant": True},
    }
    v = zc._sql_verdict(state, "Hyperscale")
    assert v["verdict"] == "blocked"
    assert "Region not enabled" in v["message"]


def test_sql_verdict_available_when_region_status_enabled():
    state = {
        zc._SQL_REGION_KEY: {"status": "Available", "reason": ""},
        "businesscritical": {"status": "Available", "zone_redundant": True},
    }
    assert zc._sql_verdict(state, "BusinessCritical")["verdict"] == "available"


# ---------------------------------------------------- parsers over mocked httpx

def _mock_client(monkeypatch, payload, status=200):
    class _Resp:
        status_code = status

        def json(self):
            return payload

        def raise_for_status(self):
            if status >= 400:
                raise httpx.HTTPStatusError("err", request=None, response=None)

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(zc.httpx, "Client", _Client)


def test_fetch_storage_sku_state_parses(monkeypatch):
    payload = {"value": [
        {"name": "Standard_ZRS", "locations": ["eastus"], "restrictions": []},
        {"name": "Standard_GZRS", "locations": ["eastus"],
         "restrictions": [{"type": "Location", "reasonCode": "NotAvailableForSubscription",
                            "restrictionInfo": {"locations": ["eastus"]}}]},
    ]}
    _mock_client(monkeypatch, payload)
    state = zc.fetch_storage_sku_state(arm_token="t", subscription_id="s", region="eastus")
    assert state["standard_zrs"]["offered"] is True
    assert state["standard_zrs"]["restricted"] is False
    assert state["standard_gzrs"]["restricted"] is True


def test_fetch_storage_sku_state_empty_on_403(monkeypatch):
    _mock_client(monkeypatch, {}, status=403)
    assert zc.fetch_storage_sku_state(arm_token="t", subscription_id="s", region="eastus") == {}


def test_fetch_sql_edition_state_parses(monkeypatch):
    payload = {"supportedServerVersions": [
        {"supportedEditions": [
            {"name": "BusinessCritical", "status": "Available",
             "supportedServiceLevelObjectives": [{"name": "BC", "zoneRedundant": True, "status": "Available"}]},
            {"name": "Basic", "status": "Available",
             "supportedServiceLevelObjectives": [{"name": "Basic"}]},
        ]},
    ]}
    _mock_client(monkeypatch, payload)
    state = zc.fetch_sql_edition_state(arm_token="t", subscription_id="s", region="eastus")
    assert state["businesscritical"]["zone_redundant"] is True
    assert state["basic"]["zone_redundant"] is False


def test_fetch_sql_edition_state_captures_region_status(monkeypatch):
    payload = {"status": "Disabled", "reason": "The subscription does not have access to this region.",
               "supportedServerVersions": [
                   {"supportedEditions": [
                       {"name": "Hyperscale", "status": "Available",
                        "supportedServiceLevelObjectives": [{"name": "HS", "zoneRedundant": True, "status": "Available"}]}]}]}
    _mock_client(monkeypatch, payload)
    state = zc.fetch_sql_edition_state(arm_token="t", subscription_id="s", region="eastus")
    assert state[zc._SQL_REGION_KEY]["status"] == "Disabled"
    assert zc._sql_verdict(state, "Hyperscale")["verdict"] == "blocked"


# ------------------------------------------------------------------- evaluate()

def test_evaluate_marks_uncheckable_not_verifiable(monkeypatch):
    # No ARM call should be made for a documented-only service.
    def _boom(*a, **k):
        raise AssertionError("should not fetch for uncheckable service")

    monkeypatch.setattr(zc, "fetch_storage_sku_state", _boom)
    out = zc.evaluate(
        services=[{"name": "Azure Firewall", "tier": "standard"}],
        region="eastus", arm_token="t", subscription_id="s",
    )
    assert out[0]["verdict"] == "not_verifiable"
    assert out[0]["checkable"] is False


def test_evaluate_uses_storage_state_once(monkeypatch):
    calls = {"n": 0}

    def _fake_state(**k):
        calls["n"] += 1
        return {"standard_zrs": {"offered": True, "restricted": False, "reason": ""}}

    monkeypatch.setattr(zc, "fetch_storage_sku_state", _fake_state)
    out = zc.evaluate(
        services=[
            {"name": "Azure Blob Storage", "tier": "zrs"},
            {"name": "Azure Files", "tier": "zrs"},
        ],
        region="eastus", arm_token="t", subscription_id="s",
    )
    assert calls["n"] == 1  # single round-trip covers all storage selections
    assert all(v["verdict"] == "available" for v in out)
    assert out[0]["source"] == "Microsoft.Storage/skus"


# ------------------------------------------------- flexible server (PostgreSQL)

def test_service_check_kind_flex():
    assert zc.service_check_kind("Azure Database for PostgreSQL") == "flex"
    assert zc.service_check_kind("Azure Database for MySQL") == "flex"


def test_flag_to_bool_variants():
    assert zc._flag_to_bool(True) is True
    assert zc._flag_to_bool("Enabled") is True
    assert zc._flag_to_bool("Disabled") is False
    assert zc._flag_to_bool("mystery") is None


def test_has_zone_redundant_ha_detects_mode():
    ed = {"name": "GeneralPurpose", "supportedServerSkus": [
        {"name": "GP_D4ds_v5", "supportedHighAvailabilityModes": ["SameZone", "ZoneRedundant"]}]}
    assert zc._has_zone_redundant_ha(ed) is True
    ed2 = {"name": "Burstable", "supportedServerSkus": [
        {"name": "B1ms", "supportedHighAvailabilityModes": ["SameZone"]}]}
    assert zc._has_zone_redundant_ha(ed2) is False


def test_fetch_flex_edition_state_parses(monkeypatch):
    payload = {"value": [{
        "zoneRedundantHaSupported": "Enabled",
        "supportedServerEditions": [
            {"name": "GeneralPurpose", "supportedServerSkus": [
                {"name": "GP_D4ds_v5", "supportedHighAvailabilityModes": ["SameZone", "ZoneRedundant"]}]},
            {"name": "Burstable", "supportedServerSkus": [
                {"name": "B1ms", "supportedHighAvailabilityModes": ["SameZone"]}]},
        ],
    }]}
    _mock_client(monkeypatch, payload)
    state = zc.fetch_flex_edition_state(
        provider="Microsoft.DBforPostgreSQL", arm_token="t", subscription_id="s", region="eastus")
    assert state[zc._REGION_ZR_KEY]["value"] is True
    assert state["generalpurpose"]["zone_redundant"] is True
    assert state["burstable"]["zone_redundant"] is False


def test_fetch_flex_edition_state_parses_mysql_zone_shape(monkeypatch):
    # MySQL's capabilities feed is partitioned per zone: HA lives on the item
    # (supportedHAMode), not per-SKU, and editions carry no HA fields.
    payload = {"value": [{
        "zone": "none",
        "supportedHAMode": ["SameZone", "ZoneRedundant"],
        "supportedFlexibleServerEditions": [
            {"name": "GeneralPurpose", "supportedServerVersions": [{"name": "8.0.21"}]},
            {"name": "MemoryOptimized", "supportedServerVersions": [{"name": "8.0.21"}]},
        ],
    }]}
    _mock_client(monkeypatch, payload)
    state = zc.fetch_flex_edition_state(
        provider="Microsoft.DBforMySQL", arm_token="t", subscription_id="s", region="eastus")
    assert state[zc._REGION_ZR_KEY]["value"] is True
    assert state["generalpurpose"]["zone_redundant"] is True
    assert state["memoryoptimized"]["zone_redundant"] is True


def test_fetch_flex_edition_state_mysql_no_zr_when_zone_lacks_mode(monkeypatch):
    payload = {"value": [{
        "zone": "none",
        "supportedHAMode": ["SameZone"],
        "supportedFlexibleServerEditions": [
            {"name": "GeneralPurpose", "supportedServerVersions": [{"name": "8.0.21"}]},
        ],
    }]}
    _mock_client(monkeypatch, payload)
    state = zc.fetch_flex_edition_state(
        provider="Microsoft.DBforMySQL", arm_token="t", subscription_id="s", region="eastus")
    assert state["generalpurpose"]["zone_redundant"] is False
    assert zc._flex_verdict(state, "GeneralPurpose")["verdict"] == "blocked"


def test_flex_verdict_blocked_when_region_disabled():
    state = {zc._REGION_ZR_KEY: {"value": False}, "generalpurpose": {"zone_redundant": True}}
    assert zc._flex_verdict(state, "GeneralPurpose")["verdict"] == "blocked"


def test_flex_verdict_available_when_supported():
    state = {zc._REGION_ZR_KEY: {"value": True}, "memoryoptimized": {"zone_redundant": True}}
    assert zc._flex_verdict(state, "MemoryOptimized")["verdict"] == "available"


def test_flex_verdict_blocked_when_edition_has_no_zr_sku():
    state = {zc._REGION_ZR_KEY: {"value": None}, "generalpurpose": {"zone_redundant": False}}
    assert zc._flex_verdict(state, "GeneralPurpose")["verdict"] == "blocked"


def test_flex_verdict_unverifiable_on_empty():
    assert zc._flex_verdict({}, "GeneralPurpose")["verdict"] == "unverifiable"


def test_evaluate_flex_uses_provider_once(monkeypatch):
    calls = {"n": 0}

    def _fake(**k):
        calls["n"] += 1
        return {zc._REGION_ZR_KEY: {"value": True}, "generalpurpose": {"zone_redundant": True},
                "memoryoptimized": {"zone_redundant": True}}

    monkeypatch.setattr(zc, "fetch_flex_edition_state", _fake)
    out = zc.evaluate(
        services=[
            {"name": "Azure Database for PostgreSQL", "tier": "general_purpose"},
            {"name": "Azure Database for PostgreSQL", "tier": "memory_optimized"},
        ],
        region="eastus", arm_token="t", subscription_id="s",
    )
    assert calls["n"] == 1  # one round-trip per provider
    assert all(v["verdict"] == "available" for v in out)
    assert out[0]["source"] == "Flexible Server capabilities"


# ---------------------------------------------------------------- Elastic SAN

def test_service_check_kind_elasticsan():
    assert zc.service_check_kind("Azure Elastic SAN") == "elasticsan"


def test_fetch_elasticsan_sku_state_parses(monkeypatch):
    payload = {"value": [
        {"name": "Premium_ZRS", "locationInfo": [
            {"location": "eastus", "zones": ["1", "2", "3"], "restrictions": []}]},
        {"name": "Premium_LRS", "locationInfo": [
            {"location": "eastus", "zones": [], "restrictions": []}]},
    ]}
    _mock_client(monkeypatch, payload)
    state = zc.fetch_elasticsan_sku_state(arm_token="t", subscription_id="s", region="eastus")
    assert state["premium_zrs"]["zones"] == ["1", "2", "3"]
    assert state["premium_zrs"]["restricted"] is False


def test_fetch_elasticsan_honours_locationinfo_restriction(monkeypatch):
    payload = {"value": [
        {"name": "Premium_ZRS", "locationInfo": [
            {"location": "eastus", "zones": ["1", "2", "3"],
             "restrictions": [{"type": "Location", "reasonCode": "NotAvailableForSubscription",
                               "restrictionInfo": {"locations": ["eastus"]}}]}]},
    ]}
    _mock_client(monkeypatch, payload)
    state = zc.fetch_elasticsan_sku_state(arm_token="t", subscription_id="s", region="eastus")
    assert state["premium_zrs"]["restricted"] is True


def test_elasticsan_verdict_available():
    state = {"premium_zrs": {"zones": ["1", "2", "3"], "restricted": False, "reason": ""}}
    assert zc._elasticsan_verdict(state, "Premium_ZRS")["verdict"] == "available"


def test_elasticsan_verdict_unavailable_without_zones():
    state = {"premium_zrs": {"zones": [], "restricted": False, "reason": ""}}
    assert zc._elasticsan_verdict(state, "Premium_ZRS")["verdict"] == "unavailable"


def test_evaluate_elasticsan(monkeypatch):
    monkeypatch.setattr(zc, "fetch_elasticsan_sku_state",
                        lambda **k: {"premium_zrs": {"zones": ["1", "2", "3"], "restricted": False, "reason": ""}})
    out = zc.evaluate(
        services=[{"name": "Azure Elastic SAN", "tier": "zrs"}],
        region="eastus", arm_token="t", subscription_id="s",
    )
    assert out[0]["verdict"] == "available"
    assert out[0]["source"] == "Microsoft.ElasticSan/skus"

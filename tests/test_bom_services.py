"""Tests for api/_shared/bom_services.py (server-side ARM availability check
+ BOM record synthesis used by the in-app BOM editor)."""
import json
from unittest.mock import patch, MagicMock

import pytest

from _shared import bom_services
from _shared.pipeline import model as pipeline_model


# ─── Catalog ────────────────────────────────────────────────────────────────

def test_catalog_loads_and_has_expected_shape():
    cat = bom_services.load_catalog()
    assert isinstance(cat, list)
    assert len(cat) >= 20, "extracted catalog should have ~30 entries"
    for entry in cat:
        assert set(entry.keys()) >= {"name", "provider", "resource_type", "zone_check"}
        assert isinstance(entry["zone_check"], bool)


def test_resolve_services_returns_catalog_entries():
    out = bom_services.resolve_services(["Azure Automation", "Premium SSD v2"])
    names = [s["name"] for s in out]
    assert names == ["Azure Automation", "Premium SSD v2"]
    ssdv2 = next(s for s in out if s["name"] == "Premium SSD v2")
    assert ssdv2["zone_check"] is True


def test_resolve_services_dedupes_and_preserves_order():
    out = bom_services.resolve_services(["Azure Automation", "Azure Automation", "Azure Firewall"])
    assert [s["name"] for s in out] == ["Azure Automation", "Azure Firewall"]


def test_resolve_services_rejects_unknown():
    with pytest.raises(bom_services.BomServicesError) as ex:
        bom_services.resolve_services(["Azure Automation", "Not A Real Thing"])
    assert ex.value.code == "unknown_services"
    assert "Not A Real Thing" in ex.value.message


def test_resolve_services_skips_empty_and_whitespace():
    out = bom_services.resolve_services(["Azure Automation", "", "  ", None])
    assert [s["name"] for s in out] == ["Azure Automation"]


# ─── Region normalization ──────────────────────────────────────────────────

def test_normalize_region():
    assert bom_services._normalize_region("East US") == "eastus"
    assert bom_services._normalize_region("West Europe") == "westeurope"
    assert bom_services._normalize_region("Brazil South (Stage)") == "brazilsouthstage"
    assert bom_services._normalize_region("") == ""


# ─── ARM mocks ─────────────────────────────────────────────────────────────

def _arm_response(status, body):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    r.text = json.dumps(body)
    return r


def test_fetch_provider_locations_parses_resource_types():
    body = {
        "resourceTypes": [
            {"resourceType": "automationAccounts",
             "locations": ["East US", "West US"]},
            {"resourceType": "someOtherThing", "locations": ["Mars"]},
        ],
    }
    with patch("_shared.bom_services.httpx.Client") as MC:
        client = MC.return_value.__enter__.return_value
        client.get.return_value = _arm_response(200, body)
        out = bom_services.fetch_provider_locations(
            [{"name": "Azure Automation", "provider": "Microsoft.Automation",
              "resource_type": "automationAccounts"}],
            arm_token="t",
        )
    key = "Microsoft.Automation/automationAccounts"
    assert key in out
    assert "East US" in out[key]
    assert "Mars" not in out[key]  # different resource type


def test_fetch_provider_locations_treats_global_sentinel():
    body = {
        "resourceTypes": [
            {"resourceType": "automationAccounts", "locations": ["global"]},
        ],
    }
    with patch("_shared.bom_services.httpx.Client") as MC:
        client = MC.return_value.__enter__.return_value
        client.get.return_value = _arm_response(200, body)
        out = bom_services.fetch_provider_locations(
            [{"name": "Azure Automation", "provider": "Microsoft.Automation",
              "resource_type": "automationAccounts"}],
            arm_token="t",
        )
    assert out["Microsoft.Automation/automationAccounts"] == ["*"]


def test_fetch_provider_locations_401_raises():
    with patch("_shared.bom_services.httpx.Client") as MC:
        client = MC.return_value.__enter__.return_value
        client.get.return_value = _arm_response(401, {"error": "unauth"})
        with pytest.raises(bom_services.BomServicesError) as ex:
            bom_services.fetch_provider_locations(
                [{"name": "x", "provider": "p", "resource_type": "rt"}],
                arm_token="t",
            )
    assert ex.value.code == "arm_unauthorized"


def test_fetch_ssdv2_zones_paginates_and_merges():
    page1 = {
        "value": [{
            "name": "PremiumV2_LRS",
            "resourceType": "disks",
            "locationInfo": [
                {"location": "eastus", "zones": ["1", "2", "3"]},
                {"location": "westus3", "zones": ["1"]},
            ],
        }],
        "nextLink": "https://management.azure.com/next",
    }
    page2 = {
        "value": [{
            "name": "PremiumV2_LRS",
            "resourceType": "disks",
            "locationInfo": [{"location": "eastus", "zones": ["3", "4"]}],
        }],
    }
    with patch("_shared.bom_services.httpx.Client") as MC:
        client = MC.return_value.__enter__.return_value
        client.get.side_effect = [
            _arm_response(200, page1),
            _arm_response(200, page2),
        ]
        out = bom_services.fetch_ssdv2_zones(
            arm_token="t", subscription_id="11111111-1111-1111-1111-111111111111",
        )
    assert out["eastus"] == ["1", "2", "3", "4"]
    assert out["westus3"] == ["1"]


def test_fetch_ssdv2_zones_403_returns_empty():
    with patch("_shared.bom_services.httpx.Client") as MC:
        client = MC.return_value.__enter__.return_value
        client.get.return_value = _arm_response(403, {"error": "no access"})
        out = bom_services.fetch_ssdv2_zones(
            arm_token="t", subscription_id="11111111-1111-1111-1111-111111111111",
        )
    assert out == {}


# ─── check_services_availability ───────────────────────────────────────────

def test_check_services_availability_empty_services_passes_all():
    out = bom_services.check_services_availability(
        [], [{"name": "eastus", "display_name": "East US"},
             {"name": "westus3", "display_name": "West US 3"}],
        arm_token="t", subscription_id="00000000-0000-0000-0000-000000000000",
    )
    assert all(r["overall"] == "PASS" for r in out)
    assert all(r["services"] == {} for r in out)


def test_check_services_availability_marks_missing_regions_fail():
    svc = [{"name": "Azure Automation", "provider": "Microsoft.Automation",
            "resource_type": "automationAccounts", "zone_check": False}]
    body = {
        "resourceTypes": [
            {"resourceType": "automationAccounts", "locations": ["East US"]},
        ],
    }
    regions = [
        {"name": "eastus", "display_name": "East US"},
        {"name": "westus3", "display_name": "West US 3"},
    ]
    with patch("_shared.bom_services.httpx.Client") as MC:
        client = MC.return_value.__enter__.return_value
        client.get.return_value = _arm_response(200, body)
        out = bom_services.check_services_availability(
            svc, regions, arm_token="t",
            subscription_id="00000000-0000-0000-0000-000000000000",
        )
    e = next(r for r in out if r["region"] == "eastus")
    w = next(r for r in out if r["region"] == "westus3")
    assert e["overall"] == "PASS"
    assert e["services"]["Azure Automation"]["available"] is True
    assert w["overall"] == "FAIL"
    assert w["services"]["Azure Automation"]["available"] is False


def test_check_services_availability_ssdv2_needs_three_zones():
    svc = [{"name": "Premium SSD v2", "provider": "Microsoft.Compute",
            "resource_type": "disks", "zone_check": True}]
    skus_body = {
        "value": [{
            "name": "PremiumV2_LRS",
            "resourceType": "disks",
            "locationInfo": [
                {"location": "eastus", "zones": ["1", "2", "3"]},
                {"location": "northeurope", "zones": ["1"]},
            ],
        }],
    }
    regions = [
        {"name": "eastus", "display_name": "East US"},
        {"name": "northeurope", "display_name": "North Europe"},
        {"name": "qatarcentral", "display_name": "Qatar Central"},
    ]
    with patch("_shared.bom_services.httpx.Client") as MC:
        client = MC.return_value.__enter__.return_value
        client.get.return_value = _arm_response(200, skus_body)
        out = bom_services.check_services_availability(
            svc, regions, arm_token="t",
            subscription_id="11111111-1111-1111-1111-111111111111",
        )
    by_region = {r["region"]: r for r in out}
    assert by_region["eastus"]["services"]["Premium SSD v2"]["available"] is True
    assert by_region["northeurope"]["services"]["Premium SSD v2"]["available"] is False
    assert by_region["qatarcentral"]["services"]["Premium SSD v2"]["available"] is False


def test_check_services_availability_uses_ssdv2_subscription_id_override():
    """Cross-tenant fix: when ssdv2_subscription_id is supplied, the SKU
    URL must use that sub (the operator's own) instead of the customer's
    subscription_id. Otherwise the call 401s in the foreign tenant and
    every region falls through to "Premium SSD v2: not available".
    """
    svc = [{"name": "Premium SSD v2", "provider": "Microsoft.Compute",
            "resource_type": "disks", "zone_check": True}]
    skus_body = {
        "value": [{
            "name": "PremiumV2_LRS",
            "resourceType": "disks",
            "locationInfo": [
                {"location": "centralus", "zones": ["1", "2", "3"]},
            ],
        }],
    }
    regions = [{"name": "centralus", "display_name": "Central US"}]
    customer_sub = "77777777-7777-7777-7777-777777777777"
    operator_sub = "08080808-0808-0808-0808-080808080808"
    with patch("_shared.bom_services.httpx.Client") as MC:
        client = MC.return_value.__enter__.return_value
        client.get.return_value = _arm_response(200, skus_body)
        out = bom_services.check_services_availability(
            svc, regions, arm_token="t",
            subscription_id=customer_sub,
            ssdv2_subscription_id=operator_sub,
        )
        # ARM URL must reference the operator's sub, not the customer's
        called_url = client.get.call_args_list[0].args[0]
        assert operator_sub in called_url
        assert customer_sub not in called_url
    by_region = {r["region"]: r for r in out}
    assert by_region["centralus"]["services"]["Premium SSD v2"]["available"] is True


def test_check_services_availability_ssdv2_subscription_id_defaults_to_subscription_id():
    """When ssdv2_subscription_id is omitted, the existing single-sub
    behaviour is preserved (backward compat)."""
    svc = [{"name": "Premium SSD v2", "provider": "Microsoft.Compute",
            "resource_type": "disks", "zone_check": True}]
    skus_body = {
        "value": [{
            "name": "PremiumV2_LRS",
            "resourceType": "disks",
            "locationInfo": [{"location": "eastus", "zones": ["1", "2", "3"]}],
        }],
    }
    regions = [{"name": "eastus", "display_name": "East US"}]
    sub_id = "33333333-3333-3333-3333-333333333333"
    with patch("_shared.bom_services.httpx.Client") as MC:
        client = MC.return_value.__enter__.return_value
        client.get.return_value = _arm_response(200, skus_body)
        bom_services.check_services_availability(
            svc, regions, arm_token="t", subscription_id=sub_id,
        )
        called_url = client.get.call_args_list[0].args[0]
        assert sub_id in called_url


# ─── Synthesis → pipeline_model contract ───────────────────────────────────

def test_synthesize_bom_records_marks_pass_supported_and_fail_unsupported():
    svc = [{"name": "Azure Automation"}]
    results = [
        {"region": "eastus", "display_name": "East US", "overall": "PASS",
         "services": {"Azure Automation": {"available": True, "detail": ""}}},
        {"region": "westus3", "display_name": "West US 3", "overall": "FAIL",
         "services": {"Azure Automation": {"available": False,
                                            "detail": "not in provider list"}}},
    ]
    header, records = bom_services.synthesize_bom_records(svc, results)
    assert header == ["Region", "Display Name", "Overall Status", "Azure Automation"]
    assert records[0]["Overall Status"].startswith("\u2705")
    assert "SUPPORTED" in records[0]["Overall Status"]
    assert "UNSUPPORTED" not in records[0]["Overall Status"]
    assert records[1]["Overall Status"].startswith("\u274c")
    assert "UNSUPPORTED" in records[1]["Overall Status"]
    assert records[0]["Azure Automation"].startswith("\u2705")
    assert records[1]["Azure Automation"].startswith("\u274c")


def test_synthesize_empty_bom_marks_every_region_supported():
    region_specs = [
        {"name": "eastus", "display_name": "East US"},
        {"name": "qatarcentral", "display_name": "Qatar Central"},
    ]
    header, records = bom_services.synthesize_empty_bom(region_specs)
    assert header == ["Region", "Display Name", "Overall Status"]
    assert len(records) == 2
    for rec in records:
        assert "SUPPORTED" in rec["Overall Status"]
        assert "UNSUPPORTED" not in rec["Overall Status"]


def test_empty_bom_does_not_cause_false_bom_issues_in_pipeline():
    """The critical contract: a SKU-only saved BOM (no services) must not
    cause pipeline_model.extract_missing_services to flag every region."""
    region_specs = [
        {"name": "eastus", "display_name": "East US"},
        {"name": "qatarcentral", "display_name": "Qatar Central"},
    ]
    header, records = bom_services.synthesize_empty_bom(region_specs)
    for rec in records:
        missing = pipeline_model.extract_missing_services(rec, header)
        # 0 service columns → 0 missing entries, regardless of return shape
        assert len(missing) == 0, (
            f"empty BOM should produce 0 missing services per region, got {missing}"
        )


def test_synthesized_records_with_services_flag_only_failing_ones():
    svc = [{"name": "Azure Firewall"}, {"name": "Azure Automation"}]
    results = [{
        "region": "eastus", "display_name": "East US", "overall": "FAIL",
        "services": {
            "Azure Firewall": {"available": False, "detail": "not in provider list"},
            "Azure Automation": {"available": True, "detail": ""},
        },
    }]
    header, records = bom_services.synthesize_bom_records(svc, results)
    missing = pipeline_model.extract_missing_services(records[0], header)
    missing_names = [m["service"] if isinstance(m, dict) else m for m in missing]
    assert "Azure Firewall" in missing_names
    assert "Azure Automation" not in missing_names


# ─── build_region_specs ── display-name cascade ──────────────
import os
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARED_DATA_DIR = os.path.join(HERE, "api", "_shared", "data")


def test_build_region_specs_falls_back_to_catalog_when_latency_csv_misses():
    """Newer regions (Austria East, Belgium Central, Chile Central,
    Denmark East, Indonesia Central, Spain Central) aren't yet in the
    latency CSV. The pipeline must still resolve their human-readable
    display name from the master bom_region_catalog.json — otherwise
    they render as `austriaeast` in the Region table and miss every
    downstream display-name lookup (geo.lookup, alternatives, etc).
    """
    # Reset module-level cache so the test sees the current on-disk CSV.
    bom_services._REGION_DISPLAY_CACHE = None
    specs = bom_services.build_region_specs(
        ["austriaeast", "belgiumcentral", "chilecentral", "denmarkeast",
         "indonesiacentral", "spaincentral", "eastus"],
        data_dir=SHARED_DATA_DIR,
    )
    by_name = {s["name"]: s["display_name"] for s in specs}
    assert by_name["austriaeast"] == "Austria East"
    assert by_name["belgiumcentral"] == "Belgium Central"
    assert by_name["chilecentral"] == "Chile Central"
    assert by_name["denmarkeast"] == "Denmark East"
    assert by_name["indonesiacentral"] == "Indonesia Central"
    assert by_name["spaincentral"] == "Spain Central"
    # eastus is in both sources — latency CSV (existing source) wins to
    # preserve the exact string other modules use as a join key.
    assert by_name["eastus"] == "East US"


def test_build_region_specs_keeps_shortname_when_unknown_to_both_sources():
    """A region the user added that's in neither the latency CSV nor the
    catalog (e.g. a brand-new region launched before any data refresh)
    should surface visibly as the shortname rather than crash."""
    bom_services._REGION_DISPLAY_CACHE = None
    specs = bom_services.build_region_specs(
        ["someneweuapregion"], data_dir=SHARED_DATA_DIR,
    )
    assert specs == [{"name": "someneweuapregion",
                      "display_name": "someneweuapregion"}]

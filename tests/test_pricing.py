"""Unit tests for the BOM cost-estimate engine (_shared.pricing).

Covers the pure logic that does not need the network:
- ``anchor_size_for`` family->representative-size mapping (incl. unmappable).
- ``_matches_target_os`` OS + spot/low-priority filtering of retail meters.
- ``estimate`` compute math, ACD-net, non-compute uplift and itemized totals,
  with ``_fetch_region_size_prices`` monkeypatched so no HTTP happens.
"""

from _shared import pricing as pricing_mod


# --------------------------------------------------------------- anchor_size_for

def test_anchor_size_for_mainstream_families():
    assert pricing_mod.anchor_size_for("standardDSv3Family", 4) == "Standard_D4s_v3"
    assert pricing_mod.anchor_size_for("Dsv5", 4) == "Standard_D4s_v5"
    assert pricing_mod.anchor_size_for("Dasv6", 4) == "Standard_D4as_v6"
    assert pricing_mod.anchor_size_for("Easv6", 4) == "Standard_E4as_v6"
    assert pricing_mod.anchor_size_for("Fsv2", 4) == "Standard_F4s_v2"


def test_anchor_size_for_respects_vcpu_token():
    assert pricing_mod.anchor_size_for("Dsv5", 2) == "Standard_D2s_v5"
    assert pricing_mod.anchor_size_for("Dsv5", 8) == "Standard_D8s_v5"


def test_anchor_size_for_specialized_returns_none():
    # Specialized GPU/HPC families with subfamily digits are not mappable.
    assert pricing_mod.anchor_size_for("standardNCadsA100v4Family", 4) is None
    assert pricing_mod.anchor_size_for("", 4) is None


# --------------------------------------------------------------- _matches_target_os

def _meter(product, sku="", meter="", type_="Consumption", uom="1 Hour"):
    return {
        "productName": product,
        "skuName": sku,
        "meterName": meter,
        "type": type_,
        "unitOfMeasure": uom,
    }


def test_matches_linux_excludes_windows():
    assert pricing_mod._matches_target_os(_meter("Virtual Machines Dv5"), "linux")
    assert not pricing_mod._matches_target_os(
        _meter("Virtual Machines Dv5 Windows"), "linux"
    )


def test_matches_windows_requires_windows_product():
    assert pricing_mod._matches_target_os(
        _meter("Virtual Machines Dv5 Windows"), "windows"
    )
    assert not pricing_mod._matches_target_os(_meter("Virtual Machines Dv5"), "windows")


def test_matches_excludes_spot_and_low_priority():
    assert not pricing_mod._matches_target_os(
        _meter("Virtual Machines Dv5", sku="D4s v5 Spot"), "linux"
    )
    assert not pricing_mod._matches_target_os(
        _meter("Virtual Machines Dv5", meter="D4s v5 Low Priority"), "linux"
    )


def test_matches_excludes_non_hourly_and_reservations():
    assert not pricing_mod._matches_target_os(
        _meter("Virtual Machines Dv5", type_="Reservation"), "linux"
    )
    assert not pricing_mod._matches_target_os(
        _meter("Virtual Machines Dv5", uom="1 Month"), "linux"
    )


# --------------------------------------------------------------- estimate()

def _patch_prices(monkeypatch, price_map):
    """Patch the network layer so every requested size returns ``price_map``."""
    def fake_fetch(region, sizes, *, currency, os_name):
        return {s: price_map[s] for s in sizes if s in price_map}

    monkeypatch.setattr(pricing_mod, "_fetch_region_size_prices", fake_fetch)
    pricing_mod.reset_cache()


def test_estimate_compute_math(monkeypatch):
    # D4s_v5 anchor at $0.20/hr -> $0.05 per vCPU-hr. 100 cores * 730h = $3650.
    _patch_prices(monkeypatch, {"Standard_D4s_v5": 0.20})
    result = pricing_mod.estimate(
        ["eastus"],
        [{"family": "Dsv5", "label": "Dsv5", "required_cores": 100}],
        os_name="linux",
        hours_per_month=730,
        acd_discount_pct=0.0,
        noncompute_uplift_pct=0.0,
    )
    region = result["regions"]["eastus"]
    assert region["priced_any"] is True
    assert region["complete"] is True
    assert region["compute"]["monthly_list"] == 3650.0
    assert region["compute"]["monthly_net"] == 3650.0
    # No uplift, no itemized services -> total == compute.
    assert region["monthly_net"] == 3650.0
    assert result["estimate_only"] is True


def test_estimate_applies_acd_discount(monkeypatch):
    _patch_prices(monkeypatch, {"Standard_D4s_v5": 0.20})
    result = pricing_mod.estimate(
        ["eastus"],
        [{"family": "Dsv5", "label": "Dsv5", "required_cores": 100}],
        hours_per_month=730,
        acd_discount_pct=20.0,
    )
    region = result["regions"]["eastus"]
    assert region["compute"]["monthly_list"] == 3650.0
    # 20% off list.
    assert region["compute"]["monthly_net"] == 2920.0


def test_estimate_noncompute_uplift_and_itemized(monkeypatch):
    _patch_prices(monkeypatch, {"Standard_D4s_v5": 0.20})
    result = pricing_mod.estimate(
        ["eastus"],
        [{"family": "Dsv5", "label": "Dsv5", "required_cores": 100}],
        hours_per_month=730,
        acd_discount_pct=0.0,
        noncompute_uplift_pct=35.0,
        services=["Azure Bastion"],
        service_estimates={"Azure Bastion": 140.0},
    )
    region = result["regions"]["eastus"]
    nc = region["noncompute"]
    # 35% uplift on $3650 compute.
    assert nc["uplift_list"] == 1277.5
    assert nc["itemized_total_list"] == 140.0
    assert nc["monthly_list"] == 1417.5
    # Total = compute + non-compute.
    assert region["monthly_list"] == 3650.0 + 1417.5


def test_estimate_itemized_only_for_bom_services(monkeypatch):
    _patch_prices(monkeypatch, {"Standard_D4s_v5": 0.20})
    result = pricing_mod.estimate(
        ["eastus"],
        [{"family": "Dsv5", "label": "Dsv5", "required_cores": 100}],
        noncompute_uplift_pct=0.0,
        services=["Azure Bastion"],  # only this service is in the BOM
        service_estimates={"Azure Bastion": 140.0, "Something Else": 999.0},
    )
    region = result["regions"]["eastus"]
    assert region["noncompute"]["itemized_total_list"] == 140.0
    names = {i["service"] for i in region["noncompute"]["items"]}
    assert names == {"Azure Bastion"}


def test_estimate_unpriceable_family_marked_incomplete(monkeypatch):
    # No prices returned at all -> family unpriced, region not priced.
    _patch_prices(monkeypatch, {})
    result = pricing_mod.estimate(
        ["eastus"],
        [{"family": "Dsv5", "label": "Dsv5", "required_cores": 100}],
    )
    region = result["regions"]["eastus"]
    assert region["priced_any"] is False
    assert region["complete"] is False
    assert region["compute"]["families"][0]["priced"] is False


def test_estimate_multiple_regions(monkeypatch):
    _patch_prices(monkeypatch, {"Standard_D4s_v5": 0.20})
    result = pricing_mod.estimate(
        ["eastus", "westus2"],
        [{"family": "Dsv5", "label": "Dsv5", "required_cores": 100}],
        hours_per_month=730,
        noncompute_uplift_pct=0.0,
    )
    assert set(result["regions"].keys()) == {"eastus", "westus2"}
    for region in result["regions"].values():
        assert region["compute"]["monthly_list"] == 3650.0


# --------------------------------------------------------------- settings store

def test_pricing_settings_round_trip():
    from _shared import pricing_settings as ps

    # Defaults are returned when nothing is saved yet.
    defaults = ps.get_settings()
    assert defaults["pricing_os"] == "linux"
    assert defaults["currency"] == "USD"

    saved = ps.save_settings({
        "acd_discount_pct": 15,
        "pricing_os": "windows",
        "hours_per_month": 720,
        "currency": "eur",
        "noncompute_uplift_pct": 40,
        "service_estimates": {"Azure Bastion": 140, "Bad": -5},
    })
    assert saved["acd_discount_pct"] == 15.0
    assert saved["pricing_os"] == "windows"
    assert saved["hours_per_month"] == 720
    assert saved["currency"] == "EUR"
    assert saved["noncompute_uplift_pct"] == 40.0
    # Negative figures are dropped by the cleaner.
    assert saved["service_estimates"] == {"Azure Bastion": 140.0}


def test_pricing_settings_invalid_values_fall_back():
    from _shared import pricing_settings as ps

    saved = ps.save_settings({
        "pricing_os": "bogus",
        "hours_per_month": -1,
        "currency": "dollars",
    })
    assert saved["pricing_os"] == "linux"
    assert saved["hours_per_month"] == 730
    assert saved["currency"] == "USD"


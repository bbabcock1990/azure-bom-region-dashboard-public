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


def test_estimate_falls_back_to_alt_family_when_primary_unpriced(monkeypatch):
    # Primary generation (Dsv7 -> Standard_D4s_v7) has no price in this region,
    # but the BOM fallback (Dsv6 -> Standard_D4s_v6) does. The region should be
    # priced on the fallback and flagged priced_via_alt.
    _patch_prices(monkeypatch, {"Standard_D4s_v6": 0.20})
    result = pricing_mod.estimate(
        ["austriaeast"],
        [{
            "family": "Dsv7", "label": "Dsv7", "required_cores": 100,
            "alt_family": "Dsv6", "alt_label": "Dsv6",
        }],
        hours_per_month=730,
        acd_discount_pct=0.0,
        noncompute_uplift_pct=0.0,
        suggest_alternatives=False,
    )
    region = result["regions"]["austriaeast"]
    assert region["priced_any"] is True
    assert region["priced_via_alt"] is True
    assert region["compute"]["priced_via_alt"] is True
    # 100 cores * $0.05/vCPU-hr * 730h = $3650, on the Dsv6 fallback.
    assert region["compute"]["monthly_list"] == 3650.0
    f = region["compute"]["families"][0]
    assert f["priced"] is True
    assert f["priced_via_alt"] is True
    assert f["priced_label"] == "Dsv6"
    # The row still identifies the requested BOM family.
    assert f["label"] == "Dsv7"


def test_estimate_no_alt_fallback_stays_unpriced(monkeypatch):
    # Neither primary nor alt priced -> region remains unpriced (no false cost).
    _patch_prices(monkeypatch, {})
    result = pricing_mod.estimate(
        ["austriaeast"],
        [{
            "family": "Dsv7", "label": "Dsv7", "required_cores": 100,
            "alt_family": "Dsv6", "alt_label": "Dsv6",
        }],
        suggest_alternatives=False,
    )
    region = result["regions"]["austriaeast"]
    assert region["priced_any"] is False
    assert region["priced_via_alt"] is False


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


def test_pricing_settings_alternative_fields():
    from _shared import pricing_settings as ps

    defaults = ps.get_settings()
    assert defaults["suggest_alternatives"] is True
    assert defaults["alt_min_savings_pct"] == 5.0

    saved = ps.save_settings({"suggest_alternatives": False, "alt_min_savings_pct": 12})
    assert saved["suggest_alternatives"] is False
    assert saved["alt_min_savings_pct"] == 12.0


# --------------------------------------------------------------- equivalents / vendor

def test_equivalents_same_ratio_group():
    eqs = {e.lower() for e in pricing_mod.equivalents("Dsv5")}
    # AMD, ARM and newer/older generations of the same 4 GiB/vCPU D-series.
    assert "dasv6" in eqs
    assert "dpsv5" in eqs
    assert "dsv3" in eqs
    # The family itself is never in its own equivalents.
    assert "dsv5" not in eqs
    # Different ratio series must not leak in.
    assert not any(x.startswith("e") or x.startswith("f") for x in eqs)


def test_equivalents_handles_canonical_family_id():
    # BOM ids arrive as e.g. "standardDsv5Family" (any case).
    eqs = {e.lower() for e in pricing_mod.equivalents("standardDsv5Family")}
    assert "dasv6" in eqs


def test_equivalents_newer_generation_family():
    # Newest Intel gens (Dsv7/Esv7) must still map to cheaper AMD/ARM peers.
    dq = {e.lower() for e in pricing_mod.equivalents("Dsv7")}
    assert "dasv6" in dq and "dpsv6" in dq
    eq = {e.lower() for e in pricing_mod.equivalents("Esv7")}
    assert "easv6" in eq and "epsv6" in eq


def test_equivalents_unknown_series_empty():
    assert pricing_mod.equivalents("NCadsA100v4") == []
    assert pricing_mod.equivalents("") == []


def test_cpu_vendor_detection():
    assert pricing_mod._cpu_vendor("Dasv6")[0] == "AMD"
    assert pricing_mod._cpu_vendor("Dpsv5")[0] == "ARM"
    assert pricing_mod._cpu_vendor("Dsv5")[0] == "Intel"
    # ARM carries an image-compatibility caveat.
    assert "ARM64" in pricing_mod._cpu_vendor("Dpsv5")[1]


# --------------------------------------------------------------- estimate() alternatives

def test_estimate_suggests_cheaper_equivalent(monkeypatch):
    # Dsv5 Intel $0.20/hr anchor; a cheaper AMD Dasv6 at $0.16/hr (20% off).
    _patch_prices(monkeypatch, {
        "Standard_D4s_v5": 0.20,
        "Standard_D4as_v6": 0.16,
    })
    result = pricing_mod.estimate(
        ["eastus"],
        [{"family": "Dsv5", "label": "Dsv5", "required_cores": 100}],
        hours_per_month=730,
        noncompute_uplift_pct=0.0,
        suggest_alternatives=True,
        alt_min_savings_pct=5.0,
    )
    region = result["regions"]["eastus"]
    fam = region["compute"]["families"][0]
    labels = {a["family"] for a in fam["alternatives"]}
    assert "Dasv6" in labels
    dasv6 = next(a for a in fam["alternatives"] if a["family"] == "Dasv6")
    assert dasv6["vendor"] == "AMD"
    assert dasv6["savings_pct"] == 20.0
    # Region roll-up reflects the swap.
    assert region["has_cheaper_alt"] is True
    assert region["compute"]["alt_savings_pct"] == 20.0
    # 100 cores * 730h: $3650 primary -> $2920 optimized.
    assert region["compute"]["optimized_monthly_list"] == 2920.0
    swap = region["compute"]["swaps"][0]
    assert swap["to_family"] == "Dasv6"


def test_estimate_ignores_alt_below_threshold(monkeypatch):
    # AMD only 3% cheaper — below the 5% threshold, so not suggested.
    _patch_prices(monkeypatch, {
        "Standard_D4s_v5": 0.20,
        "Standard_D4as_v6": 0.194,
    })
    result = pricing_mod.estimate(
        ["eastus"],
        [{"family": "Dsv5", "label": "Dsv5", "required_cores": 100}],
        suggest_alternatives=True,
        alt_min_savings_pct=5.0,
    )
    region = result["regions"]["eastus"]
    assert region["has_cheaper_alt"] is False
    assert region["compute"]["families"][0]["alternatives"] == []


def test_estimate_alternatives_disabled(monkeypatch):
    _patch_prices(monkeypatch, {
        "Standard_D4s_v5": 0.20,
        "Standard_D4as_v6": 0.10,
    })
    result = pricing_mod.estimate(
        ["eastus"],
        [{"family": "Dsv5", "label": "Dsv5", "required_cores": 100}],
        suggest_alternatives=False,
    )
    region = result["regions"]["eastus"]
    assert region["has_cheaper_alt"] is False
    assert region["compute"]["families"][0].get("alternatives") == []
    assert result["suggest_alternatives"] is False


def test_estimate_excludes_bom_family_from_alternatives(monkeypatch):
    # Both Dsv5 and Dasv6 are in the BOM — neither should be suggested as an
    # alternative for the other (they're already planned).
    _patch_prices(monkeypatch, {
        "Standard_D4s_v5": 0.20,
        "Standard_D4as_v6": 0.16,
    })
    result = pricing_mod.estimate(
        ["eastus"],
        [
            {"family": "Dsv5", "label": "Dsv5", "required_cores": 50},
            {"family": "Dasv6", "label": "Dasv6", "required_cores": 50},
        ],
        suggest_alternatives=True,
        alt_min_savings_pct=5.0,
    )
    region = result["regions"]["eastus"]
    for fam in region["compute"]["families"]:
        alt_labels = {a["family"].lower() for a in fam.get("alternatives", [])}
        assert "dsv5" not in alt_labels
        assert "dasv6" not in alt_labels



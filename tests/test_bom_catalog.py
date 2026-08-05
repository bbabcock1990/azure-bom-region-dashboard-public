"""Tests for api/_shared/bom_catalog.py + bom_regions.py.

Covers validators, the built-in seed loader, and the merge behavior
between built-in and custom (custom layer mocked out via monkeypatching
list_custom so we don't need Azurite for these tests).
"""
import pytest

from _shared import bom_catalog, bom_regions, bom_services


# ─── bom_catalog validators ────────────────────────────────────────────────

def test_validate_region_normalizes_and_defaults_display():
    rec = bom_catalog.validate_region({"name": "  POLANDCENTRAL  ", "has_az": True})
    assert rec == {"name": "polandcentral",
                   "display_name": "Polandcentral",  # title-cased fallback
                   "has_az": True}


def test_validate_region_keeps_explicit_display_and_has_az_defaults_false():
    rec = bom_catalog.validate_region(
        {"name": "qatarcentral", "display_name": "Qatar Central"},
    )
    assert rec["display_name"] == "Qatar Central"
    assert rec["has_az"] is False


@pytest.mark.parametrize("bad", [
    {"name": "Bad-Region"},   # uppercase
    {"name": "1eastus"},      # starts with digit
    {"name": "x"},            # too short
    {"name": "east us"},      # space
    {"name": "a" * 70},       # too long
    {"name": ""},             # empty
])
def test_validate_region_rejects_bad_names(bad):
    with pytest.raises(bom_catalog.BomCatalogError) as ex:
        bom_catalog.validate_region(bad)
    assert ex.value.code == "bad_region"


def test_validate_service_normalizes_name():
    rec = bom_catalog.validate_service({
        "name": "  Azure   Foo  ",
        "provider": "Microsoft.Foo",
        "resource_type": "fooThings",
        "zone_check": True,
    })
    assert rec == {"name": "Azure Foo", "provider": "Microsoft.Foo",
                   "resource_type": "fooThings", "zone_check": True}


@pytest.mark.parametrize("provider", [
    "microsoft", "Microsoft", "Microsoft.", "FooBar",
    "Microsoft.Foo.Bar",  # too many dotted segments
    "1Microsoft.Foo",     # starts with digit
])
def test_validate_service_rejects_bad_provider(provider):
    with pytest.raises(bom_catalog.BomCatalogError) as ex:
        bom_catalog.validate_service({
            "name": "Azure Foo",
            "provider": provider,
            "resource_type": "fooThings",
        })
    assert ex.value.code == "bad_service"


@pytest.mark.parametrize("rt", [
    "foo-things",  # dash not allowed
    "1foothings",  # starts with digit
    "",
])
def test_validate_service_rejects_bad_resource_type(rt):
    with pytest.raises(bom_catalog.BomCatalogError):
        bom_catalog.validate_service({
            "name": "Azure Foo",
            "provider": "Microsoft.Foo",
            "resource_type": rt,
        })


# ─── bom_regions seed loader ───────────────────────────────────────────────

def test_load_builtin_returns_normalized_list():
    bom_regions._BUILTIN_CACHE = None  # reset cache so reload is exercised
    items = bom_regions._load_builtin()
    assert len(items) >= 30, "seed should ship the default Azure region set"
    names = {r["name"] for r in items}
    # spot-check a few canonical regions
    for n in ("eastus", "westus2", "uksouth", "swedencentral"):
        assert n in names
    # display names should be non-empty and title-cased
    for r in items:
        assert isinstance(r["display_name"], str) and r["display_name"]
        assert isinstance(r["has_az"], bool)


# Source of truth: https://learn.microsoft.com/en-us/azure/reliability/regions-list
# (Azure public cloud, Yes ✓ for Availability zone support). These tests
# guard against accidental drift if someone hand-edits the seed JSON.
_AZ_REGIONS_PER_MS_DOCS = {
    "australiaeast", "austriaeast", "belgiumcentral", "brazilsouth",
    "canadacentral", "centralindia", "centralus", "chilecentral",
    "denmarkeast", "eastasia", "eastus", "eastus2", "francecentral",
    "germanywestcentral", "indonesiacentral", "israelcentral", "italynorth",
    "japaneast", "japanwest", "koreacentral", "malaysiawest", "mexicocentral",
    "newzealandnorth", "northeurope", "norwayeast", "polandcentral",
    "qatarcentral", "southafricanorth", "southcentralus", "southeastasia",
    "spaincentral", "swedencentral", "switzerlandnorth", "uaenorth",
    "uksouth", "westeurope", "westus2", "westus3",
}

# Regions the docs list without AZ support. The catalog ships these so
# the "No AZs only" filter in the BOM editor is meaningful.
_NO_AZ_REGIONS_PER_MS_DOCS = {
    "australiacentral", "australiacentral2", "australiasoutheast",
    "brazilsoutheast", "canadaeast", "francesouth", "germanynorth",
    "koreasouth", "northcentralus", "norwaywest", "southafricawest",
    "southindia", "switzerlandwest", "uaecentral", "ukwest",
    "westcentralus", "westindia", "westus",
}


def test_seed_has_az_flags_match_ms_docs():
    bom_regions._BUILTIN_CACHE = None
    by_name = {r["name"]: r for r in bom_regions._load_builtin()}
    # Every AZ-supporting region per the docs must be in the seed
    # with has_az=True. This catches the class of bug where someone
    # adds a new region and forgets to mark its AZ status.
    for name in _AZ_REGIONS_PER_MS_DOCS:
        assert name in by_name, f"AZ region {name!r} missing from seed"
        assert by_name[name]["has_az"] is True, (
            f"{name!r}: has_az should be True per MS docs but seed says False"
        )
    for name in _NO_AZ_REGIONS_PER_MS_DOCS:
        if name not in by_name:
            # No-AZ regions are optional in the seed (we ship them so
            # the picker filter is useful, but skipping one isn't a bug).
            continue
        assert by_name[name]["has_az"] is False, (
            f"{name!r}: has_az should be False per MS docs but seed says True"
        )


def test_seed_has_no_unknown_regions():
    """Catch typos in the seed — every region should be either in the
    AZ-yes or AZ-no canonical lists from MS docs."""
    bom_regions._BUILTIN_CACHE = None
    known = _AZ_REGIONS_PER_MS_DOCS | _NO_AZ_REGIONS_PER_MS_DOCS
    for r in bom_regions._load_builtin():
        assert r["name"] in known, (
            f"Region {r['name']!r} is in the seed but not in the MS docs "
            "lists in this test. If MS added a new region, update the "
            "test sets too."
        )


def test_merged_catalog_overlays_custom_on_top_of_builtin(monkeypatch):
    monkeypatch.setattr(bom_catalog, "list_custom",
                        lambda kind: [
                            {"name": "atlantisnorth",
                             "display_name": "Atlantis North", "has_az": True}
                        ] if kind == "region" else [])
    merged = bom_regions.load_merged_catalog()
    names = [r["name"] for r in merged]
    assert "atlantisnorth" in names
    # Custom entries are flagged so the picker can show a delete button.
    cust = next(r for r in merged if r["name"] == "atlantisnorth")
    assert cust["is_custom"] is True
    # Built-ins are not flagged custom.
    bi = next(r for r in merged if r["name"] == "eastus")
    assert bi["is_custom"] is False


def test_validate_region_names_rejects_unknown(monkeypatch):
    monkeypatch.setattr(bom_catalog, "list_custom", lambda kind: [])
    with pytest.raises(bom_catalog.BomCatalogError) as ex:
        bom_regions.validate_region_names(["eastus", "narnia"])
    assert ex.value.code == "unknown_regions"
    assert "narnia" in ex.value.message


def test_validate_region_names_accepts_and_dedupes(monkeypatch):
    monkeypatch.setattr(bom_catalog, "list_custom", lambda kind: [])
    out = bom_regions.validate_region_names(
        ["EastUS", "westus2", "eastus", "  uksouth  "],
    )
    # Deduped (case-insensitive) and lowercased.
    assert out == ["eastus", "westus2", "uksouth"]


# ─── bom_services.load_catalog merge ───────────────────────────────────────

def test_load_catalog_merges_custom_with_builtin(monkeypatch):
    monkeypatch.setattr(bom_catalog, "list_custom",
                        lambda kind: [
                            {"name": "Azure Custom Foo",
                             "provider": "Microsoft.Foo",
                             "resource_type": "fooThings",
                             "zone_check": False}
                        ] if kind == "service" else [])
    cat = bom_services.load_catalog()
    by_name = {s["name"]: s for s in cat}
    assert "Azure Custom Foo" in by_name
    assert by_name["Azure Custom Foo"]["is_custom"] is True
    # Built-ins should still be present and marked is_custom=False.
    assert "Azure Automation" in by_name
    assert by_name["Azure Automation"]["is_custom"] is False
    # Sort order: built-ins (is_custom=False) before customs.
    custom_idx = next(i for i, s in enumerate(cat) if s["name"] == "Azure Custom Foo")
    builtin_idx = next(i for i, s in enumerate(cat) if s["name"] == "Azure Automation")
    assert builtin_idx < custom_idx


def test_load_catalog_skips_custom_that_shadows_builtin(monkeypatch):
    # Built-in "Azure Automation" exists; an old custom entry by the
    # same name must NOT shadow it on read (defense-in-depth).
    monkeypatch.setattr(bom_catalog, "list_custom",
                        lambda kind: [
                            {"name": "Azure Automation",
                             "provider": "Microsoft.Other",
                             "resource_type": "otherThings"}
                        ] if kind == "service" else [])
    cat = bom_services.load_catalog()
    autos = [s for s in cat if s["name"] == "Azure Automation"]
    assert len(autos) == 1
    assert autos[0]["provider"] == "Microsoft.Automation"
    assert autos[0]["is_custom"] is False


def test_catalog_by_name_strips_is_custom(monkeypatch):
    monkeypatch.setattr(bom_catalog, "list_custom", lambda kind: [])
    by = bom_services.catalog_by_name()
    for entry in by.values():
        assert "is_custom" not in entry


# ─── add_region / add_service unique check ─────────────────────────────────

def test_add_region_rejects_collision_with_builtin(monkeypatch):
    monkeypatch.setattr(bom_catalog, "list_custom", lambda kind: [])
    with pytest.raises(bom_catalog.BomCatalogError) as ex:
        bom_catalog.add_region({"name": "eastus", "display_name": "East US"},
                               existing_builtin_names=["eastus", "westus2"])
    assert ex.value.code == "duplicate_region"


def test_add_service_rejects_collision_with_builtin(monkeypatch):
    monkeypatch.setattr(bom_catalog, "list_custom", lambda kind: [])
    with pytest.raises(bom_catalog.BomCatalogError) as ex:
        bom_catalog.add_service(
            {"name": "azure automation",   # case-insensitive collision
             "provider": "Microsoft.Other", "resource_type": "things"},
            existing_builtin_names=["Azure Automation", "Premium SSD v2"],
        )
    assert ex.value.code == "duplicate_service"

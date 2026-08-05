"""Tests for the new regions field on bom_storage records.

Validator-only — CRUD against Azurite is exercised manually via
end-to-end smoke tests on the dev stack.
"""
import pytest

from _shared import bom_catalog, bom_storage


def test_validate_regions_accepts_known(monkeypatch):
    # Use the seed catalog (no custom). Spot-check a few that ship.
    monkeypatch.setattr(bom_catalog, "list_custom", lambda kind: [])
    out = bom_storage._validate_regions(
        [{"name": "eastus"}, "westus2", "EastUS", "uksouth"],
    )
    assert out == ["eastus", "westus2", "uksouth"]


def test_validate_regions_empty_returns_empty(monkeypatch):
    monkeypatch.setattr(bom_catalog, "list_custom", lambda kind: [])
    assert bom_storage._validate_regions([]) == []
    assert bom_storage._validate_regions(None) == []


def test_validate_regions_rejects_unknown(monkeypatch):
    monkeypatch.setattr(bom_catalog, "list_custom", lambda kind: [])
    with pytest.raises(bom_storage.BomStorageError) as ex:
        bom_storage._validate_regions(["eastus", "narnia"])
    # Translated from bom_catalog.BomCatalogError → BomStorageError.
    assert ex.value.code == "unknown_regions"
    assert "narnia" in ex.value.message


def test_validate_regions_caps_at_max(monkeypatch):
    monkeypatch.setattr(bom_catalog, "list_custom", lambda kind: [])
    too_many = [f"r{i}" for i in range(bom_storage.MAX_REGIONS + 1)]
    with pytest.raises(bom_storage.BomStorageError) as ex:
        bom_storage._validate_regions(too_many)
    assert ex.value.code == "bad_regions"


def test_validate_regions_rejects_non_list(monkeypatch):
    monkeypatch.setattr(bom_catalog, "list_custom", lambda kind: [])
    with pytest.raises(bom_storage.BomStorageError):
        bom_storage._validate_regions("eastus,westus2")  # CSV string not allowed

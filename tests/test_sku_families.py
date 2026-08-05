"""Tests for api/_shared/sku_families.py — the BOM editor's SKU family picker
data source. ARM is mocked so these are deterministic and offline."""
import pytest

from _shared import sku_families


@pytest.fixture(autouse=True)
def _reset_cache():
    # The module caches the merged list in-process; clear it around each test.
    sku_families._CACHE = None
    yield
    sku_families._CACHE = None


def test_seed_families_are_well_formed():
    seed = sku_families._load_seed_families()
    assert seed, "bundled skus.txt should yield at least one family"
    assert all(sku_families._FAMILY_RE.match(f) for f in seed)


def test_merge_dedups_case_insensitively_arm_casing_wins():
    merged = sku_families._merge(
        ["standarddav6family", "standardDSv3Family"],
        ["standardDav6Family", "standardEav6Family"],
    )
    # ARM casing wins for the duplicate; union is sorted case-insensitively.
    assert "standardDav6Family" in merged["families"]
    assert "standarddav6family" not in merged["families"]
    assert "standardEav6Family" in merged["families"]
    assert "standardDSv3Family" in merged["families"]
    assert merged["source"] == "arm+builtin"


def test_merge_seed_only_when_no_arm():
    merged = sku_families._merge(["standardDav6Family"], None)
    assert merged["families"] == ["standardDav6Family"]
    assert merged["source"] == "builtin"
    assert merged["families_rich"] == [{"id": "standardDav6Family", "label": "Dav6 Series"}]


def test_load_families_falls_back_to_seed_when_arm_unavailable(monkeypatch):
    monkeypatch.setattr(sku_families, "_families_from_arm", lambda *a, **k: None)
    out = sku_families.load_families()
    assert out["source"] == "builtin"
    assert "standardDav6Family" in out["families"]


def test_load_families_includes_live_arm_values(monkeypatch):
    monkeypatch.setattr(
        sku_families, "_families_from_arm",
        lambda *a, **k: ["standardNewFamilyV9", "standardDav6Family"],
    )
    out = sku_families.load_families(refresh=True)
    assert out["source"] == "arm+builtin"
    assert "standardNewFamilyV9" in out["families"]
    # Seed entries are still present alongside the live ARM ones.
    assert "standardDav6Family" in out["families"]


def test_non_refresh_never_calls_arm(monkeypatch):
    called = {"n": 0}
    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("ARM must not be called on the non-refresh path")
    monkeypatch.setattr(sku_families, "_families_from_arm", _boom)
    out = sku_families.load_families()  # refresh=False
    assert called["n"] == 0
    assert out["source"] == "builtin"
    assert out["families"]


def test_refresh_failure_keeps_cached_arm_list(monkeypatch):
    # Cache a rich ARM-backed list.
    monkeypatch.setattr(
        sku_families, "_families_from_arm",
        lambda *a, **k: ["standardArmOnlyFamily"],
    )
    first = sku_families.load_families(refresh=True)
    assert first["source"] == "arm+builtin"
    # A *refresh* that fails (ARM returns None) must NOT downgrade the cache.
    monkeypatch.setattr(sku_families, "_families_from_arm", lambda *a, **k: None)
    second = sku_families.load_families(refresh=True)
    assert second["source"] == "arm+builtin"
    assert "standardArmOnlyFamily" in second["families"]


def test_refresh_does_not_downgrade_cached_arm_result(monkeypatch):
    # First load gets a rich ARM-backed list and caches it.
    monkeypatch.setattr(
        sku_families, "_families_from_arm",
        lambda *a, **k: ["standardArmOnlyFamily"],
    )
    first = sku_families.load_families(refresh=True)
    assert first["source"] == "arm+builtin"
    # A later non-refresh, seed-only call must not clobber the richer cache.
    monkeypatch.setattr(sku_families, "_families_from_arm", lambda *a, **k: None)
    second = sku_families.load_families(refresh=False)
    assert second["source"] == "arm+builtin"
    assert "standardArmOnlyFamily" in second["families"]

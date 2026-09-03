"""Tests for the uploadable dataset override layer (dataset_store)."""
import json

import pytest

from _shared import dataset_store


@pytest.fixture(autouse=True)
def _isolate_overrides(monkeypatch, tmp_path):
    # Send overrides to a temp dir so we never touch the real local-storage.
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    yield


def test_resolve_falls_back_to_packaged_seed():
    path = dataset_store.resolve_path("region_catalog")
    assert path.endswith("bom_region_catalog.json")
    assert "data" in path  # the packaged api/_shared/data dir
    assert not dataset_store.has_override("region_catalog")


def test_save_override_then_resolve_prefers_override(tmp_path):
    payload = json.dumps({"regions": [{"name": "eastus", "display_name": "East US", "has_az": True}]})
    info = dataset_store.save_override("region_catalog", payload.encode("utf-8"))
    assert info["source"] == "custom"
    assert info["rows"] if "rows" in info else True
    assert dataset_store.has_override("region_catalog")
    resolved = dataset_store.resolve_path("region_catalog")
    assert resolved == dataset_store.override_path("region_catalog")
    with open(resolved, "r", encoding="utf-8") as f:
        assert "eastus" in f.read()


def test_reset_reverts_to_seed():
    dataset_store.save_override("regions_list", b"eastus\nwestus\n")
    assert dataset_store.has_override("regions_list")
    info = dataset_store.reset_override("regions_list")
    assert info["source"] == "builtin"
    assert not dataset_store.has_override("regions_list")
    # resolve now points back at the packaged seed
    assert dataset_store.resolve_path("regions_list").endswith("regions.txt")


def test_unknown_dataset_raises():
    with pytest.raises(dataset_store.DatasetError) as ei:
        dataset_store.save_override("nope", b"x")
    assert ei.value.status == 404


def test_latency_validation_rejects_bad_header():
    with pytest.raises(dataset_store.DatasetError):
        dataset_store.save_override("latency", b"Region,East US\nWest US,5\n")


def test_latency_validation_accepts_matrix():
    csv = b"Source,East US,West US\nEast US,,68\nWest US,68,\n"
    info = dataset_store.save_override("latency", csv)
    assert info["source"] == "custom"
    assert "destinations" in info["summary"]


def test_region_catalog_requires_regions_array():
    with pytest.raises(dataset_store.DatasetError):
        dataset_store.save_override("region_catalog", b'{"foo": []}')


def test_service_catalog_requires_full_entries():
    good = json.dumps({"services": [
        {"name": "Foo", "provider": "Microsoft.Foo", "resource_type": "bars"}]})
    info = dataset_store.save_override("service_catalog", good.encode())
    assert "1 services" in info["summary"]
    with pytest.raises(dataset_store.DatasetError):
        dataset_store.save_override("service_catalog", b'{"services": [{"name": "x"}]}')


def test_empty_and_oversize_rejected():
    with pytest.raises(dataset_store.DatasetError):
        dataset_store.save_override("regions_list", b"")
    big = b"eastus\n" * (dataset_store.MAX_DATASET_BYTES // 6)
    with pytest.raises(dataset_store.DatasetError):
        dataset_store.save_override("regions_list", big + b"x" * dataset_store.MAX_DATASET_BYTES)


def test_upload_invalidates_region_catalog_cache():
    """A fresh region-catalog upload is reflected by bom_regions without a
    restart (cache invalidation wired through _invalidate_caches)."""
    from _shared import bom_regions
    bom_regions.reset_dataset_caches()
    before = {r["name"] for r in bom_regions.load_merged_catalog()}
    payload = json.dumps({"regions": [
        {"name": "zzztestregion", "display_name": "ZZZ Test", "has_az": True}]})
    dataset_store.save_override("region_catalog", payload.encode())
    after = {r["name"] for r in bom_regions.load_merged_catalog()}
    assert "zzztestregion" in after
    assert after != before
    # Cleanup so other tests using the real catalog aren't affected.
    dataset_store.reset_override("region_catalog")
    bom_regions.reset_dataset_caches()

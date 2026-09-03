"""Tests for api/_shared/bom_storage.py — validators (storage-layer
sanity). Skips CRUD tests that require Azurite to be running."""
import pytest

from _shared import bom_storage


# ─── Validators ────────────────────────────────────────────────────────────

def test_validate_sub_id_lowercases_and_accepts_guid():
    assert bom_storage._validate_sub_id(
        "11111111-1111-1111-1111-111111111111"
    ) == "11111111-1111-1111-1111-111111111111"
    # Uppercase normalizes
    assert bom_storage._validate_sub_id(
        "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
    ) == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def test_validate_sub_id_rejects_non_guid():
    with pytest.raises(bom_storage.BomStorageError) as ex:
        bom_storage._validate_sub_id("not-a-guid")
    assert ex.value.code == "bad_subscription"


def test_normalize_subscription_ids_prefers_list_and_dedupes():
    out = bom_storage._normalize_subscription_ids(
        "11111111-1111-1111-1111-111111111111",
        [
            "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "11111111-1111-1111-1111-111111111111",
        ],
    )
    assert out == [
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "11111111-1111-1111-1111-111111111111",
    ]


def test_entity_to_record_reads_subscription_ids_json():
    rec = bom_storage._entity_to_record({
        "PartitionKey": "sub",
        "RowKey": "bom-1",
        "subscription_id": "11111111-1111-1111-1111-111111111111",
        "subscription_ids_json": '["22222222-2222-2222-2222-222222222222","33333333-3333-3333-3333-333333333333"]',
    })
    assert rec["subscription_id"] == "22222222-2222-2222-2222-222222222222"
    assert rec["subscription_ids"] == [
        "22222222-2222-2222-2222-222222222222",
        "33333333-3333-3333-3333-333333333333",
    ]


def test_validate_tag_charset_and_length():
    assert bom_storage._validate_tag("Avaya-Prod_East") == "Avaya-Prod_East"
    assert bom_storage._validate_tag("Tag (test)") == "Tag (test)"
    assert bom_storage._validate_tag(None) is None
    assert bom_storage._validate_tag("") is None
    with pytest.raises(bom_storage.BomStorageError):
        bom_storage._validate_tag("bad<tag>")  # angle brackets disallowed
    with pytest.raises(bom_storage.BomStorageError):
        bom_storage._validate_tag("x" * 200)   # length cap


def test_validate_segments_defaults_to_ea_any():
    assert bom_storage._validate_segments(None) == "EA,ANY"
    assert bom_storage._validate_segments("") == "EA,ANY"
    assert bom_storage._validate_segments("ea, any") == "EA,ANY"
    assert bom_storage._validate_segments("MOSP,INTERNAL") == "MOSP,INTERNAL"


def test_validate_segments_rejects_unknown():
    with pytest.raises(bom_storage.BomStorageError) as ex:
        bom_storage._validate_segments("EA,VIP")
    assert ex.value.code == "bad_segments"
    assert "VIP" in ex.value.message


def test_validate_resilience_defaults_and_normalizes():
    # Missing/blank/unknown → zone_redundant (preserves legacy blocking behavior).
    assert bom_storage._validate_resilience(None) == "zone_redundant"
    assert bom_storage._validate_resilience("") == "zone_redundant"
    assert bom_storage._validate_resilience("bogus") == "zone_redundant"
    # Recognized values pass through, case-insensitively.
    assert bom_storage._validate_resilience("regional") == "regional"
    assert bom_storage._validate_resilience("REGIONAL") == "regional"
    assert bom_storage._validate_resilience("zone_redundant") == "zone_redundant"


def test_entity_to_record_defaults_resilience():
    rec = bom_storage._entity_to_record({"RowKey": "bom-x", "subscription_id": "s"})
    assert rec["resilience"] == "zone_redundant"
    rec2 = bom_storage._entity_to_record({"RowKey": "bom-y", "subscription_id": "s", "resilience": "regional"})
    assert rec2["resilience"] == "regional"


def test_validate_preferred_region_trims_and_caps():
    assert bom_storage._validate_preferred_region(None) == ""
    assert bom_storage._validate_preferred_region("") == ""
    assert bom_storage._validate_preferred_region("  eastus  ") == "eastus"
    capped = bom_storage._validate_preferred_region("x" * 500)
    assert len(capped) == bom_storage.MAX_PREFERRED_REGION_LEN


def test_entity_to_record_reads_preferred_region():
    rec = bom_storage._entity_to_record({"RowKey": "bom-z", "subscription_id": "s"})
    assert rec["preferred_region"] is None
    rec2 = bom_storage._entity_to_record(
        {"RowKey": "bom-z2", "subscription_id": "s", "preferred_region": "eastus"}
    )
    assert rec2["preferred_region"] == "eastus"


def test_validate_services_resolves_against_catalog():
    out = bom_storage._validate_services([
        {"name": "Azure Automation"},
        {"name": "Premium SSD v2"},
        "Azure Firewall",  # string form is also accepted
    ])
    names = [s["name"] for s in out]
    assert names == ["Azure Automation", "Premium SSD v2", "Azure Firewall"]


def test_validate_services_rejects_unknown_via_catalog():
    with pytest.raises(bom_storage.BomStorageError) as ex:
        bom_storage._validate_services([{"name": "Made Up Service"}])
    assert ex.value.code == "unknown_services"


def test_validate_services_empty_list_allowed():
    assert bom_storage._validate_services([]) == []


def test_validate_services_persists_valid_tier():
    from _shared import bom_services
    out = bom_storage._validate_services([
        {"name": "Azure SQL Database", "tier": "Business_Critical"},
        {"name": "Azure Automation"},  # no tiers → no tier key
    ])
    assert out[0]["name"] == "Azure SQL Database"
    # Normalizes to the catalog's canonical id casing.
    assert out[0]["tier"] == "business_critical"
    valid_ids = {t["id"] for t in bom_services.tiers_for_service("Azure SQL Database")}
    assert out[0]["tier"] in valid_ids
    assert "tier" not in out[1]


def test_validate_services_rejects_invalid_tier():
    with pytest.raises(bom_storage.BomStorageError) as ex:
        bom_storage._validate_services([
            {"name": "Azure SQL Database", "tier": "does-not-exist"},
        ])
    assert ex.value.code == "bad_services"


def test_validate_services_rejects_tier_on_non_tiered_service():
    with pytest.raises(bom_storage.BomStorageError) as ex:
        bom_storage._validate_services([
            {"name": "Azure Automation", "tier": "premium"},
        ])
    assert ex.value.code == "bad_services"


def test_validate_required_skus_normalizes_via_compile_validator():
    skus = [{
        "primary_family": "standardDav6Family",
        "primary_label": "Dav6",
        "alt_family": "standardDASv5Family",
        "alt_label": "Dasv5",
        "required_cores": 100,
    }]
    out = bom_storage._validate_required_skus(skus)
    assert len(out) == 1
    assert out[0]["primary_family"] == "standardDav6Family"
    assert out[0]["required_cores"] == 100


def test_validate_required_skus_empty_allowed_for_partial_save():
    assert bom_storage._validate_required_skus([]) == []


def test_validate_required_skus_rejects_non_list():
    with pytest.raises(bom_storage.BomStorageError) as ex:
        bom_storage._validate_required_skus("not a list")
    assert ex.value.code == "bad_required_skus"


def test_validate_required_skus_rejects_bad_row():
    # missing primary_family — compile._validate_required_families should reject
    with pytest.raises(bom_storage.BomStorageError):
        bom_storage._validate_required_skus([
            {"primary_label": "Dav6", "required_cores": 100},
        ])


def test_ensure_json_size_rejects_oversize():
    with pytest.raises(bom_storage.BomStorageError) as ex:
        bom_storage._ensure_json_size("x" * (bom_storage.MAX_JSON_BYTES + 1),
                                       what="test")
    assert ex.value.code == "payload_too_large"

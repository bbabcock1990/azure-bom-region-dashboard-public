"""Tests for compile.compile_snapshot's new bom_data kwarg path used by
the in-app BOM editor."""
import pytest

from _shared import compile as compile_mod
from _shared import bom_services
from _shared import arm_skus


def _make_bom_data(regions, required_families):
    header, records = bom_services.synthesize_empty_bom(
        [{"name": r, "display_name": r} for r in regions]
    )
    return {
        "bom_header": header,
        "bom_records": records,
        "required_families": required_families,
    }


def test_compile_snapshot_rejects_both_step2_and_bom_data():
    with pytest.raises(compile_mod.CompileError) as ex:
        compile_mod.compile_snapshot(
            subscription_id="11111111-1111-1111-1111-111111111111",
            arm_token="t",
            step2_bytes=b"x",
            bom_data=_make_bom_data(["eastus"], [{
                "primary_family": "standardDav6Family",
                "primary_label": "Dav6",
                "alt_family": None,
                "alt_label": None,
                "required_cores": 100,
            }]),
            triggered_by_email="t@example.com",
            triggered_by_oid="oid",
        )
    assert ex.value.code == "bom_source_conflict"


def test_compile_snapshot_rejects_neither_step2_nor_bom_data():
    with pytest.raises(compile_mod.CompileError):
        compile_mod.compile_snapshot(
            subscription_id="11111111-1111-1111-1111-111111111111",
            arm_token="t",
            triggered_by_email="t@example.com",
            triggered_by_oid="oid",
        )


def test_compile_snapshot_marks_global_unscoped_when_target_has_no_access(monkeypatch):
    calls = []

    def fake_fetch(*, arm_token, subscription_id, want_regions, want_families, **kwargs):
        calls.append(subscription_id)
        return [{
            "region": "eastus",
            "family": "standardDav6Family",
            "display": "Dav6",
            "zones": [True, True, True],
            "sub_restricted": False,
            "sub_restriction_raw": "Available",
        }]

    monkeypatch.setattr(compile_mod.arm_sku_availability, "fetch_arm_sku_records", fake_fetch)
    monkeypatch.setattr(compile_mod.quota_groups, "check_quota_groups", lambda *args, **kwargs: {
        "subscription_id": kwargs.get("subscription_id") if kwargs else None,
        "status": "unknown",
        "has_quota_groups": False,
        "groups": [],
    })
    monkeypatch.setattr(compile_mod.quota_groups, "check_subscription_quota", lambda *args, **kwargs: {
        "subscription_id": kwargs.get("subscription_id") if kwargs else args[1],
        "status": "unknown",
        "regions": {},
    })

    snap = compile_mod.compile_snapshot(
        subscription_id="11111111-1111-1111-1111-111111111111",
        subscriptions=[
            {
                "subscription_id": "11111111-1111-1111-1111-111111111111",
                "arm_token": None,
                "status": "no_access",
                "error": "Need Reader role on the subscription.",
                "role": "target",
            },
            {
                "subscription_id": "22222222-2222-2222-2222-222222222222",
                "arm_token": "fallback-token",
                "status": "ok",
                "role": "operator_fallback",
            },
        ],
        bom_data=_make_bom_data(["eastus"], [{
            "primary_family": "standardDav6Family",
            "primary_label": "Dav6",
            "alt_family": None,
            "alt_label": None,
            "required_cores": 100,
        }]),
        triggered_by_email="t@example.com",
        triggered_by_oid="oid",
    )

    assert calls == ["22222222-2222-2222-2222-222222222222"]
    assert snap["meta"]["mode"] == "global_unscoped"
    assert "Per-subscription restrictions not evaluated" in snap["meta"]["mode_note"]
    assert snap["meta"]["sku_query_subscription_id"] == "22222222-2222-2222-2222-222222222222"


def test_compile_snapshot_raises_on_target_401_for_refresh(monkeypatch):
    monkeypatch.setattr(
        compile_mod.arm_sku_availability,
        "fetch_arm_sku_records",
        lambda **kwargs: (_ for _ in ()).throw(
            arm_skus.ArmError("arm_token_expired", "ARM rejected the token (401). Re-mint and retry.", 401)
        ),
    )

    with pytest.raises(compile_mod.CompileError) as ex:
        compile_mod.compile_snapshot(
            subscription_id="11111111-1111-1111-1111-111111111111",
            subscriptions=[{
                "subscription_id": "11111111-1111-1111-1111-111111111111",
                "arm_token": "stale-token",
                "status": "ok",
                "role": "target",
            }],
            bom_data=_make_bom_data(["eastus"], [{
                "primary_family": "standardDav6Family",
                "primary_label": "Dav6",
                "alt_family": None,
                "alt_label": None,
                "required_cores": 100,
            }]),
            triggered_by_email="t@example.com",
            triggered_by_oid="oid",
        )

    assert ex.value.code == "arm_arm_token_expired"

import pytest

from _shared import bom_services
from _shared import compile as compile_mod


def _make_bom_data(regions, required_families):
    header, records = bom_services.synthesize_empty_bom(
        [{"name": r, "display_name": r} for r in regions]
    )
    return {
        "bom_header": header,
        "bom_records": records,
        "required_families": required_families,
    }


def test_compile_snapshot_merges_multi_subscription_results(monkeypatch):
    required = [{
        "primary_family": "standardDav6Family",
        "primary_label": "Dav6",
        "alt_family": None,
        "alt_label": None,
        "required_cores": 100,
    }]

    def fake_fetch(*, subscription_id, **kwargs):
        if subscription_id.endswith("0001"):
            return [{
                "region": "eastus",
                "family": "standardDav6Family",
                "display": "Dav6 Series",
                "zones": [True, False, False],
                "sub_restricted": False,
                "sub_restriction_raw": "Available",
            }]
        return [{
            "region": "eastus",
            "family": "standardDav6Family",
            "display": "Dav6 Series",
            "zones": [False, True, False],
            "sub_restricted": False,
            "sub_restriction_raw": "Available",
        }]

    monkeypatch.setattr(compile_mod.arm_sku_availability, "fetch_arm_sku_records", fake_fetch)
    monkeypatch.setattr(
        compile_mod.quota_groups,
        "check_quota_groups",
        lambda *args, **kwargs: {
            "subscription_id": kwargs.get("subscription_id") or args[1],
            "status": "no_quota_group",
            "has_quota_groups": False,
            "groups": [],
        },
    )
    monkeypatch.setattr(
        compile_mod.quota_groups,
        "check_subscription_quota",
        lambda *args, **kwargs: {
            "subscription_id": kwargs.get("subscription_id") or args[1],
            "status": "ok",
            "regions": {},
        },
    )

    snap = compile_mod.compile_snapshot(
        subscriptions=[
            {"subscription_id": "00000000-0000-0000-0000-000000000001", "arm_token": "tok-1"},
            {"subscription_id": "00000000-0000-0000-0000-000000000002", "arm_token": "tok-2"},
            {"subscription_id": "00000000-0000-0000-0000-000000000003", "arm_token": None, "status": "no_access", "error": "not signed in"},
        ],
        bom_data=_make_bom_data(["eastus"], required),
        regions=["eastus"],
        triggered_by_email="t@example.com",
        triggered_by_oid="oid-1",
    )

    row = snap["sku_records"][0]
    assert row["zones"] == [True, True, False]
    assert snap["meta"]["subscription_id"] == "00000000-0000-0000-0000-000000000001"
    assert snap["meta"]["subscription_ids"] == [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        "00000000-0000-0000-0000-000000000003",
    ]
    assert snap["meta"]["per_sub_status"]["00000000-0000-0000-0000-000000000003"]["status"] == "no_access"
    assert row["per_sub_results"]["00000000-0000-0000-0000-000000000003"]["status"] == "no_access"
    assert "00000000-0000-0000-0000-000000000002" in snap["per_sub_results"]


def test_compile_snapshot_sets_quota_group_region_status(monkeypatch):
    required = [{
        "primary_family": "standardDav6Family",
        "primary_label": "Dav6",
        "alt_family": None,
        "alt_label": None,
        "required_cores": 100,
    }]

    monkeypatch.setattr(
        compile_mod.arm_sku_availability,
        "fetch_arm_sku_records",
        lambda **kwargs: [{
            "region": "eastus",
            "family": "standardDav6Family",
            "display": "Dav6 Series",
            "zones": [True, True, True],
            "sub_restricted": False,
            "sub_restriction_raw": "Available",
        }],
    )
    monkeypatch.setattr(
        compile_mod.quota_groups,
        "check_quota_groups",
        lambda *args, **kwargs: {
            "subscription_id": kwargs.get("subscription_id") or args[1],
            "status": "ok",
            "has_quota_groups": True,
            "groups": [{
                "name": "quota-group-a",
                "region": "eastus",
                "families": [{
                    "family": "standardDav6Family",
                    "limit": 200,
                    "usage": 50,
                }],
            }],
        },
    )
    monkeypatch.setattr(
        compile_mod.quota_groups,
        "check_subscription_quota",
        lambda *args, **kwargs: {
            "subscription_id": kwargs.get("subscription_id") or args[1],
            "status": "ok",
            "regions": {
                "eastus": {
                    "status": "ok",
                    "families": {
                        "standardDav6Family": {"limit": 120, "usage": 80, "headroom": 40},
                    },
                    "total_regional": {"limit": 300, "usage": 120, "headroom": 180},
                },
            },
        },
    )

    snap = compile_mod.compile_snapshot(
        subscription_id="00000000-0000-0000-0000-000000000001",
        arm_token="tok-1",
        bom_data=_make_bom_data(["eastus"], required),
        regions=["eastus"],
        triggered_by_email="t@example.com",
        triggered_by_oid="oid-1",
    )

    region = snap["regions"][0]
    assert region["quota_status"] == "sufficient"
    assert region["quota_tiers"]["families"]["standarddav6family"]["satisfied_by"] == "quota_group"
    assert region["quota_subscriptions"][0]["status"] == "sufficient_group"
    assert region["quota_subscriptions"][0]["groups"][0]["name"] == "quota-group-a"


def test_compile_snapshot_keeps_cross_sub_quota_informational_only(monkeypatch):
    required = [{
        "primary_family": "standardDav6Family",
        "primary_label": "Dav6",
        "alt_family": None,
        "alt_label": None,
        "required_cores": 100,
    }]

    monkeypatch.setattr(
        compile_mod.arm_sku_availability,
        "fetch_arm_sku_records",
        lambda **kwargs: [{
            "region": "eastus",
            "family": "standardDav6Family",
            "display": "Dav6 Series",
            "zones": [True, True, True],
            "sub_restricted": False,
            "sub_restriction_raw": "Available",
        }],
    )
    monkeypatch.setattr(
        compile_mod.quota_groups,
        "check_quota_groups",
        lambda *args, **kwargs: {
            "subscription_id": kwargs.get("subscription_id") or args[1],
            "status": "no_quota_group",
            "has_quota_groups": False,
            "groups": [],
        },
    )

    def fake_sub_quota(*args, **kwargs):
        sub_id = kwargs.get("subscription_id") or args[1]
        if sub_id.endswith("0001"):
            headroom = 40
        else:
            headroom = 120
        return {
            "subscription_id": sub_id,
            "status": "ok",
            "regions": {
                "eastus": {
                    "status": "ok",
                    "families": {
                        "standardDav6Family": {"limit": 200, "usage": 200 - headroom, "headroom": headroom},
                    },
                    "total_regional": {"limit": 400, "usage": 200, "headroom": 200},
                },
            },
        }

    monkeypatch.setattr(
        compile_mod.quota_groups,
        "check_subscription_quota",
        fake_sub_quota,
    )

    snap = compile_mod.compile_snapshot(
        subscriptions=[
            {"subscription_id": "00000000-0000-0000-0000-000000000001", "arm_token": "tok-1"},
            {"subscription_id": "00000000-0000-0000-0000-000000000002", "arm_token": "tok-2"},
        ],
        bom_data=_make_bom_data(["eastus"], required),
        regions=["eastus"],
        triggered_by_email="t@example.com",
        triggered_by_oid="oid-1",
    )

    region = snap["regions"][0]
    family = region["quota_tiers"]["families"]["standarddav6family"]
    assert region["quota_status"] == "insufficient"
    assert family["satisfied_by"] is None
    assert family["tier3_cross_sub"][0]["subscription_id"].endswith("0002")
    assert family["tier3_cross_sub"][0]["sufficient"] is True


def test_compile_snapshot_adds_ready_with_constraints_verdict(monkeypatch):
    required = [{
        "primary_family": "standardDav6Family",
        "primary_label": "Dav6",
        "alt_family": "standardDasv5Family",
        "alt_label": "Dasv5",
        "required_cores": 80,
    }]

    monkeypatch.setattr(
        compile_mod.arm_sku_availability,
        "fetch_arm_sku_records",
        lambda **kwargs: [
            {
                "region": "eastus",
                "family": "standardDav6Family",
                "display": "Dav6 Series",
                "zones": [True, True, False],
                "sub_restricted": False,
                "sub_restriction_raw": "Available",
            },
            {
                "region": "eastus",
                "family": "standardDasv5Family",
                "display": "Dasv5 Series",
                "zones": [True, True, True],
                "sub_restricted": False,
                "sub_restriction_raw": "Available",
            },
        ],
    )
    monkeypatch.setattr(
        compile_mod.quota_groups,
        "check_quota_groups",
        lambda *args, **kwargs: {
            "subscription_id": kwargs.get("subscription_id") or args[1],
            "status": "no_quota_group",
            "has_quota_groups": False,
            "groups": [],
        },
    )
    monkeypatch.setattr(
        compile_mod.quota_groups,
        "check_subscription_quota",
        lambda *args, **kwargs: {
            "subscription_id": kwargs.get("subscription_id") or args[1],
            "status": "ok",
            "regions": {
                "eastus": {
                    "status": "ok",
                    "families": {
                        "standardDav6Family": {"limit": 500, "usage": 410, "headroom": 90},
                        "standardDasv5Family": {"limit": 500, "usage": 420, "headroom": 80},
                    },
                },
            },
        },
    )

    snap = compile_mod.compile_snapshot(
        subscription_id="00000000-0000-0000-0000-000000000001",
        arm_token="tok-1",
        bom_data=_make_bom_data(["eastus"], required),
        regions=["eastus"],
        triggered_by_email="t@example.com",
        triggered_by_oid="oid-1",
    )

    verdict = snap["regions"][0]["deployment_verdict"]
    assert verdict["verdict"] == "ready_with_constraints"
    assert "Using fallback SKU Dasv5 for Dav6" in verdict["constraints"]
    assert "Quota tight for Dav6 (82% used)" in verdict["constraints"]
    assert {
        "type": "zone_gap",
        "message": "Dav6 available in 2 of 3 zones (AZ 1/2); using fallback Dasv5 across all 3 zones",
        "severity": "warning",
    } in verdict["blockers"]


def test_compile_snapshot_adds_missing_service_verdict(monkeypatch):
    required = [{
        "primary_family": "standardDav6Family",
        "primary_label": "Dav6",
        "alt_family": None,
        "alt_label": None,
        "required_cores": None,
    }]

    monkeypatch.setattr(
        compile_mod.arm_sku_availability,
        "fetch_arm_sku_records",
        lambda **kwargs: [{
            "region": "eastus",
            "family": "standardDav6Family",
            "display": "Dav6 Series",
            "zones": [True, True, True],
            "sub_restricted": False,
            "sub_restriction_raw": "Available",
        }],
    )
    monkeypatch.setattr(
        compile_mod.quota_groups,
        "check_quota_groups",
        lambda *args, **kwargs: {
            "subscription_id": kwargs.get("subscription_id") or args[1],
            "status": "unknown",
            "has_quota_groups": False,
            "groups": [],
        },
    )
    monkeypatch.setattr(
        compile_mod.quota_groups,
        "check_subscription_quota",
        lambda *args, **kwargs: {
            "subscription_id": kwargs.get("subscription_id") or args[1],
            "status": "unknown",
            "regions": {},
        },
    )

    snap = compile_mod.compile_snapshot(
        subscription_id="00000000-0000-0000-0000-000000000001",
        arm_token="tok-1",
        bom_data={
            "bom_header": ["Region", "Display Name", "Overall Status", "Azure NetApp Files"],
            "bom_records": [{
                "Region": "eastus",
                "Display Name": "eastus",
                "Overall Status": "❌ UNSUPPORTED",
                "Azure NetApp Files": "❌ Not available",
            }],
            "required_families": required,
        },
        regions=["eastus"],
        triggered_by_email="t@example.com",
        triggered_by_oid="oid-1",
    )

    verdict = snap["regions"][0]["deployment_verdict"]
    assert verdict["verdict"] == "not_recommended"
    assert {
        "type": "missing_service",
        "message": "Azure NetApp Files not available (Not available)",
        "severity": "critical",
    } in verdict["blockers"]


def test_compile_snapshot_adds_needs_validation_verdict_for_access_issues(monkeypatch):
    required = [{
        "primary_family": "standardDav6Family",
        "primary_label": "Dav6",
        "alt_family": None,
        "alt_label": None,
        "required_cores": 100,
    }]

    monkeypatch.setattr(
        compile_mod.arm_sku_availability,
        "fetch_arm_sku_records",
        lambda **kwargs: [{
            "region": "eastus",
            "family": "standardDav6Family",
            "display": "Dav6 Series",
            "zones": [True, True, True],
            "sub_restricted": False,
            "sub_restriction_raw": "Available",
        }],
    )
    monkeypatch.setattr(
        compile_mod.quota_groups,
        "check_quota_groups",
        lambda *args, **kwargs: {
            "subscription_id": kwargs.get("subscription_id") or args[1],
            "status": "no_quota_group",
            "has_quota_groups": False,
            "groups": [],
        },
    )

    def fake_sub_quota(*args, **kwargs):
        sub_id = kwargs.get("subscription_id") or args[1]
        headroom = 0 if sub_id.endswith("0001") else 160
        return {
            "subscription_id": sub_id,
            "status": "ok",
            "regions": {
                "eastus": {
                    "status": "ok",
                    "families": {
                        "standardDav6Family": {"limit": 300, "usage": 300 - headroom, "headroom": headroom},
                    },
                },
            },
        }

    monkeypatch.setattr(
        compile_mod.quota_groups,
        "check_subscription_quota",
        fake_sub_quota,
    )

    snap = compile_mod.compile_snapshot(
        subscriptions=[
            {
                "subscription_id": "00000000-0000-0000-0000-000000000001",
                "arm_token": None,
                "status": "no_access",
                "error": "Need Reader role on the subscription.",
                "role": "target",
            },
            {
                "subscription_id": "00000000-0000-0000-0000-000000000002",
                "arm_token": "tok-2",
                "status": "ok",
                "role": "operator_fallback",
            },
        ],
        bom_data=_make_bom_data(["eastus"], required),
        regions=["eastus"],
        triggered_by_email="t@example.com",
        triggered_by_oid="oid-1",
    )

    verdict = snap["regions"][0]["deployment_verdict"]
    assert verdict["verdict"] == "needs_validation"
    assert any(
        blocker["type"] == "no_access" and "Per-subscription restrictions not evaluated" in blocker["message"]
        for blocker in verdict["blockers"]
    )


def test_build_quota_remediation_plan_marks_zero_headroom_as_critical():
    required = [{
        "primary_family": "standardDav6Family",
        "primary_label": "Dav6 Series",
        "alt_family": None,
        "alt_label": None,
        "required_cores": 100,
    }]
    per_sub_results = {
        "00000000-0000-0000-0000-000000000001": {
            "subscription_quota": {
                "status": "ok",
                "regions": {
                    "eastus": {
                        "status": "ok",
                        "families": {
                            "standardDav6Family": {"limit": 10, "usage": 10, "headroom": 0},
                        },
                    },
                },
            },
        },
    }
    quota_tiers = {
        "eastus": {
            "status": "insufficient",
            "families": {
                "standarddav6family": {
                    "overall_status": "insufficient",
                    "required_cores": 100,
                    "label": "Dav6 Series",
                },
            },
        },
    }

    plan = compile_mod._build_quota_remediation_plan(
        per_sub_results=per_sub_results,
        quota_tiers=quota_tiers,
        regions=[{"short": "eastus", "name": "East US"}],
        required_families=required,
    )

    assert plan == [{
        "region": "eastus",
        "region_display": "East US",
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "family": "standardDav6Family",
        "family_label": "Dav6 Series",
        "current_limit": 10,
        "current_usage": 10,
        "required_cores": 100,
        "increase_needed": 100,
        "new_limit_recommended": 110,
        "priority": "critical",
    }]


def test_compile_snapshot_adds_quota_remediation_plan(monkeypatch):
    required = [{
        "primary_family": "standardDav6Family",
        "primary_label": "Dav6 Series",
        "alt_family": None,
        "alt_label": None,
        "required_cores": 100,
    }]

    monkeypatch.setattr(
        compile_mod.arm_sku_availability,
        "fetch_arm_sku_records",
        lambda **kwargs: [{
            "region": "eastus",
            "family": "standardDav6Family",
            "display": "Dav6 Series",
            "zones": [True, True, True],
            "sub_restricted": False,
            "sub_restriction_raw": "Available",
        }],
    )
    monkeypatch.setattr(
        compile_mod.quota_groups,
        "check_quota_groups",
        lambda *args, **kwargs: {
            "subscription_id": kwargs.get("subscription_id") or args[1],
            "status": "no_quota_group",
            "has_quota_groups": False,
            "groups": [],
        },
    )

    def fake_sub_quota(*args, **kwargs):
        sub_id = kwargs.get("subscription_id") or args[1]
        if sub_id.endswith("0001"):
            return {
                "subscription_id": sub_id,
                "status": "ok",
                "regions": {
                    "eastus": {
                        "status": "ok",
                        "families": {
                            "standardDav6Family": {"limit": 10, "usage": 10, "headroom": 0},
                        },
                    },
                },
            }
        return {
            "subscription_id": sub_id,
            "status": "ok",
            "regions": {
                "eastus": {
                    "status": "ok",
                    "families": {
                        "standardDav6Family": {"limit": 60, "usage": 35, "headroom": 25},
                    },
                },
            },
        }

    monkeypatch.setattr(
        compile_mod.quota_groups,
        "check_subscription_quota",
        fake_sub_quota,
    )

    snap = compile_mod.compile_snapshot(
        subscriptions=[
            {"subscription_id": "00000000-0000-0000-0000-000000000001", "arm_token": "tok-1"},
            {"subscription_id": "00000000-0000-0000-0000-000000000002", "arm_token": "tok-2"},
        ],
        bom_data=_make_bom_data(["eastus"], required),
        regions=["eastus"],
        triggered_by_email="t@example.com",
        triggered_by_oid="oid-1",
    )

    assert snap["regions"][0]["quota_status"] == "insufficient"
    assert snap["quota_remediation"] == [{
        "region": "eastus",
        "region_display": "eastus",
        "subscription_id": "00000000-0000-0000-0000-000000000001",
        "family": "standardDav6Family",
        "family_label": "Dav6 Series",
        "current_limit": 10,
        "current_usage": 10,
        "required_cores": 100,
        "increase_needed": 100,
        "new_limit_recommended": 110,
        "priority": "critical",
    }]

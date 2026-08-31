"""Unit tests for the deployment-recommendation engine (_build_recommendations).

The engine maps blocker/restriction signals to actionable mitigations. The
headline recommendation is On-Demand Capacity Reservations (ODCR) for
capacity-constrained or restricted regions.
"""

from _shared import compile as compile_mod


def _rec_types(recs):
    return {r["type"] for r in recs}


def _by_type(recs, rec_type):
    return next(r for r in recs if r["type"] == rec_type)


def test_zone_gap_recommends_odcr_high_priority():
    recs = compile_mod._build_recommendations(
        blockers=[{"type": "zone_gap", "severity": "critical", "message": "x"}],
        region={},
        region_display="East US",
        fallback_used=False,
        restricted_notes=[],
        constrained_labels=["Dsv7"],
        quota_required=True,
    )
    assert "odcr" in _rec_types(recs)
    odcr = _by_type(recs, "odcr")
    assert odcr["priority"] == "high"
    assert "capacity-reservation" in odcr["doc_url"]
    assert "East US" in odcr["detail"]
    assert "Dsv7" in odcr["detail"]


def test_restriction_alone_recommends_odcr_medium():
    recs = compile_mod._build_recommendations(
        blockers=[],
        region={},
        region_display="Australia East",
        fallback_used=False,
        restricted_notes=["NotAvailableForSubscription"],
        constrained_labels=[],
        quota_required=True,
    )
    odcr = _by_type(recs, "odcr")
    # No hard capacity blocker -> medium priority, but still recommended
    assert odcr["priority"] == "medium"


def test_restricted_zone_gap_also_recommends_zonal_access_ticket():
    recs = compile_mod._build_recommendations(
        blockers=[{"type": "zone_gap", "severity": "critical", "message": "x"}],
        region={},
        region_display="Australia East",
        fallback_used=False,
        restricted_notes=["Zone restriction"],
        constrained_labels=["Dsv7"],
        quota_required=True,
    )
    assert "zonal_access" in _rec_types(recs)
    za = _by_type(recs, "zonal_access")
    assert za["ticket_kind"] == "technical"
    assert za["priority"] == "high"


def test_quota_blocker_recommends_quota_ticket():
    recs = compile_mod._build_recommendations(
        blockers=[{"type": "quota_insufficient", "severity": "warning", "message": "x"}],
        region={},
        region_display="West US 2",
        fallback_used=False,
        restricted_notes=[],
        constrained_labels=[],
        quota_required=True,
    )
    assert "quota_increase" in _rec_types(recs)
    assert _by_type(recs, "quota_increase")["ticket_kind"] == "quota"


def test_fallback_used_recommends_standardizing_on_fallback():
    recs = compile_mod._build_recommendations(
        blockers=[{"type": "zone_gap", "severity": "warning", "message": "x"}],
        region={},
        region_display="Central US",
        fallback_used=True,
        restricted_notes=[],
        constrained_labels=["Dsv7"],
        quota_required=True,
    )
    assert "fallback_sku" in _rec_types(recs)


def test_missing_service_recommends_alternate_region():
    recs = compile_mod._build_recommendations(
        blockers=[{"type": "missing_service", "severity": "critical", "message": "x"}],
        region={},
        region_display="North Europe",
        fallback_used=False,
        restricted_notes=[],
        constrained_labels=[],
        quota_required=False,
    )
    assert "alt_region" in _rec_types(recs)


def test_pure_no_access_does_not_recommend_odcr_but_grants_access():
    recs = compile_mod._build_recommendations(
        blockers=[{"type": "no_access", "severity": "critical", "message": "x"}],
        region={},
        region_display="East US",
        fallback_used=False,
        restricted_notes=[],
        constrained_labels=[],
        quota_required=True,
    )
    types = _rec_types(recs)
    assert "odcr" not in types
    assert "grant_access" in types


def test_ready_region_has_no_recommendations():
    recs = compile_mod._build_recommendations(
        blockers=[],
        region={},
        region_display="East US",
        fallback_used=False,
        restricted_notes=[],
        constrained_labels=[],
        quota_required=True,
    )
    assert recs == []

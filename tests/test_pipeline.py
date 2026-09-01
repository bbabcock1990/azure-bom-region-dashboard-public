"""Smoke test: pipeline runs end-to-end on the lifted Step 1 + Step 2 fixtures.

Also covers the tier-agnostic recommendation engine so peers who use a
non-v5/v6 primary-alt pair (e.g. v5 primary + v4 alt) get correct verdicts.
"""
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(HERE, "api", "_shared", "data")
FIXT = os.path.join(HERE, "fixtures")

sys.path.insert(0, os.path.join(HERE, "api"))

from _shared.pipeline import sources, model
from _shared.pipeline.model import recommendation, _extract_tier


def test_build_model_against_fixtures():
    sku_path = os.path.join(FIXT, "sample_step1.xlsx")
    bom_path = os.path.join(FIXT, "sample_step2.xlsx")
    if not (os.path.exists(sku_path) and os.path.exists(bom_path)):
        import pytest
        pytest.skip("fixtures not lifted; skip")

    sku = sources.read_sku_v2(sku_path)
    bom_header, bom_rows = sources.read_bom_v2(bom_path)
    latency = sources.read_latency_csv(os.path.join(DATA, "azure_region_latency.csv"))

    m = model.build_model({
        "sku_records": sku,
        "bom_header": bom_header,
        "bom_records": bom_rows,
        "latency": latency,
    })
    assert "regions" in m and isinstance(m["regions"], list)
    assert m["regions"], "expected at least one region"
    assert "stats" in m and m["stats"]["total_regions"] == len(m["regions"])
    # Each region row has the keys the dashboard relies on (legacy + new).
    sample = m["regions"][0]
    for key in ("name", "geo", "deployment_health", "status",
                "zone_health", "v6_viable", "primary_used", "fell_back",
                "alt_regions"):
        assert key in sample, f"missing {key}"
    # bom.skus now carries tier metadata so the UI can label things generically
    assert "bom" in m and "skus" in m["bom"]
    if m["bom"]["skus"]:
        s0 = m["bom"]["skus"][0]
        for key in ("primary", "alt", "primary_family", "alt_family",
                    "primary_tier", "alt_tier"):
            assert key in s0, f"missing bom.skus[0].{key}"


# ── Tier extraction ──────────────────────────────────────────────────────────

def test_extract_tier_handles_known_family_names():
    assert _extract_tier("standardDav6Family") == "v6"
    assert _extract_tier("standardDASv5Family") == "v5"
    assert _extract_tier("standardDav4Family") == "v4"
    assert _extract_tier("standardDSv3Family") == "v3"
    assert _extract_tier("standardLsv3Family") == "v3"


def test_extract_tier_returns_none_for_missing_or_unmatched():
    assert _extract_tier(None) is None
    assert _extract_tier("") is None
    assert _extract_tier("weirdFamily") is None


# ── recommendation() — primary chosen everywhere ────────────────────────────

V6_V5_PAIR = [
    {"primary_family": "standardDav6Family", "primary_label": "Dasv6",
     "alt_family": "standardDASv5Family", "alt_label": "Dasv5"},
    {"primary_family": "standardEav6Family", "primary_label": "Easv6",
     "alt_family": "standardEASv5Family", "alt_label": "Easv5"},
]


def test_recommendation_all_primary_v6_pair():
    msg, primary_viable, healthy = recommendation(V6_V5_PAIR, ["Dasv6", "Easv6"])
    assert primary_viable is True
    assert healthy is True
    assert "Dasv6" in msg and "Easv6" in msg
    # Should NOT claim alternatives are available — recommendation() can't
    # actually verify per-region alt availability, so the wording stays clean.
    assert "not available" not in msg
    assert "alternatives" not in msg


def test_recommendation_all_fallback_v6_pair():
    msg, primary_viable, healthy = recommendation(V6_V5_PAIR, ["Dasv5", "Easv5"])
    assert primary_viable is False
    assert healthy is True
    assert "Dasv5" in msg and "Easv5" in msg
    assert "Dasv6" in msg and "Easv6" in msg
    assert "not available in all 3 AZs" in msg


def test_recommendation_mixed_v6_pair():
    msg, primary_viable, healthy = recommendation(V6_V5_PAIR, ["Dasv6", "Easv5"])
    assert primary_viable is False
    assert healthy is True
    assert "Dasv6" in msg
    assert "Easv5 for Easv6" in msg


# ── recommendation() — v5 primary + v4 alt (the user's question) ────────────

V5_V4_PAIR = [
    {"primary_family": "standardDASv5Family", "primary_label": "Dasv5",
     "alt_family": "standardDav4Family", "alt_label": "Dav4"},
]


def test_recommendation_v5_primary_with_v4_fallback_all_primary():
    """When the BOM has v5 primary + v4 alt, an all-primary region should
    emit a clean 'Use Dasv5 in all AZs' message — not hardcode v6/v5 text."""
    msg, primary_viable, healthy = recommendation(V5_V4_PAIR, ["Dasv5"])
    assert primary_viable is True
    assert msg == "Use Dasv5 in all AZs"


def test_recommendation_v5_primary_with_v4_fallback_falls_back():
    """When Dasv5 isn't all-3-AZ but Dav4 is, recommend Dav4 by name."""
    msg, primary_viable, healthy = recommendation(V5_V4_PAIR, ["Dav4"])
    assert primary_viable is False
    assert "Dav4" in msg


# ── alternatives() — surfaces alts for newer regions ─────────────────────────

def test_alternatives_uses_published_latency_when_available():
    """East US is in DISPLAY_TO_LATENCY and the latency dict, so we use real
    Microsoft-published ms numbers (sorted ascending)."""
    latency = {"East US": {"East US 2": 12, "Central US": 28, "West US 3": 53}}
    out = model.alternatives("East US", latency,
                             ["East US 2", "Central US", "West US 3"], top_n=2)
    assert out[0]["region"] == "East US 2"
    assert out[0]["latency_ms"] == 12
    assert out[0]["source"] == "ms_published"
    assert out[1]["region"] == "Central US"
    assert out[1]["latency_ms"] == 28


def test_alternatives_geo_fallback_for_curated_newer_region():
    """Austria East is in GEO_FALLBACK; when at least one curated peer is
    healthy we use the curated list (no latency_ms)."""
    out = model.alternatives("Austria East", latency={},
                             healthy=["Germany West Central", "Italy North", "Sweden Central"],
                             top_n=3)
    assert len(out) == 3
    sources_used = {a["source"] for a in out}
    assert sources_used == {"geo_fallback"}
    assert all(a["latency_ms"] is None for a in out)
    regions = [a["region"] for a in out]
    assert regions[0] == "Germany West Central"


def test_alternatives_geo_distance_fallback_for_uncurated_region():
    """Indonesia Central isn't in DISPLAY_TO_LATENCY or GEO_FALLBACK, but it
    IS in geo.REGION_GEO. Distance-based fallback should kick in and prefer
    same-continent (Asia Pacific) peers ordered by haversine distance."""
    healthy = ["Singapore — not a region",
               "Southeast Asia", "Japan East", "Australia East",
               "West Europe", "Germany West Central"]
    # Filter to only known-geo regions inside the test fixture
    healthy = [h for h in healthy if h in (
        "Southeast Asia", "Japan East", "Australia East",
        "West Europe", "Germany West Central")]
    out = model.alternatives("Indonesia Central", latency={},
                             healthy=healthy, top_n=3)
    assert len(out) == 3
    sources_used = {a["source"] for a in out}
    assert sources_used == {"geo_distance"}
    # All 3 should have distance_km; latency_ms is null (UI renders "geo proximity")
    for a in out:
        assert a["latency_ms"] is None
        assert isinstance(a["distance_km"], int) and a["distance_km"] > 0
    # Closest same-continent (Asia Pacific) regions should come first.
    # Southeast Asia (Singapore) is ~880 km from Jakarta — clearly the
    # nearest of the healthy set.
    assert out[0]["region"] == "Southeast Asia"
    # Europe should appear only after exhausting Asia Pacific picks.
    asia_pacific_regions = {"Southeast Asia", "Japan East", "Australia East"}
    first_two_regions = {out[0]["region"], out[1]["region"]}
    assert first_two_regions <= asia_pacific_regions


def test_alternatives_returns_empty_when_no_healthy_regions():
    assert model.alternatives("East US", latency={}, healthy=[], top_n=3) == []


# ── _least_bad_alternatives() — fallback when NO region is healthy ───────────

def _mk_analysis(name, gap_score, gap_summary):
    return {"name": name, "gap_score": gap_score, "gap_summary": gap_summary}


def test_least_bad_ranks_by_fewest_gaps():
    """When no region is healthy, suggest only regions with strictly fewer
    gaps than the source, ordered by gap score (then latency)."""
    analyses = [
        _mk_analysis("East US", 300, "3 missing services"),
        _mk_analysis("East US 2", 100, "1 missing service"),
        _mk_analysis("Central US", 200, "2 SKU gaps"),
        _mk_analysis("West US 3", 400, "4 missing services"),
    ]
    latency = {"East US": {"East US 2": 12, "Central US": 28}}
    out = model._least_bad_alternatives("East US", analyses, latency, top_n=3)
    # West US 3 (worse than source) is excluded; results sorted by gap_score.
    assert [a["region"] for a in out] == ["East US 2", "Central US"]
    assert out[0]["source"] == "least_bad"
    assert out[0]["caveat"] == "1 missing service"
    assert out[0]["latency_ms"] == 12


def test_least_bad_excludes_regions_no_better_than_source():
    analyses = [
        _mk_analysis("East US", 100, "1 missing service"),
        _mk_analysis("Central US", 100, "1 SKU gap"),   # equal — not suggested
        _mk_analysis("West US 3", 150, "1 SKU gap, 5 zones restricted"),
    ]
    out = model._least_bad_alternatives("East US", analyses, latency={}, top_n=3)
    assert out == []


def test_least_bad_uses_distance_when_no_latency():
    analyses = [
        _mk_analysis("Indonesia Central", 300, "3 missing services"),
        _mk_analysis("Southeast Asia", 100, "1 missing service"),
        _mk_analysis("Japan East", 100, "1 missing service"),
    ]
    out = model._least_bad_alternatives("Indonesia Central", analyses, latency={}, top_n=3)
    assert {a["region"] for a in out} == {"Southeast Asia", "Japan East"}
    for a in out:
        assert a["latency_ms"] is None
        assert isinstance(a["distance_km"], int) and a["distance_km"] > 0
    # Nearest (Southeast Asia / Singapore) should rank first within equal score.
    assert out[0]["region"] == "Southeast Asia"


def test_alternatives_falls_through_when_curated_geo_fallback_has_no_healthy():
    """If GEO_FALLBACK has entries but none of them are healthy right now,
    the distance fallback must run (don't return empty)."""
    # Austria East's curated list: Germany West Central, Italy North,
    # Sweden Central, Switzerland North — none of these are in the
    # healthy set below, but other geo-known peers are.
    out = model.alternatives("Austria East", latency={},
                             healthy=["West Europe", "UK South"],
                             top_n=3)
    assert len(out) >= 1
    assert {a["source"] for a in out} == {"geo_distance"}
    assert out[0]["region"] in ("West Europe", "UK South")


# ── recommendation() — single family, no alt ────────────────────────────────

NO_ALT = [{"primary_family": "standardDSv3Family", "primary_label": "DSv3",
           "alt_family": None, "alt_label": None}]


def test_recommendation_no_alt_family():
    msg, primary_viable, healthy = recommendation(NO_ALT, ["DSv3"])
    assert primary_viable is True
    assert msg == "Use DSv3 in all AZs"


# ── recommendation() — defensive paths ──────────────────────────────────────

def test_recommendation_unhealthy_region_returns_empty():
    msg, primary_viable, healthy = recommendation(V6_V5_PAIR, [None, "Easv6"])
    assert msg == ""
    assert primary_viable is False
    assert healthy is False


def test_recommendation_length_mismatch_returns_empty():
    msg, primary_viable, healthy = recommendation(V6_V5_PAIR, ["Dasv6"])
    assert msg == ""
    assert primary_viable is False
    assert healthy is False


def test_recommendation_chosen_label_not_in_family_returns_empty():
    """Defensive: if analyze_skus somehow returns a label that matches neither
    primary nor alt, bail safely rather than emit a garbled message."""
    msg, primary_viable, healthy = recommendation(V6_V5_PAIR, ["Garbage", "Easv6"])
    assert msg == ""
    assert primary_viable is False

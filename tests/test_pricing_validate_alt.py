"""Unit tests for the cheaper-SKU live validation endpoint helpers.

The endpoint module uses package-relative imports (``from .._shared import ...``),
so it must be imported as ``api.pricing_validate_alt`` with the repo root on the
path. These tests cover the pure verdict/mapping logic without any HTTP.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from api.pricing_validate_alt import _arm_family_id, _num, _verdict_for


# --------------------------------------------------------------- _arm_family_id

def test_arm_family_id_wraps_core_form():
    assert _arm_family_id("Dpsv6") == "standardDpsv6Family"
    assert _arm_family_id("Dasv5") == "standardDasv5Family"


def test_arm_family_id_trims_whitespace():
    assert _arm_family_id("  Esv7 ") == "standardEsv7Family"


# ------------------------------------------------------------------------- _num

def test_num_parses_and_guards():
    assert _num("100") == 100.0
    assert _num(5) == 5.0
    assert _num(None) is None
    assert _num("not-a-number") is None


# ------------------------------------------------------------------ _verdict_for

def test_verdict_ok_when_available_and_quota_sufficient():
    v = _verdict_for(
        offered=True, region_restricted=False, zones=[True, True, True],
        quota={"headroom": 5000, "limit": 6000}, required_cores=1000,
    )
    assert v["verdict"] == "ok"
    assert v["quota_enough"] is True
    assert v["zone_limited"] is False


def test_verdict_quota_short_reports_shortfall():
    v = _verdict_for(
        offered=True, region_restricted=False, zones=[True, True, True],
        quota={"headroom": 100}, required_cores=1000,
    )
    assert v["verdict"] == "quota"
    assert v["quota_enough"] is False
    assert v["shortfall"] == 900.0


def test_verdict_region_restricted():
    v = _verdict_for(
        offered=True, region_restricted=True, zones=[False, False, False],
        quota={"headroom": 5000}, required_cores=10,
    )
    assert v["verdict"] == "restricted"


def test_verdict_all_zones_blocked_is_restricted():
    v = _verdict_for(
        offered=True, region_restricted=False, zones=[False, False, False],
        quota={"headroom": 5000}, required_cores=10,
    )
    assert v["verdict"] == "restricted"


def test_verdict_unavailable_when_not_offered():
    v = _verdict_for(
        offered=False, region_restricted=False, zones=[False, False, False],
        quota={}, required_cores=10,
    )
    assert v["verdict"] == "unavailable"


def test_verdict_zone_limited_but_ok():
    v = _verdict_for(
        offered=True, region_restricted=False, zones=[True, False, True],
        quota={"headroom": 5000}, required_cores=10,
    )
    assert v["verdict"] == "ok"
    assert v["zone_limited"] is True
    assert "AZ 2" in v["message"]


def test_verdict_unknown_when_quota_unreadable():
    v = _verdict_for(
        offered=True, region_restricted=False, zones=[True, True, True],
        quota={}, required_cores=10,
    )
    assert v["verdict"] == "unknown"
    assert v["quota_enough"] is None

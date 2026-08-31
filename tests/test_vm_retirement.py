"""Unit tests for VM size lifecycle (retirement / previous-gen) filtering.

Covers the retirement data lookup, the pricing recommendation filter
(:func:`pricing.eligible_alternatives`) for temp-disk parity, generation floor
and retirement exclusion, and the ARM capability parity comparator.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_API = os.path.join(_ROOT, "api")
for p in (_ROOT, _API):
    if p not in sys.path:
        sys.path.insert(0, p)

from _shared import pricing, vm_retirement, sku_capabilities


# ----------------------------------------------------------- vm_retirement

def test_fsv2_is_announced_and_blocks():
    rec = vm_retirement.status_for_core("Fsv2")
    assert rec is not None
    assert rec["status"] == "announced"
    assert rec["blocks_recommendation"] is True


def test_family_id_and_core_forms_resolve_same():
    assert vm_retirement.blocks_recommendation("standardFsv2Family") is True
    assert vm_retirement.blocks_recommendation("Fsv2") is True


def test_previous_gen_next_gen_available_does_not_block():
    rec = vm_retirement.status_for_core("Dsv3")
    assert rec is not None
    assert rec["status"] == "previous_gen"
    assert rec["blocks_recommendation"] is False
    assert "Previous-gen" in vm_retirement.short_note("Dsv3")


def test_current_family_has_no_record():
    assert vm_retirement.status_for_core("Dsv6") is None
    assert vm_retirement.blocks_recommendation("Dsv6") is False


# --------------------------------------------------- eligible_alternatives

def test_retiring_family_excluded_from_recommendations():
    # Fsv2 is announced for retirement -> never suggested for the F group.
    alts = [c["family"] for c in pricing.eligible_alternatives("standardFasv6Family")]
    assert "Fsv2" not in alts


def test_temp_disk_parity_preserved():
    # Ddsv5 has a local temp disk ('d'); every suggestion must keep it.
    alts = [c["family"] for c in pricing.eligible_alternatives("standardDdsv5Family")]
    assert alts, "expected some temp-disk-preserving alternatives"
    assert all("d" in pricing._size_features(a) for a in alts)


def test_generation_floor_default_same_or_newer():
    alts = pricing.eligible_alternatives("standardDsv7Family")
    assert all(pricing._generation(c["family"]) >= 7 for c in alts)


def test_generation_floor_opt_in_allows_older():
    default = pricing.eligible_alternatives("standardDsv7Family")
    older = pricing.eligible_alternatives("standardDsv7Family", allow_older_generation=True)
    assert len(older) > len(default)
    assert any(pricing._generation(c["family"]) < 7 for c in older)


def test_exclude_cores_drops_bom_members():
    alts = [c["family"].lower()
            for c in pricing.eligible_alternatives(
                "standardDsv7Family", exclude_cores={"dasv7"}, allow_older_generation=True)]
    assert "dasv7" not in alts


# ------------------------------------------------------- helper accessors

def test_generation_parses_version():
    assert pricing._generation("Dsv7") == 7
    assert pricing._generation("Fsv2") == 2
    assert pricing._generation("Fasv6") == 6


def test_size_features_extracts_letters():
    assert pricing._size_features("Ddsv5") == "ds"
    assert pricing._size_features("Dpsv6") == "ps"
    assert pricing._size_features("Dpdsv6") == "pds"


def test_naming_parity_ok_temp_disk():
    assert pricing._naming_parity_ok("Ddsv5", "Ddsv6") is True
    assert pricing._naming_parity_ok("Ddsv5", "Dpsv6") is False   # loses temp disk
    assert pricing._naming_parity_ok("Dsv5", "Dpsv6") is True     # neither has temp disk


# ------------------------------------------------------ sku_capabilities

def test_parse_size_name():
    assert sku_capabilities.parse_size_name("Standard_D4ps_v6") == ("dpsv6", 4)
    assert sku_capabilities.parse_size_name("Standard_D4s_v5") == ("dsv5", 4)
    assert sku_capabilities.parse_size_name("Standard_E4-2s_v5") == ("esv5", 4)
    assert sku_capabilities.parse_size_name("nonsense") is None


def test_compare_caps_missing_temp_disk():
    orig = {"MaxResourceVolumeMB": "51200", "MemoryGB": "16"}
    alt = {"MaxResourceVolumeMB": "0", "MemoryGB": "16"}
    missing = sku_capabilities.compare_caps(orig, alt)
    assert any(m["cap"] == "Temp (local) disk" for m in missing)


def test_compare_caps_missing_accel_net_and_gen():
    orig = {"AcceleratedNetworkingEnabled": "True", "HyperVGenerations": "V1,V2"}
    alt = {"AcceleratedNetworkingEnabled": "False", "HyperVGenerations": "V2"}
    caps = {m["cap"] for m in sku_capabilities.compare_caps(orig, alt)}
    assert "Accelerated networking" in caps
    assert any("Hyper-V" in c and "V1" in c for c in caps)


def test_compare_caps_full_parity_is_empty():
    same = {"MaxResourceVolumeMB": "51200", "PremiumIO": "True",
            "AcceleratedNetworkingEnabled": "True", "MemoryGB": "16",
            "HyperVGenerations": "V1,V2"}
    assert sku_capabilities.compare_caps(same, dict(same)) == []


def test_parity_check_picks_common_size_and_flags():
    index = {
        "dsv7": {2: {"MaxResourceVolumeMB": "0", "AcceleratedNetworkingEnabled": "True"}},
        "dpsv6": {2: {"MaxResourceVolumeMB": "0", "AcceleratedNetworkingEnabled": "False"}},
    }
    res = sku_capabilities.parity_check(index, "Dsv7", "Dpsv6")
    assert res["status"] == "incompatible"
    assert res.get("vcpus") == 2


def test_parity_check_unknown_when_family_absent():
    index = {"dsv7": {2: {"MemoryGB": "8"}}}
    res = sku_capabilities.parity_check(index, "Dsv7", "Dpsv6")
    assert res["status"] == "unknown"

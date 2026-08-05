"""Tests for the per-BOM Required SKUs feature added in swa-1.2.0.

Covers:
  - parsing the multiline `families_override` form field
  - reading the optional 'Required SKUs' sheet from a BOM xlsx
  - validating a resolved required-families list
"""
import io
import os
import sys

import openpyxl
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "api"))

from _shared import compile as compile_mod  # noqa: E402

parse_families_override = compile_mod.parse_families_override


# ---------- families_override parsing ---------------------------------------

def test_parse_override_returns_none_when_blank():
    assert parse_families_override("") is None
    assert parse_families_override("   \n\n  ") is None


def test_parse_override_minimal_one_line():
    out = parse_families_override("standardDav6Family")
    assert out == [{
        "primary_family": "standardDav6Family",
        "primary_label": None,
        "alt_family": None,
        "alt_label": None,
        "required_cores": None,
    }]


def test_parse_override_full_columns_and_skip_comments():
    text = "\n".join([
        "# this is a comment",
        "",
        "standardDav6Family,Dav6,standardDASv5Family,Dasv5",
        "standardEav6Family,Eav6,standardEASv5Family,Easv5",
        "  standardDSv3Family , DSv3 ,  ,  ",
    ])
    out = parse_families_override(text)
    assert len(out) == 3
    assert out[0]["primary_family"] == "standardDav6Family"
    assert out[0]["primary_label"] == "Dav6"
    assert out[0]["alt_family"] == "standardDASv5Family"
    assert out[0]["alt_label"] == "Dasv5"
    assert out[2]["primary_family"] == "standardDSv3Family"
    assert out[2]["alt_family"] is None
    assert out[2]["alt_label"] is None


def test_parse_override_tab_separated_also_works():
    out = parse_families_override("standardDav6Family\tDav6\tstandardDASv5Family\tDasv5")
    assert out[0]["primary_family"] == "standardDav6Family"
    assert out[0]["alt_label"] == "Dasv5"


def test_parse_override_rejects_duplicate_primary():
    with pytest.raises(compile_mod.CompileError) as ei:
        parse_families_override("standardDav6Family\nstandardDav6Family,Dav6")
    assert ei.value.code == "bad_families_override"
    assert "duplicate" in ei.value.message.lower()


def test_parse_override_rejects_too_many_rows():
    text = "\n".join(f"family{i}" for i in range(60))
    with pytest.raises(compile_mod.CompileError) as ei:
        parse_families_override(text)
    assert ei.value.code == "bad_families_override"
    assert "too many" in ei.value.message.lower()


def test_parse_override_rejects_too_long():
    with pytest.raises(compile_mod.CompileError) as ei:
        parse_families_override("x" * 9000)
    assert ei.value.code == "bad_families_override"


def test_parse_override_rejects_blank_primary():
    with pytest.raises(compile_mod.CompileError):
        parse_families_override(",Dav6,standardDASv5Family,Dasv5")


# ---------- families_override: Required Cores (5th column) ------------------

def test_parse_override_with_cores():
    out = parse_families_override(
        "standardDav6Family,Dav6,standardDASv5Family,Dasv5,100\n"
        "standardEav6Family,Eav6,standardEASv5Family,Easv5,50"
    )
    assert len(out) == 2
    assert out[0]["required_cores"] == 100
    assert out[1]["required_cores"] == 50


def test_parse_override_4_columns_still_works_no_cores():
    """Backward compat: old 4-column inputs still parse with cores=None."""
    out = parse_families_override("standardDav6Family,Dav6,standardDASv5Family,Dasv5")
    assert out[0]["required_cores"] is None


def test_parse_override_trailing_comma_yields_none_cores():
    """Users often have trailing commas; should treat empty 5th cell as no req."""
    out = parse_families_override("standardDav6Family,Dav6,standardDASv5Family,Dasv5,")
    assert out[0]["required_cores"] is None


def test_parse_override_cores_only_primary_and_cores():
    """Minimal: just primary family + cores (skip labels with extra commas)."""
    out = parse_families_override("standardDav6Family,,,,200")
    assert out[0]["primary_family"] == "standardDav6Family"
    assert out[0]["required_cores"] == 200


def test_parse_override_cores_tab_separated():
    out = parse_families_override(
        "standardDav6Family\tDav6\tstandardDASv5Family\tDasv5\t300"
    )
    assert out[0]["required_cores"] == 300


def test_parse_override_rejects_negative_cores():
    with pytest.raises(compile_mod.CompileError) as ei:
        parse_families_override("standardDav6Family,Dav6,,,-5")
    assert ei.value.code == "bad_required_cores"


def test_parse_override_rejects_zero_cores():
    with pytest.raises(compile_mod.CompileError) as ei:
        parse_families_override("standardDav6Family,Dav6,,,0")
    assert ei.value.code == "bad_required_cores"
    assert "positive" in ei.value.message.lower()


def test_parse_override_rejects_non_numeric_cores():
    with pytest.raises(compile_mod.CompileError) as ei:
        parse_families_override("standardDav6Family,Dav6,,,100cores")
    assert ei.value.code == "bad_required_cores"


def test_parse_override_rejects_fractional_cores():
    """vCPU cores are whole numbers."""
    with pytest.raises(compile_mod.CompileError) as ei:
        parse_families_override("standardDav6Family,Dav6,,,100.5")
    assert ei.value.code == "bad_required_cores"
    assert "whole" in ei.value.message.lower()


def test_parse_override_accepts_float_with_zero_fraction():
    """'100.0' from a numeric-string source should be accepted as 100."""
    out = parse_families_override("standardDav6Family,Dav6,,,100.0")
    assert out[0]["required_cores"] == 100
    assert isinstance(out[0]["required_cores"], int)


# ---------- _parse_required_cores helper (direct unit tests) ----------------

def test_parse_cores_blank_variants_return_none():
    assert compile_mod._parse_required_cores(None, where="t") is None
    assert compile_mod._parse_required_cores("", where="t") is None
    assert compile_mod._parse_required_cores("   ", where="t") is None


def test_parse_cores_accepts_int_and_float():
    assert compile_mod._parse_required_cores(100, where="t") == 100
    assert compile_mod._parse_required_cores(100.0, where="t") == 100


def test_parse_cores_rejects_bool():
    """isinstance(True, int) is True in Python — booleans must be rejected."""
    with pytest.raises(compile_mod.CompileError) as ei:
        compile_mod._parse_required_cores(True, where="t")
    assert ei.value.code == "bad_required_cores"
    with pytest.raises(compile_mod.CompileError):
        compile_mod._parse_required_cores(False, where="t")


def test_parse_cores_rejects_nan_and_inf():
    with pytest.raises(compile_mod.CompileError):
        compile_mod._parse_required_cores(float("nan"), where="t")
    with pytest.raises(compile_mod.CompileError):
        compile_mod._parse_required_cores(float("inf"), where="t")


def test_parse_cores_rejects_unsupported_type():
    with pytest.raises(compile_mod.CompileError):
        compile_mod._parse_required_cores([100], where="t")


# ---------- Required SKUs sheet: Required Cores column ----------------------

def test_read_bom_required_sheet_with_cores_column():
    blob = _build_bom_xlsx(
        with_required_sheet=True,
        sheet_header=[
            "Primary Family", "Primary Label",
            "Alt Family", "Alt Label", "Required Cores",
        ],
        required_rows=[
            ["standardDav6Family", "Dav6", "standardDASv5Family", "Dasv5", 100],
            ["standardEav6Family", "Eav6", "standardEASv5Family", "Easv5", 50],
            ["standardDSv3Family", "DSv3", None, None, None],
        ],
    )
    _, _, required = compile_mod._read_bom_xlsx_bytes(blob)
    assert required is not None
    assert required[0]["required_cores"] == 100
    assert required[1]["required_cores"] == 50
    assert required[2]["required_cores"] is None


def test_read_bom_required_sheet_without_cores_column_backcompat():
    """Old BOMs without a Required Cores column still load (cores=None)."""
    blob = _build_bom_xlsx(with_required_sheet=True, required_rows=[
        ["standardDav6Family", "Dav6", "standardDASv5Family", "Dasv5"],
    ])
    _, _, required = compile_mod._read_bom_xlsx_bytes(blob)
    assert required[0]["required_cores"] is None


def test_read_bom_required_sheet_cores_as_string():
    """openpyxl returns text when cells are formatted as text; accept '100'."""
    blob = _build_bom_xlsx(
        with_required_sheet=True,
        sheet_header=[
            "Primary Family", "Primary Label",
            "Alt Family", "Alt Label", "Required Cores",
        ],
        required_rows=[
            ["standardDav6Family", "Dav6", None, None, "100"],
        ],
    )
    _, _, required = compile_mod._read_bom_xlsx_bytes(blob)
    assert required[0]["required_cores"] == 100


def test_read_bom_required_sheet_invalid_cores_raises():
    blob = _build_bom_xlsx(
        with_required_sheet=True,
        sheet_header=[
            "Primary Family", "Primary Label",
            "Alt Family", "Alt Label", "Required Cores",
        ],
        required_rows=[
            ["standardDav6Family", "Dav6", None, None, "abc"],
        ],
    )
    with pytest.raises(compile_mod.CompileError) as ei:
        compile_mod._read_bom_xlsx_bytes(blob)
    assert ei.value.code == "bad_required_cores"


def test_read_bom_required_sheet_negative_cores_raises():
    blob = _build_bom_xlsx(
        with_required_sheet=True,
        sheet_header=[
            "Primary Family", "Primary Label",
            "Alt Family", "Alt Label", "Required Cores",
        ],
        required_rows=[
            ["standardDav6Family", "Dav6", None, None, -10],
        ],
    )
    with pytest.raises(compile_mod.CompileError) as ei:
        compile_mod._read_bom_xlsx_bytes(blob)
    assert ei.value.code == "bad_required_cores"


def test_read_bom_required_sheet_cores_header_case_insensitive():
    blob = _build_bom_xlsx(
        with_required_sheet=True,
        sheet_header=[
            "primary family", "primary label",
            "alt family", "alt label", "REQUIRED CORES",
        ],
        required_rows=[
            ["standardDav6Family", "Dav6", None, None, 75],
        ],
    )
    _, _, required = compile_mod._read_bom_xlsx_bytes(blob)
    assert required[0]["required_cores"] == 75


# ---------- _validate_required_families: cores normalization ----------------

def test_validate_normalizes_missing_cores_to_none():
    fams = [{"primary_family": "standardDav6Family"}]
    compile_mod._validate_required_families(fams)
    assert fams[0]["required_cores"] is None


def test_validate_accepts_positive_int_cores():
    fams = [{"primary_family": "standardDav6Family", "required_cores": 50}]
    compile_mod._validate_required_families(fams)
    assert fams[0]["required_cores"] == 50


def test_validate_rejects_bool_cores():
    """bool must be rejected since isinstance(True, int) == True in Python."""
    fams = [{"primary_family": "standardDav6Family", "required_cores": True}]
    with pytest.raises(compile_mod.CompileError) as ei:
        compile_mod._validate_required_families(fams)
    assert ei.value.code == "bad_required_cores"


def test_validate_rejects_zero_and_negative_cores():
    for bad in (0, -1, -100):
        fams = [{"primary_family": "standardDav6Family", "required_cores": bad}]
        with pytest.raises(compile_mod.CompileError) as ei:
            compile_mod._validate_required_families(fams)
        assert ei.value.code == "bad_required_cores"


# ---------- Required SKUs sheet reader --------------------------------------

def _build_bom_xlsx(*, with_required_sheet=False, required_rows=None,
                    sheet_header=None) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Region Results"
    ws.append(["Azure Region BOM Check  |  Generated: test", None, None])
    ws.append(["All services available", "One or more services missing", None])
    ws.append(["Region", "Display Name", "Overall Status", "Test Service"])
    ws.append(["eastus", "East US", "✅ SUPPORTED", "✅ Available"])
    ws.append(["westus3", "West US 3", "✅ SUPPORTED", "✅ Available"])

    if with_required_sheet:
        ws2 = wb.create_sheet("Required SKUs")
        if sheet_header:
            ws2.append(sheet_header)
        else:
            ws2.append(["Required SKU families for this BOM", None, None, None])
            ws2.append(["Primary Family", "Primary Label", "Alt Family", "Alt Label"])
        for row in (required_rows or []):
            ws2.append(row)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_read_bom_no_required_sheet_returns_none():
    blob = _build_bom_xlsx()
    header, records, required = compile_mod._read_bom_xlsx_bytes(blob)
    assert header[:3] == ["Region", "Display Name", "Overall Status"]
    assert len(records) == 2
    assert required is None


def test_read_bom_with_required_sheet():
    blob = _build_bom_xlsx(with_required_sheet=True, required_rows=[
        ["standardDav6Family", "Dav6", "standardDASv5Family", "Dasv5"],
        ["standardEav6Family", "Eav6", "standardEASv5Family", "Easv5"],
        ["standardDSv3Family", "DSv3", None, None],
    ])
    _, _, required = compile_mod._read_bom_xlsx_bytes(blob)
    assert required is not None
    assert len(required) == 3
    assert required[0]["primary_family"] == "standardDav6Family"
    assert required[0]["alt_family"] == "standardDASv5Family"
    assert required[2]["alt_family"] is None


def test_read_bom_required_sheet_empty_treated_as_absent():
    blob = _build_bom_xlsx(with_required_sheet=True, required_rows=[])
    _, _, required = compile_mod._read_bom_xlsx_bytes(blob)
    assert required is None


def test_read_bom_required_sheet_stops_at_blank_row():
    """Convention: a blank row (or a row with empty primary family) ends the
    data block so authors can put guidance/notes below."""
    blob = _build_bom_xlsx(with_required_sheet=True, required_rows=[
        ["standardDav6Family", "Dav6", None, None],
        ["standardEav6Family", "Eav6", None, None],
        [None, None, None, None],          # blank gap
        ["Notes:", None, None, None],       # documentation, not data
        ["•", "These are not SKU families", None, None],
    ])
    _, _, required = compile_mod._read_bom_xlsx_bytes(blob)
    assert [e["primary_family"] for e in required] == [
        "standardDav6Family", "standardEav6Family",
    ]


def test_read_bom_required_sheet_stops_at_empty_primary():
    """Even without a fully blank row, an empty primary-family cell ends the
    block. Catches the case where someone left B/C/D filled in a stub row."""
    blob = _build_bom_xlsx(with_required_sheet=True, required_rows=[
        ["standardDav6Family", "Dav6", None, None],
        ["", "ignored", "x", "y"],
        ["standardDSv3Family", "DSv3", None, None],
    ])
    _, _, required = compile_mod._read_bom_xlsx_bytes(blob)
    assert [e["primary_family"] for e in required] == ["standardDav6Family"]


def test_read_bom_required_sheet_stops_at_notes_without_blank_separator():
    """Regression: users who delete the blank separator row when editing the
    template would previously trip a 'Duplicate primary_family' error because
    'Notes:' and a series of '•' bullet rows got parsed as SKU entries.
    The reader now recognises rows whose primary-family cell isn't a valid
    Azure family identifier and stops there."""
    blob = _build_bom_xlsx(with_required_sheet=True, required_rows=[
        ["standardDav6Family", "Dav6", "standardDASv5Family", "Dasv5"],
        ["standardEav6Family", "Eav6", "standardEASv5Family", "Easv5"],
        ["standardDSv3Family", "DSv3", None, None],
        # NO blank row — this is what the user's broken file looks like.
        ["Notes:", None, None, None],
        ["•", "One row per VM family your build needs.", None, None],
        ["•", "Primary Label / Alt columns are optional.", None, None],
        ["•", "Alt Family is the v5 fallback.", None, None],
    ])
    _, _, required = compile_mod._read_bom_xlsx_bytes(blob)
    assert [e["primary_family"] for e in required] == [
        "standardDav6Family", "standardEav6Family", "standardDSv3Family",
    ]


def test_read_bom_required_sheet_stops_at_dash_bullet():
    """Authors using '-' or '*' instead of '•' for bullets also stop the block."""
    blob = _build_bom_xlsx(with_required_sheet=True, required_rows=[
        ["standardDav6Family", "Dav6", None, None],
        ["- item one", "ignored", None, None],
    ])
    _, _, required = compile_mod._read_bom_xlsx_bytes(blob)
    assert [e["primary_family"] for e in required] == ["standardDav6Family"]


def test_read_bom_required_sheet_allows_letters_digits_dashes():
    """Hypothetical/future family IDs with `-` or `_` are still accepted."""
    blob = _build_bom_xlsx(with_required_sheet=True, required_rows=[
        ["standardF8sv2-1Family", "F8sv2-1", None, None],
        ["standard_custom_Family", "Custom", None, None],
    ])
    _, _, required = compile_mod._read_bom_xlsx_bytes(blob)
    assert [e["primary_family"] for e in required] == [
        "standardF8sv2-1Family", "standard_custom_Family",
    ]


def test_read_bom_required_sheet_missing_header_raises():
    blob = _build_bom_xlsx(
        with_required_sheet=True,
        sheet_header=["Banner only", None, None, None],
        required_rows=[["standardDav6Family", "Dav6", None, None]],
    )
    with pytest.raises(compile_mod.CompileError) as ei:
        compile_mod._read_bom_xlsx_bytes(blob)
    assert ei.value.code == "bad_required_skus_sheet"


def test_read_bom_required_sheet_handles_lowercase_headers():
    """Excel users often shift-case headers; the reader matches case-insensitively."""
    blob = _build_bom_xlsx(
        with_required_sheet=True,
        sheet_header=["primary family", "primary label", "alt family", "alt label"],
        required_rows=[["standardDav6Family", "Dav6", "standardDASv5Family", "Dasv5"]],
    )
    _, _, required = compile_mod._read_bom_xlsx_bytes(blob)
    assert len(required) == 1
    assert required[0]["alt_label"] == "Dasv5"


# ---------- _validate_required_families -------------------------------------

def test_validate_fills_missing_labels():
    fams = [{"primary_family": "standardDav6Family", "alt_family": "standardDASv5Family"}]
    compile_mod._validate_required_families(fams)
    assert fams[0]["primary_label"] == "Dav6"
    assert fams[0]["alt_label"] == "DASv5"


def test_validate_rejects_empty_list():
    with pytest.raises(compile_mod.CompileError) as ei:
        compile_mod._validate_required_families([])
    assert ei.value.code == "bad_required_families"


def test_validate_rejects_missing_primary_family():
    with pytest.raises(compile_mod.CompileError):
        compile_mod._validate_required_families([{"primary_label": "x"}])


def test_validate_rejects_duplicate_primary():
    with pytest.raises(compile_mod.CompileError):
        compile_mod._validate_required_families([
            {"primary_family": "standardDav6Family"},
            {"primary_family": "STANDARDdav6FAMILY"},
        ])


def test_validate_rejects_garbage_primary_with_clear_error():
    """If someone pastes 'Notes:' or '•' into the override textarea, give them
    a useful error rather than the confusing 'Duplicate primary_family'."""
    with pytest.raises(compile_mod.CompileError) as ei:
        compile_mod._validate_required_families([
            {"primary_family": "Notes:"},
        ])
    assert ei.value.code == "bad_required_families"
    assert "invalid primary_family" in ei.value.message
    assert "standardDav6Family" in ei.value.message  # example in error


def test_validate_rejects_garbage_alt_with_clear_error():
    with pytest.raises(compile_mod.CompileError) as ei:
        compile_mod._validate_required_families([
            {"primary_family": "standardDav6Family", "alt_family": "* see notes"},
        ])
    assert ei.value.code == "bad_required_families"
    assert "invalid alt_family" in ei.value.message


def test_validate_rejects_identical_primary_and_alt_labels():
    """When alt_family is set, the two labels MUST differ — recommendation()
    uses label-equality to detect which one was chosen, so identical labels
    would silently misclassify fallbacks as primary."""
    with pytest.raises(compile_mod.CompileError) as ei:
        compile_mod._validate_required_families([
            {
                "primary_family": "standardDav6Family",
                "primary_label": "Dav",
                "alt_family": "standardDASv5Family",
                "alt_label": "Dav",
            },
        ])
    assert ei.value.code == "bad_required_families"
    assert "identical primary_label and alt_label" in ei.value.message


def test_validate_allows_identical_labels_when_no_alt():
    """If there is no alt_family, there's no fallback to misclassify, so the
    identical-label guard should not fire (and never auto-fills an alt_label)."""
    fams = [{"primary_family": "standardDSv3Family", "primary_label": "X",
             "alt_family": None, "alt_label": "X"}]
    compile_mod._validate_required_families(fams)
    assert fams[0]["primary_label"] == "X"


# ---------- _flatten_family_ids ---------------------------------------------

def test_flatten_dedups_case_insensitive_and_preserves_order():
    out = compile_mod._flatten_family_ids([
        {"primary_family": "standardDav6Family", "alt_family": "standardDASv5Family"},
        {"primary_family": "standardEav6Family", "alt_family": "standardDASv5Family"},  # dup alt
        {"primary_family": "STANDARDDAV6FAMILY"},  # dup case
        {"primary_family": "standardDSv3Family"},
    ])
    assert out == [
        "standardDav6Family", "standardDASv5Family",
        "standardEav6Family", "standardDSv3Family",
    ]


# ---------- compile_snapshot end-to-end persistence -------------------------

def test_compile_snapshot_persists_bom_and_sku_inputs(monkeypatch):
    """Regression: bom_header, bom_records, and sku_records must end up in
    the persisted snapshot dict, not just be passed to build_model. Without
    this the BOM & SKUs tab would always render an 'unavailable in this
    snapshot' empty state."""
    blob = _build_bom_xlsx(with_required_sheet=True, required_rows=[
        ["standardDav6Family", "Dav6", "standardDASv5Family", "Dasv5"],
    ])

    fake_sku_rows = [
        {
            "region": "eastus", "family": "standardDav6Family",
            "display": "Dav6", "zones": [True, True, True],
            "sub_restricted": False, "sub_restriction_raw": "",
        },
        {
            "region": "westus3", "family": "standardDASv5Family",
            "display": "Dasv5", "zones": [True, True, False],
            "sub_restricted": False, "sub_restriction_raw": "",
        },
    ]

    def fake_fetch(*args, **kwargs):
        return list(fake_sku_rows)

    monkeypatch.setattr(compile_mod.arm_sku_availability, "fetch_arm_sku_records", fake_fetch)
    monkeypatch.setattr(compile_mod.quota_groups, "check_quota_groups", lambda *args, **kwargs: {
        "subscription_id": kwargs.get("subscription_id") if kwargs else args[1],
        "status": "no_quota_group",
        "has_quota_groups": False,
        "groups": [],
    })
    monkeypatch.setattr(compile_mod.quota_groups, "check_subscription_quota", lambda *args, **kwargs: {
        "subscription_id": kwargs.get("subscription_id") if kwargs else args[1],
        "status": "ok",
        "regions": {},
    })
    # validate_required_families inside build_model checks that resolved
    # families exist in the SKU dump; our fake rows cover the primary + alt
    # so this passes without further mocking.

    snap = compile_mod.compile_snapshot(
        subscription_id="00000000-0000-0000-0000-000000000001",
        arm_token="fake-token",
        step2_bytes=blob,
        regions=["eastus", "westus3"],
        triggered_by_email="t@example.com",
        triggered_by_oid="oid-1",
    )

    assert "bom_header" in snap, "bom_header missing from snapshot"
    assert "bom_records" in snap, "bom_records missing from snapshot"
    assert "sku_records" in snap, "sku_records missing from snapshot"
    assert snap["bom_header"][:3] == ["Region", "Display Name", "Overall Status"]
    assert len(snap["bom_records"]) == 2
    assert snap["bom_records"][0]["Region"] == "eastus"
    assert snap["sku_records"] == fake_sku_rows
    assert snap["meta"]["skus_source"] == "bom_sheet"
    assert snap["meta"]["engine_version"].startswith("swa-1.")

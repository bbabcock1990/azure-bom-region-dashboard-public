"""Compare Azure VM SKU capabilities to enforce recommendation *parity*.

A cheaper size-equivalent is only a safe swap if it supports **everything** the
original size does. The equivalence groups already guarantee the vCPU:RAM ratio,
and the naming-convention guard in :mod:`pricing` preserves the local temp disk,
but the authoritative source of a size's capabilities is ARM's
``Microsoft.Compute/skus`` ``capabilities`` list. This module parses those size
names + capabilities and reports any capability the original has that the
candidate lacks (accelerated networking, premium/ultra disk, encryption at host,
Hyper-V generation, temp disk size, memory).
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# Boolean capabilities where the substitute must match "True" if the original is
# "True". (name in ARM, friendly label for the UI.)
_BOOL_CAPS: Tuple[Tuple[str, str], ...] = (
    ("PremiumIO", "Premium SSD"),
    ("AcceleratedNetworkingEnabled", "Accelerated networking"),
    ("EncryptionAtHostSupported", "Encryption at host"),
    ("UltraSSDAvailable", "Ultra disk"),
)

_SIZE_RE = re.compile(
    r"^(?:standard_)?([A-Za-z]+?)(\d+)(?:-\d+)?([a-z]*)_v(\d+)$",
    re.IGNORECASE,
)


def parse_size_name(name: str) -> Optional[Tuple[str, int]]:
    """``Standard_D4ps_v6`` -> ``("dpsv6", 4)``; ``None`` if not a standard size.

    Constrained-core variants (``Standard_E4-2s_v5``) map to their full vCPU
    count and base family so they still compare against the same core form.
    """
    m = _SIZE_RE.match(str(name or "").strip())
    if not m:
        return None
    series = m.group(1).lower()
    try:
        vcpus = int(m.group(2))
    except (TypeError, ValueError):
        return None
    features = (m.group(3) or "").lower()
    version = m.group(4)
    return (f"{series}{features}v{version}", vcpus)


def index_by_core(caps_by_size: Dict[str, Dict[str, str]]) -> Dict[str, Dict[int, Dict[str, str]]]:
    """Reindex ``{size_name: caps}`` into ``{core_form: {vcpus: caps}}``."""
    out: Dict[str, Dict[int, Dict[str, str]]] = {}
    for name, caps in (caps_by_size or {}).items():
        parsed = parse_size_name(name)
        if not parsed:
            continue
        core, vcpus = parsed
        out.setdefault(core, {})[vcpus] = caps or {}
    return out


def _as_bool(value) -> bool:
    return str(value).strip().lower() == "true"


def _as_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _gen_set(value) -> set:
    return {p.strip().upper() for p in str(value or "").split(",") if p.strip()}


def compare_caps(orig: Dict[str, str], alt: Dict[str, str]) -> List[Dict[str, str]]:
    """Return capabilities the original has that ``alt`` is missing.

    Each item: ``{"cap": <friendly label>, "detail": <short explanation>}``.
    Empty list means full parity.
    """
    missing: List[Dict[str, str]] = []

    o_temp = _as_float(orig.get("MaxResourceVolumeMB")) or 0.0
    a_temp = _as_float(alt.get("MaxResourceVolumeMB")) or 0.0
    if o_temp > 0 and a_temp <= 0:
        missing.append({"cap": "Temp (local) disk", "detail": "original has a local temp disk"})

    for name, label in _BOOL_CAPS:
        if _as_bool(orig.get(name)) and not _as_bool(alt.get(name)):
            missing.append({"cap": label, "detail": f"original supports {label.lower()}"})

    o_mem = _as_float(orig.get("MemoryGB"))
    a_mem = _as_float(alt.get("MemoryGB"))
    if o_mem is not None and a_mem is not None and a_mem + 1e-6 < o_mem:
        missing.append({
            "cap": "Memory",
            "detail": f"{a_mem:g} GB < {o_mem:g} GB at the same vCPU count",
        })

    o_gens = _gen_set(orig.get("HyperVGenerations"))
    a_gens = _gen_set(alt.get("HyperVGenerations"))
    if o_gens and a_gens:
        lost = sorted(o_gens - a_gens)
        if lost:
            missing.append({
                "cap": f"Hyper-V {', '.join(lost)}",
                "detail": f"original supports {', '.join(sorted(o_gens))}",
            })

    return missing


def parity_check(
    core_index: Dict[str, Dict[int, Dict[str, str]]],
    orig_core: str,
    alt_core: str,
) -> Dict[str, object]:
    """Compare the original vs candidate family capabilities at a shared size.

    Picks the smallest vCPU count offered by **both** families (capabilities are
    consistent across sizes within a family) and compares there. Returns
    ``{"status": "ok"|"incompatible"|"unknown", "missing": [...], "vcpus": n}``.
    ``unknown`` means one of the families wasn't found in the region's SKU list,
    so parity couldn't be established from ARM.
    """
    orig_sizes = core_index.get(str(orig_core).lower()) or {}
    alt_sizes = core_index.get(str(alt_core).lower()) or {}
    if not orig_sizes or not alt_sizes:
        return {"status": "unknown", "missing": [], "vcpus": None}

    common = sorted(set(orig_sizes.keys()) & set(alt_sizes.keys()))
    vcpus = common[0] if common else min(alt_sizes.keys())
    orig_caps = orig_sizes.get(vcpus) or orig_sizes[min(orig_sizes.keys())]
    alt_caps = alt_sizes.get(vcpus) or alt_sizes[min(alt_sizes.keys())]
    missing = compare_caps(orig_caps, alt_caps)
    return {
        "status": "incompatible" if missing else "ok",
        "missing": missing,
        "vcpus": vcpus,
    }

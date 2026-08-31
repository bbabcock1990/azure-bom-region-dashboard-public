"""
Pure-Python analysis. Takes raw rows from sources.load_all and returns a
canonical region-by-region analysis. No file IO, no Excel, no presentation.

AKS deployment model (drives all logic):
  Each required SKU FAMILY must be available in all 3 AZs. For families that
  have a v6/v5 pair we prefer v6, fall back to v5 if v6 isn't usable in all 3
  AZs, otherwise the region is unhealthy for that family. v3 / standalone
  families have no fallback.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Tuple

from . import geo


# ---- BOM definition ---------------------------------------------------------

# Default required SKU families (matches what the Avaya Step 1 export emits).
# Override by dropping a `data/bom.json` file with the same shape.
DEFAULT_REQUIRED_FAMILIES: List[Dict] = [
    {
        "primary_family": "standardDav6Family",
        "primary_label":  "Dasv6",
        "alt_family":     "standardDASv5Family",
        "alt_label":      "Dasv5",
    },
    {
        "primary_family": "standardEav6Family",
        "primary_label":  "Easv6",
        "alt_family":     "standardEASv5Family",
        "alt_label":      "Easv5",
    },
    {
        "primary_family": "standardDSv3Family",
        "primary_label":  "DSv3",
        "alt_family":     None,
        "alt_label":      None,
    },
]


def load_required_families(data_dir: Optional[str], known_families: set) -> List[Dict]:
    """
    Load required families from data/bom.json if present, else use defaults.
    Validates that every family ID actually exists in the SKU dump.
    """
    families = DEFAULT_REQUIRED_FAMILIES
    if data_dir:
        path = os.path.join(data_dir, "bom.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                families = json.load(f)

    for entry in families:
        for k in ("primary_family", "primary_label"):
            if not entry.get(k):
                raise RuntimeError(f"BOM entry missing '{k}': {entry}")
        if entry["primary_family"] not in known_families:
            raise RuntimeError(
                f"BOM primary family '{entry['primary_family']}' not present in "
                f"SKU dump. Known: {sorted(known_families)}"
            )
        alt = entry.get("alt_family")
        if alt and alt not in known_families:
            raise RuntimeError(
                f"BOM alt family '{alt}' not present in SKU dump. "
                f"Known: {sorted(known_families)}"
            )
        # Primary/alt labels MUST differ when an alt is present — recommendation()
        # uses label-equality to detect whether the primary or fallback was chosen,
        # and collapsing them would misclassify fallback regions as primary.
        if alt and entry.get("primary_label") == entry.get("alt_label"):
            raise RuntimeError(
                f"BOM entry primary_label and alt_label must differ when "
                f"alt_family is set (got '{entry.get('primary_label')}' for both)."
            )
    return families


# Pulls a `v<digits>` tier hint out of a family name so the dashboard can label
# things like "v6 vs v5" generically (also works for v5 vs v4, v3-only, etc).
_TIER_RE = re.compile(r"v(\d+)", re.IGNORECASE)


def _extract_tier(family_name: Optional[str]) -> Optional[str]:
    """Return 'v6', 'v5', 'v4', 'v3', ... from a family name, or None."""
    if not family_name:
        return None
    m = _TIER_RE.search(family_name)
    return f"v{m.group(1)}" if m else None


# ---- SKU parsing ------------------------------------------------------------

def index_sku_records(sku_records: List[Dict]) -> Dict[str, Dict[str, Dict]]:
    """
    Convert flat SKU rows into {region_short: {family_id: row}}.
    Each row keeps zones[3], sub_restricted, sub_restriction_raw, display.
    """
    out: Dict[str, Dict[str, Dict]] = {}
    for r in sku_records:
        out.setdefault(r["region"], {})[r["family"]] = r
    return out


def known_families(sku_records: List[Dict]) -> set:
    return {r["family"] for r in sku_records}


def describe_unavailable(sub_restriction_raw: str, zone_idx: int) -> str:
    """
    Convert the Subscription Restriction text into a short per-zone reason.
    zone_idx is 0-based.
    """
    s = (sub_restriction_raw or "").strip()
    sl = s.lower()
    if not sl or sl == "available":
        # Zone N said No but the subscription column claims "Available"
        return f"not available in AZ {zone_idx + 1}"
    if sl == "region: notavailableforsubscription":
        return "not available for this subscription (region-wide)"
    if sl == "sku not in region":
        return "SKU not deployed in this region"
    # e.g. "Restricted in Zone 1", "Restricted in Zones 1, 2"
    return s


# ---- BOM (services) parsing -------------------------------------------------

def extract_missing_services(rec: Dict, header: List[str]) -> List[Dict[str, str]]:
    """Return [{service, detail}] for any ❌-marked service in this record."""
    out = []
    for key in header[3:]:
        if not key:
            continue
        val = rec.get(key)
        if val is None:
            continue
        v = str(val)
        if (
            v.startswith("\u274c")  # ❌
            or "not available" in v.lower()
            or "not in provider list" in v.lower()
        ):
            short = re.sub(r"^[\u2705\u274c]\s*", "", v).strip()
            out.append({"service": str(key), "detail": short})
    return out


def extract_registration_required(rec: Dict, header: List[str]) -> List[Dict[str, str]]:
    """Return [{service, detail, provider}] for any ⚠️ registration-required
    service in this record.

    These are BOM services whose Azure resource provider is not registered on
    the subscription. Availability is unknown until the provider is registered,
    so they are surfaced as an amber warning (with a one-click / CLI register
    hand-off) rather than a red "not available" that fails the BOM.
    The provider namespace is parsed from the trailing ``(Microsoft.X)``.
    """
    out = []
    for key in header[3:]:
        if not key:
            continue
        val = rec.get(key)
        if val is None:
            continue
        v = str(val)
        if v.startswith("\u26a0") or "requires provider registration" in v.lower():
            short = re.sub(r"^[\u26a0\ufe0f]+\s*", "", v).strip()
            m = re.search(r"\(([A-Za-z0-9.]+)\)", short)
            provider = m.group(1) if m else ""
            out.append({
                "service": str(key),
                "detail": short,
                "provider": provider,
            })
    return out


# ---- Per-region SKU analysis ------------------------------------------------

def _zone_state(zones: Optional[List[bool]]) -> str:
    if not zones:
        return "no zones"
    if all(zones):
        return "all 3 zones"
    present = [str(i + 1) for i, ok in enumerate(zones) if ok]
    if not present:
        return "no zones"
    return "Zone " + "/".join(present)


def analyze_skus(
    region_short: str,
    region_skus: Dict[str, Dict],
    required_families: List[Dict],
):
    """
    AKS all-or-nothing per family. Returns:
      (zone_ok[3], blockers, fallbacks, chosen_labels, sku_zone_detail)
    """
    sku_zone_detail: Dict[str, List[bool]] = {}

    if not region_skus:
        return (
            [False, False, False],
            ["No SKU data available for this region"],
            [],
            [None] * len(required_families),
            sku_zone_detail,
        )

    zone_ok = [True, True, True]
    blockers: List[str] = []
    fallbacks: List[str] = []
    chosen: List[Optional[str]] = []

    for req in required_families:
        primary = req["primary_family"]
        alt = req.get("alt_family")
        p_label = req["primary_label"]
        a_label = req.get("alt_label")

        p_row = region_skus.get(primary)
        a_row = region_skus.get(alt) if alt else None

        p_zones = p_row["zones"] if p_row else None
        a_zones = a_row["zones"] if a_row else None

        if p_zones is not None:
            sku_zone_detail[p_label] = p_zones
        if a_zones is not None and a_label:
            sku_zone_detail[a_label] = a_zones

        primary_all = bool(p_zones) and all(p_zones)
        alt_all = bool(a_zones) and all(a_zones)

        if primary_all:
            chosen.append(p_label)
            # Note if fallback has zone gaps (even though primary is fine)
            if a_zones and not alt_all and a_label:
                a_missing = [str(i + 1) for i, ok in enumerate(a_zones) if not ok]
                fallbacks.append(
                    f"Fallback ({a_label}) unavailable in Zone {'/'.join(a_missing)} "
                    f"— Primary ({p_label}) covers all 3 zones"
                )
        elif alt_all:
            chosen.append(a_label)
            if p_zones:
                missing = [str(i + 1) for i, ok in enumerate(p_zones) if not ok]
                fallbacks.append(
                    f"Primary ({p_label}) unavailable in Zone {'/'.join(missing)}; "
                    f"using Fallback ({a_label}) which covers all 3 zones"
                )
            else:
                fallbacks.append(
                    f"Primary ({p_label}) not offered in this region; "
                    f"using Fallback ({a_label}) which covers all 3 zones"
                )
        else:
            chosen.append(None)
            if not p_zones and not a_zones:
                alt_note = f" (Fallback {a_label} also not offered)" if a_label else " (no fallback available)"
                blockers.append(f"Primary ({p_label}) not offered in this region{alt_note}")
            else:
                p_missing = [str(i + 1) for i, ok in enumerate(p_zones) if not ok] if p_zones else []
                a_missing = [str(i + 1) for i, ok in enumerate(a_zones) if not ok] if a_zones else []
                if a_label and a_zones:
                    blockers.append(
                        f"Primary ({p_label}) unavailable in Zone {'/'.join(p_missing)}; "
                        f"Fallback ({a_label}) unavailable in Zone {'/'.join(a_missing)} "
                        f"— neither covers all 3 zones"
                    )
                else:
                    alt_note = "; no fallback available" if not a_label else f"; Fallback ({a_label}) not offered"
                    blockers.append(
                        f"Primary ({p_label}) unavailable in Zone {'/'.join(p_missing)}{alt_note} "
                        f"— cannot deploy zone-redundant"
                    )
            for i in range(3):
                p_ok = p_zones[i] if p_zones else False
                a_ok = a_zones[i] if a_zones else False
                if not p_ok and not a_ok:
                    zone_ok[i] = False

    return zone_ok, blockers, fallbacks, chosen, sku_zone_detail


def derive_zone_restrictions(
    region_skus: Dict[str, Dict],
    required_families: List[Dict],
) -> List[str]:
    """
    Build short per-AZ narrative for the BOM-relevant families using only
    Zone N (Yes/No) and the Subscription Restriction column.
    The noisy 'Zonal Restrictions (raw)' segment list is intentionally NOT
    used - it's customer-segment metadata, not a per-customer signal.
    """
    per_zone_text: List[List[str]] = [[], [], []]

    for req in required_families:
        for fam_key, label in (
            (req["primary_family"], req["primary_label"]),
            (req.get("alt_family"), req.get("alt_label")),
        ):
            if not fam_key:
                continue
            row = region_skus.get(fam_key)
            if not row:
                continue
            for i in range(3):
                if not row["zones"][i]:
                    reason = describe_unavailable(row["sub_restriction_raw"], i)
                    per_zone_text[i].append(f"{label}: {reason}")

    out_text = []
    for i in range(3):
        seen = set()
        unique = []
        for entry in per_zone_text[i]:
            if entry in seen:
                continue
            seen.add(entry)
            unique.append(entry)
        out_text.append(" | ".join(unique))
    return out_text


# ---- Recommendation text ----------------------------------------------------

def recommendation(
    required_families: List[Dict],
    chosen: List[Optional[str]],
) -> Tuple[str, bool, bool]:
    """
    Plain-English primary/fallback verdict + (primary_viable, healthy) flags.

    Returns ("", False, False) for unhealthy regions (any None in `chosen`)
    or any structural mismatch. Tier-agnostic — works for v6/v5, v5/v4,
    standalone v3, etc — because the message is built from the actual
    primary_label / alt_label values, not hardcoded tier names.

    The legacy tuple shape (msg, v6_viable, v5_viable) is preserved for
    backwards-compat with callers that already destructure it; the second
    element is now "primary was used everywhere" (semantically equivalent
    to the old v6_viable when the BOM is a v6→v5 pair).
    """
    if len(required_families) != len(chosen):
        return "", False, False
    if any(c is None for c in chosen):
        return "", False, False

    primary_labels: List[str] = []      # families where primary was chosen
    fell_back_primary: List[str] = []   # primary labels whose families fell back
    fell_back_alt: List[str] = []       # the alt labels actually used as fallback

    for req, ch in zip(required_families, chosen):
        primary_label = req["primary_label"]
        alt_label = req.get("alt_label")
        if ch == primary_label:
            primary_labels.append(primary_label)
        elif alt_label and ch == alt_label:
            fell_back_primary.append(primary_label)
            fell_back_alt.append(alt_label)
        else:
            # chosen label matches neither primary nor alt — shouldn't happen
            # given analyze_skus only picks from those two, but bail safely.
            return "", False, False

    if primary_labels and not fell_back_primary:
        msg = f"Use {', '.join(primary_labels)} in all AZs"
    elif fell_back_primary and not primary_labels:
        msg = (
            f"Use {', '.join(fell_back_alt)} in all AZs "
            f"({', '.join(fell_back_primary)} not available in all 3 AZs)"
        )
    elif primary_labels and fell_back_primary:
        fallback_pairs = ", ".join(
            f"{alt} for {pri}"
            for pri, alt in zip(fell_back_primary, fell_back_alt)
        )
        msg = (
            f"Use {', '.join(primary_labels)} in all AZs; "
            f"use {fallback_pairs} "
            f"({', '.join(fell_back_primary)} not available in all 3 AZs)"
        )
    else:
        msg = ""

    primary_viable = bool(primary_labels) and not fell_back_primary
    return msg, primary_viable, True


# ---- Latency / alternatives -------------------------------------------------

DISPLAY_TO_LATENCY = {
    "australia east": "Australia East", "brazil south": "Brazil South",
    "canada central": "Canada Central", "central india": "Central India",
    "central us": "Central US", "east asia": "East Asia",
    "east us": "East US", "east us 2": "East US 2",
    "france central": "France Central", "germany west central": "Germany West Central",
    "indonesia central": "Indonesia Central", "israel central": "Israel Central",
    "italy north": "Italy North", "japan east": "Japan East",
    "japan west": "Japan West", "korea central": "Korea Central",
    "malaysia west": "Malaysia West", "mexico central": "Mexico Central",
    "new zealand north": "New Zealand North", "north europe": "North Europe",
    "norway east": "Norway East", "poland central": "Poland Central",
    "qatar central": "Qatar Central", "south africa north": "South Africa North",
    "south central us": "South Central US", "southeast asia": "Southeast Asia",
    "sweden central": "Sweden Central", "switzerland north": "Switzerland North",
    "uae north": "UAE North", "uk south": "UK South",
    "west europe": "West Europe", "west us 2": "West US 2",
    "west us 3": "West US 3",
}

GEO_FALLBACK = {
    "austria east": ["Germany West Central", "Italy North", "Sweden Central", "Switzerland North"],
    "belgium central": ["Germany West Central", "Sweden Central", "Switzerland North", "Italy North"],
    "chile central": ["Central US", "Canada Central", "West US 3"],
    "denmark east": ["Sweden Central", "Germany West Central", "Switzerland North"],
    "spain central": ["Italy North", "Germany West Central", "Sweden Central", "Switzerland North"],
}


def _geo_distance_alternatives(display: str, healthy: List[str], top_n: int) -> List[Dict]:
    """Compute distance-based alternatives using ``geo.REGION_GEO`` lat/lon.

    Prefers same-continent candidates; falls through to cross-continent
    by haversine distance if there aren't enough same-continent picks.
    Returns ``[]`` if the source region has no coords (avoids guessing).
    """
    src = geo.coords(display)
    if src is None:
        return []
    src_lat, src_lon = src
    src_continent = geo.lookup(display).get("continent") or "Unknown"

    same: List[Dict] = []
    other: List[Dict] = []
    for h in healthy:
        if h.lower() == display.lower():
            continue
        h_coord = geo.coords(h)
        if h_coord is None:
            continue
        h_lat, h_lon = h_coord
        km = geo.haversine_km(src_lat, src_lon, h_lat, h_lon)
        rec = {
            "region": h,
            "latency_ms": None,
            "distance_km": int(round(km)),
            "source": "geo_distance",
        }
        h_continent = geo.lookup(h).get("continent") or "Unknown"
        (same if h_continent == src_continent and src_continent != "Unknown"
         else other).append(rec)
    same.sort(key=lambda c: c["distance_km"])
    other.sort(key=lambda c: c["distance_km"])
    combined = same + other
    return combined[:top_n]


def alternatives(display: str, latency: Dict, healthy: List[str], top_n: int = 3) -> List[Dict]:
    if not healthy:
        return []
    src_name = DISPLAY_TO_LATENCY.get(display.lower())
    if src_name and src_name in latency:
        cand = []
        for h in healthy:
            if h.lower() == display.lower():
                continue
            h_name = DISPLAY_TO_LATENCY.get(h.lower())
            if not h_name:
                continue
            ms = latency[src_name].get(h_name)
            if ms is None:
                continue
            cand.append({"region": h, "latency_ms": ms, "source": "ms_published"})
        cand.sort(key=lambda c: c["latency_ms"])
        if cand:
            return cand[:top_n]
        # Fall through to geo fallbacks if no published latency pair matched
        # a healthy region.

    fb = GEO_FALLBACK.get(display.lower(), [])
    healthy_set = {h.lower() for h in healthy}
    fb_results = [
        {"region": r, "latency_ms": None, "source": "geo_fallback"}
        for r in fb if r.lower() in healthy_set
    ][:top_n]
    if fb_results:
        return fb_results

    # Final fallback: distance-based using geo.REGION_GEO. Covers regions
    # that aren't in the latency CSV *and* aren't hand-listed in
    # GEO_FALLBACK (e.g. newly-launched regions like Indonesia Central),
    # and regions whose curated GEO_FALLBACK list filtered to zero
    # healthy candidates.
    return _geo_distance_alternatives(display, healthy, top_n)


# ---- Top-level pipeline -----------------------------------------------------

def build_model(raw: Dict,
                data_dir: Optional[str] = None,
                required_families: Optional[List[Dict]] = None) -> Dict:
    """
    Take {sku_records, bom_header, bom_records, latency} from sources.load_all
    and produce the canonical analysis dict for snapshotting.

    ``required_families`` (when supplied) is the resolved per-customer SKU
    family list and skips the ``data/bom.json`` / defaults fallback. The
    web compile path always passes this in; the local CLI path (which
    historically only had ``data_dir``) still falls back to file/defaults.
    """
    sku_records = raw["sku_records"]
    bom_header = raw["bom_header"]
    bom_records = raw["bom_records"]
    latency = raw["latency"]

    sku_index = index_sku_records(sku_records)
    fams = known_families(sku_records)
    if required_families is None:
        required_families = load_required_families(data_dir, fams)
    else:
        # Light validation against the SKU dump so a user-supplied family
        # that the availability feed never returned doesn't silently render
        # every region red. We DO allow it (some peers may want to query a
        # family the default sub doesn't see) but we surface it as a warning row.
        for entry in required_families:
            pf = entry.get("primary_family")
            if pf and pf not in fams:
                print(f"  warn: required family '{pf}' had no availability rows — every region will look blocked for it.")
            alt = entry.get("alt_family")
            if alt and alt not in fams:
                print(f"  warn: alt family '{alt}' had no availability rows.")

    # Region key sanity check: every BOM region should have either SKU rows
    # or be flagged. Don't fail; we surface "no SKU data" as a blocker.
    sku_regions = set(sku_index.keys())
    bom_regions = {str(r.get("Region") or "").strip().lower() for r in bom_records}
    missing_in_sku = bom_regions - sku_regions
    if missing_in_sku:
        # informational only - those regions will get the "no SKU data" blocker
        print(f"  note: {len(missing_in_sku)} BOM region(s) absent from SKU dump: "
              f"{sorted(missing_in_sku)[:5]}{'...' if len(missing_in_sku) > 5 else ''}")

    bom_records.sort(key=lambda r: str(r.get("Display Name") or "").lower())

    analyses = []
    for rec in bom_records:
        region_short = str(rec.get("Region") or "").strip().lower()
        display = str(rec.get("Display Name") or "").strip()
        overall_status = str(rec.get("Overall Status") or "")
        if not region_short or not display:
            continue

        region_skus = sku_index.get(region_short, {})
        zone_ok, sku_blockers, sku_fallbacks, chosen, sku_zone_detail = analyze_skus(
            region_short, region_skus, required_families
        )

        zone_restrictions = derive_zone_restrictions(region_skus, required_families)
        # Block flags come straight from the SKU analysis (zone_ok already
        # reflects subscription restrictions because read_sku_v2 ANDs them in).
        zone_constraint_blocks = [not z for z in zone_ok]

        missing_services = extract_missing_services(rec, bom_header)
        registration_required = extract_registration_required(rec, bom_header)

        is_supported = "SUPPORTED" in overall_status and "UNSUPPORTED" not in overall_status
        # Any ❌ service in the BOM is a deal-breaker even if Overall Status
        # claims SUPPORTED. Both signals must agree.
        has_bom_issue = (not is_supported) or bool(missing_services)
        has_sku_blocker = bool(sku_blockers)
        any_zone_blocked = any(zone_constraint_blocks)

        # A region is "healthy" (deployable) only when there is no BOM issue and
        # no compute blocker — including per-zone restrictions. Previously zone
        # blocks were excluded here, so a region could report deployment_health
        # "Yes" while its status said "Compute Issue" (a contradictory verdict).
        compute_red = has_sku_blocker or any_zone_blocked
        is_healthy = not has_bom_issue and not compute_red

        if has_bom_issue and compute_red:
            status = "BOM & Compute Issue"
        elif has_bom_issue:
            status = "BOM Issue"
        elif compute_red:
            status = "Compute Issue"
        else:
            status = "OK"

        rec_msg, primary_viable, sku_healthy = recommendation(required_families, chosen)
        geo_info = geo.lookup(display)

        analyses.append({
            "name": display,
            "short": region_short,
            "geo": geo_info["continent"],
            "country": geo_info["country"],
            "coords": [geo_info["lat"], geo_info["lon"]],
            "deployment_health": "Yes" if is_healthy else "No",
            "status": status,
            "zone_health": [
                "green" if (zone_ok[i] and not zone_constraint_blocks[i]) else "red"
                for i in range(3)
            ],
            "zone_restrictions": zone_restrictions,
            "zone_constraint_blocks": zone_constraint_blocks,
            "sku_blockers": sku_blockers,
            "sku_fallbacks": sku_fallbacks,
            "missing_services": missing_services,
            "registration_required": registration_required,
            # Generic primary/fallback flags (tier-agnostic — work for v6/v5,
            # v5/v4, or any other primary+alt pair the user puts in their BOM).
            "primary_used": primary_viable,
            "fell_back": bool(sku_fallbacks),
            # Legacy aliases kept so older clients and snapshots keep working.
            "v6_viable": primary_viable,
            "v5_viable": sku_healthy if is_healthy else False,
            "recommendation": rec_msg,
            "chosen_skus": [c for c in chosen if c],
            "sku_zone_detail": sku_zone_detail,
            "has_zone_restriction": any(bool(r) for r in zone_restrictions),
            "overall_status_raw": overall_status,
        })

    healthy = [a["name"] for a in analyses if a["deployment_health"] == "Yes"]

    for a in analyses:
        if a["deployment_health"] == "No":
            a["alt_regions"] = alternatives(a["name"], latency, healthy, top_n=3)
        else:
            a["alt_regions"] = []

    bom_skus_serialized = [
        {
            "primary": req["primary_label"],
            "alt": req.get("alt_label"),
            "primary_family": req["primary_family"],
            "alt_family": req.get("alt_family"),
            "primary_tier": _extract_tier(req["primary_family"]),
            "alt_tier": _extract_tier(req.get("alt_family")),
            "required_cores": req.get("required_cores"),
        }
        for req in required_families
    ]

    return {
        "bom": {"skus": bom_skus_serialized},
        "regions": analyses,
        "latency_matrix": latency,
        "stats": {
            "total_regions": len(analyses),
            "healthy_regions": len(healthy),
            "unhealthy_regions": len(analyses) - len(healthy),
        },
    }

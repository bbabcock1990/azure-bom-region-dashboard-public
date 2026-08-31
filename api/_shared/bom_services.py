"""
In-app BOM service availability checks (server-side replacement for the
``check_azure_regions.py`` CLI).

For each service in a BOM, we ask Azure Resource Manager which regions
the underlying resource provider supports. For zone-checked services
(currently only Premium SSD v2) we also query the per-region zone list
via ``Microsoft.Compute/skus``.

Auth: caller passes an ARM bearer token (audience
``https://management.azure.com/.default``). In LOCAL_MODE the launcher
mints this via ``az account get-access-token --subscription <sub-id>``.

This module is intentionally small and pure so it's easy to unit test
with mocked httpx responses.
"""
from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Iterable, List, Optional, Tuple

import httpx

log = logging.getLogger(__name__)

ARM_BASE = "https://management.azure.com"
PROVIDER_API_VERSION = "2024-07-01"
COMPUTE_SKUS_API_VERSION = "2024-07-01"
DEFAULT_TIMEOUT_S = 60.0
MAX_PARALLEL = 8

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_CATALOG_PATH = os.path.join(_DATA_DIR, "bom_service_catalog.json")

# Cache the catalog at module load — it's a small static file.
_CATALOG_CACHE: Optional[List[Dict]] = None


def reset_dataset_caches() -> None:
    """Drop memoized dataset state (service catalog + latency display map)
    so a freshly uploaded override is picked up without a restart."""
    global _CATALOG_CACHE, _REGION_DISPLAY_CACHE
    _CATALOG_CACHE = None
    _REGION_DISPLAY_CACHE = None


class BomServicesError(Exception):
    """Stable error code we hand back to callers for nice UI messages."""

    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ─── Catalog ─────────────────────────────────────────────────────────────────

def load_builtin_catalog() -> List[Dict]:
    """Return the static service catalog from the JSON seed file.

    Cached after the first call. To pick up edits to the JSON file,
    restart the function host.
    """
    global _CATALOG_CACHE
    if _CATALOG_CACHE is None:
        from . import dataset_store
        with open(dataset_store.resolve_path("service_catalog"), "r", encoding="utf-8") as f:
            data = json.load(f)
        services = data.get("services") or []
        # Defensive copy + normalize types so callers can't accidentally
        # mutate the cached list.
        _CATALOG_CACHE = [
            {
                "name": str(s["name"]).strip(),
                "provider": str(s["provider"]).strip(),
                "resource_type": str(s["resource_type"]).strip(),
                "zone_check": bool(s.get("zone_check", False)),
            }
            for s in services
            if s.get("name") and s.get("provider") and s.get("resource_type")
        ]
    return [dict(s) for s in _CATALOG_CACHE]


def load_catalog() -> List[Dict]:
    """Return the merged service catalog (built-in + user-added custom).

    Each entry carries ``is_custom`` so the picker can show a delete
    affordance only on rows the user can remove. Custom entries with a
    name that collides with a built-in are skipped — the built-in wins
    so a re-run after pulling new defaults can't be silently shadowed
    by an old custom override. The HTTP layer enforces this on add too;
    this is just defense-in-depth on read.
    """
    builtin = load_builtin_catalog()
    builtin_by_name_lc = {s["name"].lower(): s for s in builtin}
    out: List[Dict] = [{**s, "is_custom": False} for s in builtin]

    # Lazy import to avoid a circular dependency at module load
    # (bom_catalog → storage which Functions tooling sometimes imports
    # before bom_services is fully defined).
    from . import bom_catalog
    for c in bom_catalog.list_custom("service"):
        try:
            v = bom_catalog.validate_service(c)
        except bom_catalog.BomCatalogError:
            log.warning("bom_services: dropping invalid custom service %r", c)
            continue
        if v["name"].lower() in builtin_by_name_lc:
            # Shadowing a built-in is rejected on add, but we may have
            # stale rows from before that check existed.
            log.info("bom_services: skipping custom service %s (shadows built-in)",
                     v["name"])
            continue
        out.append({**v, "is_custom": True})

    # Stable sort: built-ins first, then customs, each alphabetical.
    out.sort(key=lambda s: (s["is_custom"], s["name"].lower()))
    return out


def catalog_by_name() -> Dict[str, Dict]:
    """Lookup table for quick service-name → catalog entry resolution.

    Strips the ``is_custom`` flag — callers (resolve_services etc.)
    don't need it and historically saw the built-in shape only.
    """
    out: Dict[str, Dict] = {}
    for s in load_catalog():
        # Don't expose is_custom in the lookup so call sites that copy
        # the dict don't accidentally persist it.
        out[s["name"]] = {k: v for k, v in s.items() if k != "is_custom"}
    return out


def resolve_services(service_names: Iterable[str]) -> List[Dict]:
    """Look up each name in the catalog. Unknown names raise BomServicesError
    so callers fail loud rather than silently dropping services."""
    cat = catalog_by_name()
    out: List[Dict] = []
    unknown: List[str] = []
    seen: set = set()
    for name in service_names:
        if not name:
            continue
        key = name.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        entry = cat.get(key)
        if entry is None:
            unknown.append(key)
            continue
        out.append(dict(entry))
    if unknown:
        raise BomServicesError(
            "unknown_services",
            f"Unknown service name(s): {', '.join(unknown[:10])}",
            400,
        )
    return out


# ─── Region normalization ───────────────────────────────────────────────────

def _normalize_region(s: str) -> str:
    """Same normalization the CLI used so our results match.

    Strips whitespace and parens, lowercases. Azure returns ARM region
    locations in both 'East US' (display) and 'eastus' (canonical) forms
    depending on endpoint, so we normalize both sides before comparing.
    """
    if not s:
        return ""
    return s.lower().replace(" ", "").replace("(", "").replace(")", "")


# ─── ARM: resource provider locations ────────────────────────────────────────

def _strip_bearer(token: str) -> str:
    t = (token or "").strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    return t


def _fetch_provider_locations_one(
    client: httpx.Client,
    *,
    provider: str,
    resource_type: str,
    headers: Dict[str, str],
) -> List[str]:
    """Call ARM ``GET /providers/{provider}?api-version=...`` and return the
    list of locations for the given resource type. Returns ``["*"]`` if the
    service is globally available (e.g. Azure DNS zones)."""
    url = f"{ARM_BASE}/providers/{provider}"
    params = {"api-version": PROVIDER_API_VERSION}
    r = client.get(url, params=params, headers=headers)
    if r.status_code == 401:
        raise BomServicesError(
            "arm_unauthorized",
            "ARM rejected the bearer token (401). Run `az login` and retry.",
            401,
        )
    if r.status_code == 403:
        raise BomServicesError(
            "arm_forbidden",
            f"ARM forbade provider show for {provider} (403). The bearer "
            f"token lacks Reader on the chosen subscription's tenant.",
            403,
        )
    if r.status_code >= 400:
        raise BomServicesError(
            "arm_provider_show_failed",
            f"ARM returned {r.status_code} for provider show {provider}: "
            f"{r.text[:300]}",
            502,
        )
    try:
        data = r.json()
    except Exception as ex:
        raise BomServicesError(
            "arm_bad_json",
            f"ARM returned non-JSON for provider show {provider}: {ex}",
            502,
        )
    for rt in data.get("resourceTypes") or []:
        if rt.get("resourceType", "").lower() == resource_type.lower():
            locs = rt.get("locations") or []
            if any((loc or "").lower() == "global" for loc in locs):
                return ["*"]
            return [str(loc) for loc in locs if loc]
    # Provider exists but the requested resource type isn't listed —
    # treat as "not available anywhere" so the UI shows ❌ instead of
    # silently being green.
    return []


def fetch_provider_locations(
    services: List[Dict],
    *,
    arm_token: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Dict[str, List[str]]:
    """Pre-fetch ``provider show`` locations for every distinct
    (provider, resource_type) pair in ``services``. Returns a dict keyed by
    ``f"{provider}/{resource_type}"`` mapping to the raw ARM location list
    (or ``["*"]`` for global services).

    Parallelized with a small thread pool (one call per pair, not per region).
    """
    token = _strip_bearer(arm_token)
    if not token:
        raise BomServicesError("missing_token", "ARM bearer token is required.", 400)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    # Distinct pairs only — multiple BOM services can map to the same
    # provider/resource_type (e.g. Storage / storageAccounts).
    pairs: List[Tuple[str, str]] = []
    seen: set = set()
    for svc in services:
        if svc.get("zone_check"):
            continue  # zone services don't go through provider show
        key = (svc["provider"], svc["resource_type"])
        if key in seen:
            continue
        seen.add(key)
        pairs.append(key)

    out: Dict[str, List[str]] = {}
    if not pairs:
        return out

    workers = min(MAX_PARALLEL, len(pairs))
    with httpx.Client(timeout=timeout_s, http2=False) as client:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(
                    _fetch_provider_locations_one,
                    client,
                    provider=p,
                    resource_type=rt,
                    headers=headers,
                ): f"{p}/{rt}"
                for (p, rt) in pairs
            }
            for fut in as_completed(futs):
                key = futs[fut]
                try:
                    out[key] = fut.result()
                except BomServicesError:
                    raise
                except Exception as ex:
                    raise BomServicesError(
                        "arm_provider_show_failed",
                        f"ARM provider show failed for {key}: {ex!r}",
                        502,
                    )
    return out


# ─── ARM: Premium SSD v2 zone availability ──────────────────────────────────

_SSDV2_SKU_NAME = "PremiumV2_LRS"


def fetch_ssdv2_zones(
    *,
    arm_token: str,
    subscription_id: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Dict[str, List[str]]:
    """Return ``{region_normalized: [zone, ...]}`` for the Premium SSD v2
    disk SKU. One call to ``/subscriptions/{sub_id}/providers/Microsoft.Compute/skus``
    pulls every region in the catalog.

    ``subscription_id`` is only used to satisfy ARM's URL shape — Premium
    SSD v2 zone availability is a **global property** of the SKU per
    region, not subscription-specific. Callers running cross-tenant
    analyses should pass the operator's *own* subscription here (the one
    the bearer was minted against), not the customer's subscription, to
    avoid 401/403 in the foreign tenant.

    If the subscription has no access to Compute (rare), returns ``{}``
    rather than raising — the caller will then mark every region's SSD v2
    column as ❌ which matches the "no zones" outcome the CLI produces.
    """
    token = _strip_bearer(arm_token)
    if not token:
        raise BomServicesError("missing_token", "ARM bearer token is required.", 400)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    url = (
        f"{ARM_BASE}/subscriptions/{subscription_id}"
        f"/providers/Microsoft.Compute/skus"
    )
    params = {
        "api-version": COMPUTE_SKUS_API_VERSION,
        "$filter": f"resourceType eq 'disks'",
    }
    out: Dict[str, List[str]] = {}
    next_url: Optional[str] = url
    with httpx.Client(timeout=timeout_s, http2=False) as client:
        while next_url:
            r = client.get(next_url, params=params if next_url == url else None, headers=headers)
            if r.status_code in (401, 403):
                # Cross-tenant case: the BOM availability flow may pass a
                # default-tenant ARM token that's valid for tenant-agnostic
                # /providers/* calls but not for /subscriptions/{X}/... when
                # the operator has no rights in X's tenant. Treat that the
                # same as "no zone data" — the caller will mark every
                # region's SSD v2 column as ❌, which matches the
                # conservative outcome the CLI produces.
                log.warning("ssdv2 zones: ARM %d for sub=%s — returning empty",
                            r.status_code, subscription_id)
                return {}
            if r.status_code >= 400:
                raise BomServicesError(
                    "arm_compute_skus_failed",
                    f"ARM returned {r.status_code} for Microsoft.Compute/skus: "
                    f"{r.text[:300]}",
                    502,
                )
            try:
                data = r.json()
            except Exception as ex:
                raise BomServicesError(
                    "arm_bad_json",
                    f"ARM returned non-JSON for Microsoft.Compute/skus: {ex}",
                    502,
                )
            for item in data.get("value") or []:
                if item.get("name") != _SSDV2_SKU_NAME:
                    continue
                if item.get("resourceType") != "disks":
                    continue
                for loc_info in item.get("locationInfo") or []:
                    region = (loc_info.get("location") or "").strip()
                    zones = loc_info.get("zones") or []
                    if not region:
                        continue
                    key = _normalize_region(region)
                    # Merge in case the same region appears more than once.
                    existing = set(out.get(key, []))
                    existing.update(str(z) for z in zones if z is not None)
                    out[key] = sorted(existing)
            next_url = data.get("nextLink") or None
    return out


# ─── Result aggregation ─────────────────────────────────────────────────────

def check_services_availability(
    services: List[Dict],
    regions_to_check: List[Dict],
    *,
    arm_token: str,
    subscription_id: str,
    ssdv2_arm_token: Optional[str] = None,
    ssdv2_subscription_id: Optional[str] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> List[Dict]:
    """Top-level entry point. Given a resolved list of services and a list
    of regions (each ``{name, display_name}``), return one record per region:

        {
          "region": "eastus",
          "display_name": "East US",
          "overall": "PASS"|"FAIL",
          "services": {
              "Azure Automation": {"available": True, "detail": ""},
              "Premium SSD v2": {"available": False, "detail": "only 2 zone(s)"},
              ...
          },
        }

    Mirrors ``check_azure_regions.run_checks()`` so the synthesized
    ``bom_records`` are byte-for-byte compatible with the legacy xlsx path.

    ``arm_token`` is used for tenant-agnostic /providers/{ns} calls and
    must be valid (any tenant the operator can log into).
    ``ssdv2_arm_token`` is used for the subscription-scoped
    Microsoft.Compute/skus call; if omitted it falls back to ``arm_token``.
    ``ssdv2_subscription_id`` chooses which subscription appears in the
    Microsoft.Compute/skus URL — if omitted it falls back to
    ``subscription_id``. For cross-tenant analyses, pass the operator's
    own subscription (the one the bearer was minted against) — Premium
    SSD v2 zone availability is global per SKU per region, so the
    answer is identical regardless of which sub is queried.
    Otherwise the call falls back to
    empty zones on 401/403.
    """
    if not services:
        # Edge case: empty service list — every region passes trivially.
        return [
            {
                "region": r["name"],
                "display_name": r.get("display_name") or r["name"],
                "overall": "PASS",
                "services": {},
            }
            for r in regions_to_check
        ]

    provider_locations = fetch_provider_locations(
        services, arm_token=arm_token, timeout_s=timeout_s,
    )
    ssdv2_zones: Dict[str, List[str]] = {}
    if any(s.get("zone_check") for s in services):
        ssdv2_zones = fetch_ssdv2_zones(
            arm_token=ssdv2_arm_token or arm_token,
            subscription_id=ssdv2_subscription_id or subscription_id,
            timeout_s=timeout_s,
        )

    results: List[Dict] = []
    for region_info in regions_to_check:
        region_name = region_info["name"]
        norm_name = _normalize_region(region_name)
        display = region_info.get("display_name") or region_name

        services_result: Dict[str, Dict] = {}
        overall = "PASS"

        for svc in services:
            svc_name = svc["name"]
            if svc.get("zone_check"):
                zones = ssdv2_zones.get(norm_name) or []
                if len(zones) >= 3:
                    services_result[svc_name] = {
                        "available": True,
                        "detail": f"{len(zones)} zones",
                    }
                else:
                    services_result[svc_name] = {
                        "available": False,
                        "detail": f"only {len(zones)} zone(s)" if zones else "not available",
                    }
                    overall = "FAIL"
            else:
                key = f"{svc['provider']}/{svc['resource_type']}"
                available_locs = provider_locations.get(key, [])
                matched = (
                    "*" in available_locs
                    or region_name.lower() in [str(loc).lower() for loc in available_locs]
                    or any(_normalize_region(loc) == norm_name for loc in available_locs)
                )
                if matched:
                    services_result[svc_name] = {"available": True, "detail": ""}
                else:
                    services_result[svc_name] = {
                        "available": False,
                        "detail": "not in provider list",
                    }
                    overall = "FAIL"

        results.append({
            "region": region_name,
            "display_name": display,
            "overall": overall,
            "services": services_result,
        })

    return results


# ─── BOM records synthesis ──────────────────────────────────────────────────

def synthesize_bom_records(
    services: List[Dict],
    region_results: List[Dict],
) -> Tuple[List[str], List[Dict]]:
    """Build ``(bom_header, bom_records)`` in the exact shape
    ``api/_shared/pipeline/model.py`` expects from a legacy xlsx upload.

    Header is ``[Region, Display Name, Overall Status, <svc1>, <svc2>, …]``.
    Each record has the per-service column populated with
    ``"✅ Available"`` / ``"❌ not in provider list"`` strings so
    ``extract_missing_services()`` sees the same ❌ markers and
    ``Overall Status`` contains ``"SUPPORTED"`` / ``"UNSUPPORTED"``.

    With an empty ``services`` list the header is just the first three
    columns and every region is marked ``"✅ SUPPORTED"`` — i.e. an empty
    BOM is a clean pass, not a forced fail.
    """
    svc_names = [s["name"] for s in services]
    header = ["Region", "Display Name", "Overall Status"] + svc_names

    records: List[Dict] = []
    for rr in region_results:
        rec: Dict = {
            "Region": rr["region"],
            "Display Name": rr.get("display_name") or rr["region"],
        }
        overall = rr.get("overall") or "PASS"
        rec["Overall Status"] = (
            "✅ SUPPORTED" if overall == "PASS" else "❌ UNSUPPORTED"
        )
        for name in svc_names:
            svc_result = (rr.get("services") or {}).get(name) or {}
            available = svc_result.get("available")
            detail = svc_result.get("detail") or ""
            if available is True:
                rec[name] = f"✅ {detail}" if detail else "✅ Available"
            else:
                rec[name] = f"❌ {detail}" if detail else "❌ Not available"
        records.append(rec)
    return header, records


def synthesize_empty_bom(
    region_specs: List[Dict],
) -> Tuple[List[str], List[Dict]]:
    """Convenience for the "no services in the BOM yet" case — produces a
    header with just the 3 core columns and every region marked SUPPORTED.

    Used when the user wants to run analysis from a saved BOM that only
    has required SKUs (no services). ARM still drives the SKU
    side of the analysis; service availability simply isn't evaluated.
    """
    header = ["Region", "Display Name", "Overall Status"]
    records: List[Dict] = []
    for r in region_specs:
        records.append({
            "Region": r["name"],
            "Display Name": r.get("display_name") or r["name"],
            "Overall Status": "✅ SUPPORTED",
        })
    return header, records


# ─── Region spec helpers ────────────────────────────────────────────────────

# Cache the short→display map after the first read of the latency CSV
# headers. Static data, safe to keep across requests.
_REGION_DISPLAY_CACHE: Optional[Dict[str, str]] = None


def load_region_display_map(data_dir: str) -> Dict[str, str]:
    """Return ``{short_name: display_name}`` built from the latency CSV
    header row (which uses the human-readable region names). Missing
    regions return ``None``; callers fall back to the short name.
    """
    global _REGION_DISPLAY_CACHE
    if _REGION_DISPLAY_CACHE is not None:
        return dict(_REGION_DISPLAY_CACHE)
    from . import dataset_store
    path = dataset_store.resolve_path("latency")
    out: Dict[str, str] = {}
    if not os.path.exists(path):
        _REGION_DISPLAY_CACHE = out
        return dict(out)
    with open(path, "r", encoding="utf-8") as f:
        first = f.readline()
    if not first:
        _REGION_DISPLAY_CACHE = out
        return dict(out)
    # CSV: first column header is 'Source'; columns 2+ are display names.
    columns = [c.strip() for c in first.rstrip("\r\n").split(",")]
    for col in columns[1:]:
        if not col:
            continue
        key = _normalize_region(col)
        if key:
            out[key] = col
    _REGION_DISPLAY_CACHE = out
    return dict(out)


def build_region_specs(
    region_short_names: List[str],
    *,
    data_dir: str,
) -> List[Dict]:
    """Combine short names (from regions.txt or caller) with the display
    name lookup to produce ``[{name, display_name}, ...]`` ready to feed
    into check_services_availability / synthesize_bom_records.

    Display name resolution cascades:
      1. Latency CSV header (preserves exact strings other modules join on)
      2. Master ``bom_region_catalog.json`` (always covers newly-launched
         regions like Austria East / Belgium Central / Indonesia Central)
      3. Raw short name (last-resort — surfaces the regression visually
         so it gets noticed and fixed)
    """
    # Imported lazily so the heavier bom_regions module isn't pulled in
    # by callers that only need build_region_specs's basic shape.
    from . import bom_regions
    display_map = load_region_display_map(data_dir)
    catalog_map = bom_regions.display_map()
    out: List[Dict] = []
    seen: set = set()
    for raw in region_short_names:
        if not raw:
            continue
        short = str(raw).strip().lower()
        if not short or short in seen:
            continue
        seen.add(short)
        display = (
            display_map.get(short)
            or catalog_map.get(short)
            or short
        )
        out.append({"name": short, "display_name": display})
    return out

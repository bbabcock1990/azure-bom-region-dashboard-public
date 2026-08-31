"""Azure VM list-price estimation via the public Retail Prices API.

The BOM is expressed as *required vCPU cores per VM family* (not explicit VM
sizes and counts), so cost is derived as::

    monthly = (list $/hr for a representative size / that size's vCPUs)
              * required_cores * hours_per_month

Prices come from the public Azure Retail Prices API
(https://prices.azure.com/api/retail/prices) which needs **no authentication**.

Everything here is an ESTIMATE and is surfaced as such in the UI. Specifically:
- Each family is anchored to a representative size (``Standard_D4s_v5`` etc.)
  and we assume the size's numeric token equals its vCPU count.
- We price the on-demand (pay-as-you-go) Consumption meter for the chosen OS,
  excluding Spot / Low Priority.
- Families we can't map to a standard size (e.g. specialized GPU series) are
  returned as *unpriced* rather than guessed.

An Azure Consumption Discount (ACD) percentage from settings is applied on top
of the list price to produce a "net" figure.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

RETAIL_PRICES_URL = "https://prices.azure.com/api/retail/prices"
DEFAULT_CURRENCY = "USD"
DEFAULT_HOURS_PER_MONTH = 730
HTTP_TIMEOUT_S = 30.0

# Representative vCPU sizes to try, in order. The first that resolves to a price
# wins; the per-core rate is (price for that size / that size's vCPUs). Prices
# scale ~linearly with vCPUs within a family, so the anchor choice is not
# sensitive — it just needs to exist in the region.
ANCHOR_VCPUS: Tuple[int, ...] = (4, 2, 8, 16)

# Retail prices change infrequently; cache per (currency, os, region, family).
_CACHE_TTL_S = 6 * 3600
_LOCK = threading.Lock()
# key -> (expires_at_epoch, result_or_None)
_PRICE_CACHE: Dict[Tuple[str, str, str, str], Tuple[float, Optional[dict]]] = {}


def reset_cache() -> None:
    with _LOCK:
        _PRICE_CACHE.clear()


def _norm_region(s: str) -> str:
    return re.sub(r"\s+", "", (s or "").strip().lower())


def _family_core(family: str) -> str:
    """``standardDasv6Family`` -> ``Dasv6``; a plain label passes through."""
    s = (family or "").strip()
    m = re.match(r"^standard(.+?)family$", s, re.IGNORECASE)
    return (m.group(1) if m else s).strip()


def anchor_size_for(family: str, vcpus: int) -> Optional[str]:
    """Build a representative ``armSkuName`` for a family at ``vcpus`` cores.

    ``Dasv6`` + 4 -> ``Standard_D4as_v6``. Returns ``None`` when the family
    label doesn't look like a standard ``<letter><features>v<n>`` VM series
    (e.g. specialized GPU/HPC families with subfamily digits).
    """
    core = _family_core(family)
    m = re.match(r"^([A-Za-z])([A-Za-z]*?)v(\d+)$", core)
    if not m:
        return None
    series = m.group(1).upper()
    features = (m.group(2) or "").lower()
    version = m.group(3)
    return f"Standard_{series}{vcpus}{features}_v{version}"


def _matches_target_os(item: dict, os_name: str) -> bool:
    """True when a retail item is the on-demand PAYG meter for ``os_name``."""
    if (item.get("type") or "") != "Consumption":
        return False
    if (item.get("unitOfMeasure") or "") != "1 Hour":
        return False
    sku = str(item.get("skuName") or "")
    meter = str(item.get("meterName") or "")
    blob = f"{sku} {meter}".lower()
    if "spot" in blob or "low priority" in blob:
        return False
    is_windows = "windows" in str(item.get("productName") or "").lower()
    return is_windows if os_name == "windows" else not is_windows


def _fetch_region_size_prices(
    region: str,
    sizes: List[str],
    *,
    currency: str,
    os_name: str,
) -> Dict[str, float]:
    """Return ``{armSkuName: hourly_list_price}`` for ``sizes`` in ``region``.

    A single Retail Prices API call covers all requested sizes (they're OR'd in
    the ``$filter``). Missing sizes are simply absent from the result.
    """
    if not sizes:
        return {}
    try:
        import httpx
    except Exception:  # pragma: no cover - httpx is a hard dependency
        return {}

    size_filter = " or ".join(f"armSkuName eq '{s}'" for s in sizes)
    flt = (
        "serviceName eq 'Virtual Machines' "
        f"and armRegionName eq '{region}' "
        "and priceType eq 'Consumption' "
        f"and ({size_filter})"
    )
    params: Optional[dict] = {"currencyCode": f"'{currency}'", "$filter": flt}
    url = RETAIL_PRICES_URL
    out: Dict[str, float] = {}
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            while True:
                resp = client.get(url, params=params)
                if resp.status_code >= 400:
                    log.info(
                        "pricing: retail API %s for region=%s (%s)",
                        resp.status_code, region, resp.text[:200],
                    )
                    return out
                body = resp.json()
                for item in body.get("Items") or []:
                    if not _matches_target_os(item, os_name):
                        continue
                    name = str(item.get("armSkuName") or "")
                    price = item.get("retailPrice")
                    if not name or price is None:
                        continue
                    price = float(price)
                    # Keep the lowest matching on-demand rate per size.
                    if name not in out or price < out[name]:
                        out[name] = price
                nxt = body.get("NextPageLink")
                if not nxt:
                    break
                url = nxt
                params = None  # NextPageLink carries the full query
    except Exception as ex:  # pragma: no cover - network best-effort
        log.info("pricing: retail API call failed for region=%s (%r)", region, ex)
    return out


def price_families_in_region(
    region: str,
    families: List[str],
    *,
    os_name: str = "linux",
    currency: str = DEFAULT_CURRENCY,
) -> Dict[str, Optional[dict]]:
    """Return ``{family: {per_core_hour, anchor_size, vcpus, hourly} | None}``.

    ``None`` means we could not price the family (unmappable series or the
    region returned no matching on-demand meter).
    """
    region = _norm_region(region)
    os_name = "windows" if str(os_name).strip().lower() == "windows" else "linux"
    currency = (currency or DEFAULT_CURRENCY).strip().upper() or DEFAULT_CURRENCY
    now = time.time()

    results: Dict[str, Optional[dict]] = {}
    to_fetch: Dict[str, List[Tuple[int, str]]] = {}
    all_sizes: set = set()

    for fam in families:
        key = (currency, os_name, region, str(fam or "").strip().lower())
        with _LOCK:
            cached = _PRICE_CACHE.get(key)
        if cached and cached[0] > now:
            results[fam] = cached[1]
            continue
        candidates: List[Tuple[int, str]] = []
        for v in ANCHOR_VCPUS:
            size = anchor_size_for(fam, v)
            if size:
                candidates.append((v, size))
                all_sizes.add(size)
        to_fetch[fam] = candidates

    if to_fetch:
        price_index = _fetch_region_size_prices(
            region, sorted(all_sizes), currency=currency, os_name=os_name,
        )
        for fam, candidates in to_fetch.items():
            resolved: Optional[dict] = None
            for vcpus, size in candidates:
                price = price_index.get(size)
                if price is not None and vcpus > 0:
                    resolved = {
                        "per_core_hour": price / vcpus,
                        "anchor_size": size,
                        "vcpus": vcpus,
                        "hourly": price,
                    }
                    break
            key = (currency, os_name, region, str(fam or "").strip().lower())
            with _LOCK:
                _PRICE_CACHE[key] = (now + _CACHE_TTL_S, resolved)
            results[fam] = resolved

    return results


def _normalize_service_estimates(raw) -> Dict[str, float]:
    """Coerce a ``{service_name: monthly_usd}`` map to clean floats >= 0."""
    out: Dict[str, float] = {}
    if not isinstance(raw, dict):
        return out
    for name, value in raw.items():
        key = str(name or "").strip()
        if not key:
            continue
        try:
            amount = float(value)
        except (TypeError, ValueError):
            continue
        if amount < 0 or amount != amount:  # negative or NaN
            continue
        out[key] = round(amount, 2)
    return out


def estimate(
    regions: List[str],
    families: List[dict],
    *,
    os_name: str = "linux",
    currency: str = DEFAULT_CURRENCY,
    hours_per_month: int = DEFAULT_HOURS_PER_MONTH,
    acd_discount_pct: float = 0.0,
    services: Optional[List[str]] = None,
    noncompute_uplift_pct: float = 0.0,
    service_estimates: Optional[Dict[str, float]] = None,
) -> Dict:
    """Estimate monthly BOM cost per region (compute + non-compute).

    ``families`` items: ``{"family": <id>, "label": <str>, "required_cores": n}``.

    Non-compute cost is estimated two ways, combined:
    - **Itemized**: ``service_estimates`` maps a BOM service name to a flat
      monthly figure the operator entered (region-agnostic).
    - **Uplift**: ``noncompute_uplift_pct`` percent of the region's compute cost,
      a catch-all for everything not itemized.

    Returns a dict with a per-region breakdown (list and ACD-net) for compute,
    non-compute, and the combined total.
    """
    os_name = "windows" if str(os_name).strip().lower() == "windows" else "linux"
    currency = (currency or DEFAULT_CURRENCY).strip().upper() or DEFAULT_CURRENCY
    try:
        hours = int(hours_per_month)
    except (TypeError, ValueError):
        hours = DEFAULT_HOURS_PER_MONTH
    if hours <= 0:
        hours = DEFAULT_HOURS_PER_MONTH
    try:
        acd = float(acd_discount_pct or 0.0)
    except (TypeError, ValueError):
        acd = 0.0
    acd = min(100.0, max(0.0, acd))
    factor = 1.0 - (acd / 100.0)
    try:
        uplift = float(noncompute_uplift_pct or 0.0)
    except (TypeError, ValueError):
        uplift = 0.0
    uplift = max(0.0, uplift)
    uplift_frac = uplift / 100.0

    svc_estimates = _normalize_service_estimates(service_estimates)
    # Only count itemized estimates for services actually in this BOM (when a
    # service list is provided); otherwise honor all supplied estimates.
    service_names = [str(s or "").strip() for s in (services or []) if str(s or "").strip()]
    if service_names:
        wanted = {s.lower() for s in service_names}
        itemized = {k: v for k, v in svc_estimates.items() if k.lower() in wanted}
    else:
        itemized = dict(svc_estimates)
    itemized_items = [
        {"service": name, "monthly_list": amount, "monthly_net": round(amount * factor, 2)}
        for name, amount in sorted(itemized.items(), key=lambda kv: kv[0].lower())
    ]
    itemized_total = round(sum(itemized.values()), 2)

    norm_families = []
    for f in families or []:
        fid = str((f or {}).get("family") or "").strip()
        cores = 0
        try:
            cores = int((f or {}).get("required_cores") or 0)
        except (TypeError, ValueError):
            cores = 0
        if not fid or cores <= 0:
            continue
        norm_families.append({
            "family": fid,
            "label": str((f or {}).get("label") or fid).strip() or fid,
            "required_cores": cores,
        })

    fam_ids = [f["family"] for f in norm_families]
    out_regions: Dict[str, dict] = {}

    # Resolve the unique region shorts up front and price them concurrently —
    # each region is an independent (cached) Retail Prices API call.
    unique_shorts = []
    seen = set()
    for region in regions or []:
        short = _norm_region(region)
        if short and short not in seen:
            seen.add(short)
            unique_shorts.append(short)

    priced_by_region: Dict[str, Dict[str, Optional[dict]]] = {}
    if unique_shorts and fam_ids:
        import concurrent.futures

        def _price_one(short: str):
            return short, price_families_in_region(
                short, fam_ids, os_name=os_name, currency=currency,
            )

        workers = min(8, len(unique_shorts))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for short, priced in pool.map(_price_one, unique_shorts):
                priced_by_region[short] = priced
    else:
        priced_by_region = {short: {} for short in unique_shorts}

    for short in unique_shorts:
        priced = priced_by_region.get(short, {})
        fam_rows: List[dict] = []
        month_list = 0.0
        priced_any = False
        any_unpriced = False
        for f in norm_families:
            info = priced.get(f["family"])
            if not info:
                any_unpriced = True
                fam_rows.append({
                    "family": f["family"],
                    "label": f["label"],
                    "required_cores": f["required_cores"],
                    "priced": False,
                })
                continue
            hourly = info["per_core_hour"] * f["required_cores"]
            m_list = hourly * hours
            fam_rows.append({
                "family": f["family"],
                "label": f["label"],
                "required_cores": f["required_cores"],
                "anchor_size": info["anchor_size"],
                "per_core_hour": round(info["per_core_hour"], 5),
                "hourly_list": round(hourly, 4),
                "monthly_list": round(m_list, 2),
                "monthly_net": round(m_list * factor, 2),
                "priced": True,
            })
            month_list += m_list
            priced_any = True
        out_regions[short] = {
            "compute": {
                "monthly_list": round(month_list, 2),
                "monthly_net": round(month_list * factor, 2),
                "hourly_list": round(month_list / hours, 4) if hours else 0.0,
                "families": fam_rows,
                "priced_any": priced_any,
                "complete": priced_any and not any_unpriced,
            },
            "noncompute": {
                "itemized_total_list": itemized_total,
                "itemized_total_net": round(itemized_total * factor, 2),
                "items": itemized_items,
                "uplift_pct": uplift,
                "uplift_list": round(month_list * uplift_frac, 2),
                "uplift_net": round(month_list * uplift_frac * factor, 2),
                "monthly_list": round(itemized_total + month_list * uplift_frac, 2),
                "monthly_net": round((itemized_total + month_list * uplift_frac) * factor, 2),
            },
            # Convenience roll-ups for the whole BOM (compute + non-compute).
            "monthly_list": round(month_list + itemized_total + month_list * uplift_frac, 2),
            "monthly_net": round(
                (month_list + itemized_total + month_list * uplift_frac) * factor, 2
            ),
            "priced_any": priced_any,
            "complete": priced_any and not any_unpriced,
        }

    return {
        "currency": currency,
        "os": os_name,
        "hours_per_month": hours,
        "acd_discount_pct": acd,
        "noncompute_uplift_pct": uplift,
        "itemized_service_total": itemized_total,
        "regions": out_regions,
        "estimate_only": True,
    }

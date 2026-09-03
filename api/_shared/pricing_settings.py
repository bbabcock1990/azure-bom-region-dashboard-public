"""Pricing/estimate settings — persisted locally in SQLite.

Controls how the BOM cost estimate is computed and displayed:

- ``acd_discount_pct``      Azure Consumption Discount applied on top of list price.
- ``pricing_os``           "linux" | "windows" — which on-demand meter to price.
- ``hours_per_month``      Monthly hour basis (730 = 24*365/12).
- ``currency``             ISO currency code for the Retail Prices API.
- ``noncompute_uplift_pct``Catch-all % of compute used to approximate the
                           non-compute portion of the BOM (storage, network, …).
- ``service_estimates``    Optional per-service flat monthly figures (JSON map
                           ``{service_name: monthly}``) the operator itemizes.

All figures are estimates; the UI labels them as such. Reads never raise — a
missing/garbled record yields the built-in defaults.

Storage: a single entity in the ``pricingsettings`` table
(``PartitionKey="settings"``, ``RowKey="current"``) via the local storage shim.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from . import storage

log = logging.getLogger(__name__)

TABLE_NAME = "pricingsettings"
_PK = "settings"
_RK = "current"

VALID_OS = ("linux", "windows")

# Scalar fields persisted as columns.
_FIELDS = (
    "acd_discount_pct",
    "pricing_os",
    "hours_per_month",
    "currency",
    "noncompute_uplift_pct",
    "suggest_alternatives",
    "alt_min_savings_pct",
    "allow_older_generation",
)

DEFAULTS: Dict[str, Any] = {
    "acd_discount_pct": 0.0,
    "pricing_os": "linux",
    "hours_per_month": 730,
    "currency": "USD",
    "noncompute_uplift_pct": 35.0,
    "suggest_alternatives": True,
    "alt_min_savings_pct": 5.0,
    "allow_older_generation": False,
    "service_estimates": {},  # {service_name: monthly_usd}
}


def _clean_os(value: Any) -> str:
    os_name = str(value or "").strip().lower()
    return os_name if os_name in VALID_OS else "linux"


def _clean_pct(value: Any, default: float) -> float:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return default
    if pct != pct:  # NaN
        return default
    return min(100.0, max(0.0, pct))


def _clean_hours(value: Any) -> int:
    try:
        hours = int(float(value))
    except (TypeError, ValueError):
        return 730
    return hours if hours > 0 else 730


def _clean_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    s = str(value).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return default


def _clean_currency(value: Any) -> str:
    cur = str(value or "").strip().upper()
    return cur if cur.isalpha() and len(cur) == 3 else "USD"


def _clean_service_estimates(raw: Any) -> Dict[str, float]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, float] = {}
    for name, value in raw.items():
        key = str(name or "").strip()
        if not key:
            continue
        try:
            amount = float(value)
        except (TypeError, ValueError):
            continue
        if amount < 0 or amount != amount:
            continue
        out[key] = round(amount, 2)
    return out


def get_settings() -> Dict[str, Any]:
    """Return saved pricing settings merged over defaults. Never raises."""
    merged = dict(DEFAULTS)
    merged["service_estimates"] = {}
    try:
        table = storage.get_table_client(TABLE_NAME)
        entity = table.get_entity(_PK, _RK)
    except Exception:
        return merged
    for key in _FIELDS:
        if key in entity and entity[key] is not None:
            merged[key] = entity[key]
    if "service_estimates_json" in entity:
        merged["service_estimates"] = _clean_service_estimates(entity["service_estimates_json"])
    merged["acd_discount_pct"] = _clean_pct(merged.get("acd_discount_pct"), 0.0)
    merged["noncompute_uplift_pct"] = _clean_pct(merged.get("noncompute_uplift_pct"), 35.0)
    merged["pricing_os"] = _clean_os(merged.get("pricing_os"))
    merged["hours_per_month"] = _clean_hours(merged.get("hours_per_month"))
    merged["currency"] = _clean_currency(merged.get("currency"))
    merged["suggest_alternatives"] = _clean_bool(merged.get("suggest_alternatives"), True)
    merged["alt_min_savings_pct"] = _clean_pct(merged.get("alt_min_savings_pct"), 5.0)
    merged["allow_older_generation"] = _clean_bool(merged.get("allow_older_generation"), False)
    return merged


def save_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Upsert pricing settings from ``patch`` (unknown keys ignored)."""
    entity: Dict[str, Any] = {"PartitionKey": _PK, "RowKey": _RK}
    if "acd_discount_pct" in patch:
        entity["acd_discount_pct"] = _clean_pct(patch.get("acd_discount_pct"), 0.0)
    if "noncompute_uplift_pct" in patch:
        entity["noncompute_uplift_pct"] = _clean_pct(patch.get("noncompute_uplift_pct"), 35.0)
    if "pricing_os" in patch:
        entity["pricing_os"] = _clean_os(patch.get("pricing_os"))
    if "hours_per_month" in patch:
        entity["hours_per_month"] = _clean_hours(patch.get("hours_per_month"))
    if "currency" in patch:
        entity["currency"] = _clean_currency(patch.get("currency"))
    if "suggest_alternatives" in patch:
        entity["suggest_alternatives"] = _clean_bool(patch.get("suggest_alternatives"), True)
    if "alt_min_savings_pct" in patch:
        entity["alt_min_savings_pct"] = _clean_pct(patch.get("alt_min_savings_pct"), 5.0)
    if "allow_older_generation" in patch:
        entity["allow_older_generation"] = _clean_bool(patch.get("allow_older_generation"), False)
    if "service_estimates" in patch:
        cleaned = _clean_service_estimates(patch.get("service_estimates"))
        entity["service_estimates_json"] = json.dumps(cleaned, ensure_ascii=False)
    table = storage.get_table_client(TABLE_NAME)
    table.upsert_entity(entity, mode="merge")
    return get_settings()

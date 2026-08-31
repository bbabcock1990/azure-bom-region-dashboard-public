"""Helpers for the region catalog used by the in-app BOM editor.

The catalog is a static JSON seed (``data/bom_region_catalog.json``)
optionally extended with user-added custom regions (persisted in
Azurite via ``bom_catalog``). Reads always merge built-in + custom and
sort by display name for a stable UI.

Each merged entry is the JSON dict:

    {
        "name":        "eastus",
        "display_name": "East US",
        "has_az":      true,
        "is_custom":   false
    }
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Dict, List, Optional

from . import bom_catalog

log = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_CATALOG_PATH = os.path.join(_DATA_DIR, "bom_region_catalog.json")

_LOCK = threading.Lock()
_BUILTIN_CACHE: Optional[List[Dict]] = None


def reset_dataset_caches() -> None:
    """Drop the memoized built-in region catalog so a freshly uploaded
    override is picked up without a restart."""
    global _BUILTIN_CACHE
    with _LOCK:
        _BUILTIN_CACHE = None


def _load_builtin() -> List[Dict]:
    """Read + cache the built-in JSON seed. Cached for the process
    lifetime; restart the function host to pick up edits."""
    global _BUILTIN_CACHE
    with _LOCK:
        if _BUILTIN_CACHE is not None:
            return [dict(r) for r in _BUILTIN_CACHE]
    from . import dataset_store
    catalog_path = dataset_store.resolve_path("region_catalog")
    if not os.path.exists(catalog_path):
        log.warning("bom_regions: seed file missing at %s — empty built-in catalog",
                    catalog_path)
        with _LOCK:
            _BUILTIN_CACHE = []
        return []
    try:
        with open(catalog_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        log.exception("bom_regions: failed to parse %s", catalog_path)
        with _LOCK:
            _BUILTIN_CACHE = []
        return []
    items = data.get("regions") or []
    out: List[Dict] = []
    seen: set = set()
    for raw in items:
        if not isinstance(raw, dict):
            continue
        name = (raw.get("name") or "").strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({
            "name": name,
            "display_name": (raw.get("display_name") or name).strip(),
            "has_az": bool(raw.get("has_az", False)),
            "is_custom": False,
        })
    with _LOCK:
        _BUILTIN_CACHE = [dict(r) for r in out]
    return out


def load_merged_catalog() -> List[Dict]:
    """Return all known regions (built-in first, then custom), de-duped
    on ``name`` with the custom overlay winning (so a user-defined
    region that happens to share a short name overrides the seed)."""
    builtin = _load_builtin()
    by_name: Dict[str, Dict] = {r["name"]: dict(r) for r in builtin}
    for c in bom_catalog.list_custom("region"):
        # Tolerate corrupt persisted records by re-validating.
        try:
            v = bom_catalog.validate_region(c)
        except bom_catalog.BomCatalogError:
            log.warning("bom_regions: dropping invalid custom region %r", c)
            continue
        by_name[v["name"]] = {**v, "is_custom": True}
    out = list(by_name.values())
    out.sort(key=lambda r: (r.get("display_name") or r.get("name") or "").lower())
    return out


def list_builtin_names() -> List[str]:
    return [r["name"] for r in _load_builtin()]


def display_map() -> Dict[str, str]:
    """Short-name → display-name lookup for use by other modules
    (e.g. ``bom_services.build_region_specs`` extensions)."""
    return {r["name"]: r["display_name"] for r in load_merged_catalog()}


def validate_region_names(names: List[str]) -> List[str]:
    """Filter + validate a user-supplied list of region short names
    against the merged catalog. Raises if any are unknown."""
    cat = {r["name"] for r in load_merged_catalog()}
    cleaned: List[str] = []
    seen: set = set()
    unknown: List[str] = []
    for raw in names or []:
        if raw is None:
            continue
        s = str(raw).strip().lower()
        if not s or s in seen:
            continue
        seen.add(s)
        if s not in cat:
            unknown.append(s)
            continue
        cleaned.append(s)
    if unknown:
        raise bom_catalog.BomCatalogError(
            "unknown_regions",
            ("Unknown region(s): "
             f"{', '.join(unknown[:10])}. Add them via the region picker "
             "first."),
            400,
        )
    return cleaned

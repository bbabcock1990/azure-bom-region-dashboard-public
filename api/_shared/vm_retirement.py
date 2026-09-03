"""Azure VM size-series lifecycle (retirement / previous-gen) lookup.

Microsoft does **not** publish an official API that returns a VM size's
retirement status and dates. The authoritative source is two Microsoft Learn
articles:

  * Retired Azure VM size series
    https://learn.microsoft.com/azure/virtual-machines/sizes/lifecycle/retired-sizes-list
  * Previous generation Azure VM size series
    https://learn.microsoft.com/azure/virtual-machines/sizes/lifecycle/previous-gen-sizes-list

We ship a curated JSON snapshot of those tables (``data/vm_retirements.json``)
and expose a fast core-form lookup. Keeping it as bundled data (rather than a
live scrape) means the check is deterministic, testable, and works in customer
environments where outbound calls to learn.microsoft.com may be blocked. Refresh
the snapshot by re-reading the two pages and updating the JSON.

The dashboard uses this to make sure the cheaper size-equivalent recommendations
never steer a customer onto a series that is retired or announced for retirement
(and to flag previous-gen series).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Dict, Optional

log = logging.getLogger(__name__)

_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "vm_retirements.json")

_lock = threading.Lock()
_index: Optional[Dict[str, dict]] = None
_meta: Dict[str, str] = {}


def _core_key(family_or_core: str) -> str:
    """Normalize a family id or core-form label to a lowercase core key.

    ``standardFsv2Family`` -> ``fsv2``; ``Fsv2`` -> ``fsv2``.
    """
    s = (family_or_core or "").strip()
    m = re.match(r"^standard(.+?)family$", s, re.IGNORECASE)
    core = m.group(1) if m else s
    return core.strip().lower()


def _blocks(status: str, sub_status: str) -> bool:
    """Should a series with this status be excluded from recommendations?

    Retired and announced-for-retirement series are always excluded. Previous-gen
    series are excluded only when their capacity is limited; ``next_gen_available``
    previous-gen series stay eligible (they are cheaper and still fully supported)
    but are flagged so the UI can note a newer generation exists.
    """
    if status in ("retired", "announced"):
        return True
    if status == "previous_gen" and sub_status == "capacity_limited":
        return True
    return False


def _load() -> Dict[str, dict]:
    global _index, _meta
    if _index is not None:
        return _index
    with _lock:
        if _index is not None:
            return _index
        index: Dict[str, dict] = {}
        try:
            with open(_DATA_PATH, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as ex:  # pragma: no cover - defensive
            log.warning("vm_retirement: could not load %s: %r", _DATA_PATH, ex)
            _index = index
            return _index
        _meta = {
            "snapshot_date": str(doc.get("snapshot_date") or ""),
            "source_retired": str(doc.get("source_retired") or ""),
            "source_previous_gen": str(doc.get("source_previous_gen") or ""),
        }
        for rec in doc.get("records") or []:
            status = str(rec.get("status") or "").strip().lower()
            sub_status = str(rec.get("sub_status") or "").strip().lower()
            entry = {
                "series": rec.get("series") or "",
                "status": status,
                "sub_status": sub_status,
                "planned_retirement_date": rec.get("planned_retirement_date") or "",
                "announced": rec.get("announced") or "",
                "replacement": rec.get("replacement") or "",
                "migration_url": rec.get("migration_url") or "",
                "class": rec.get("class") or "",
                "blocks_recommendation": _blocks(status, sub_status),
            }
            for core in rec.get("cores") or []:
                key = str(core or "").strip().lower()
                if key:
                    index[key] = entry
        _index = index
        return _index


def status_for_core(family_or_core: str) -> Optional[dict]:
    """Return the lifecycle record for a family/core, or ``None`` if current.

    The returned dict includes ``status`` (retired|announced|previous_gen),
    ``sub_status``, ``planned_retirement_date``, ``replacement``,
    ``migration_url`` and a derived ``blocks_recommendation`` bool.
    """
    return _load().get(_core_key(family_or_core))


def blocks_recommendation(family_or_core: str) -> bool:
    """True when this series must not be offered as a recommendation."""
    rec = status_for_core(family_or_core)
    return bool(rec and rec.get("blocks_recommendation"))


def short_note(family_or_core: str) -> str:
    """A compact human note for a flagged (but still eligible) series, else ""."""
    rec = status_for_core(family_or_core)
    if not rec:
        return ""
    status = rec.get("status")
    if status == "retired":
        return "Retired — no longer available"
    if status == "announced":
        date = rec.get("planned_retirement_date") or ""
        return f"Retiring {date}".strip()
    if status == "previous_gen":
        if rec.get("sub_status") == "capacity_limited":
            return "Previous-gen — capacity limited"
        return "Previous-gen"
    return ""


def meta() -> Dict[str, str]:
    """Snapshot provenance (date + source URLs) for display/debugging."""
    _load()
    return dict(_meta)


def reset_cache() -> None:
    """Test hook: drop the in-memory index so the next call reloads the file."""
    global _index
    with _lock:
        _index = None

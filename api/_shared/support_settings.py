"""
Support-contact settings — persisted locally in SQLite.

Azure support tickets require caller contact details (name, email, country,
severity, …). Rather than prompt for them on every ticket, the dashboard keeps
a single editable settings record the user fills in once under
**Settings → Support**. Ticket creation reads these values as defaults and the
user can still override any field before submitting.

Storage: a single entity in the ``supportsettings`` table
(``PartitionKey="settings"``, ``RowKey="current"``) via the local storage shim.

Everything here is best-effort and never raises on read: a missing/garbled
record simply yields the built-in defaults so the UI always has something to
show.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

from . import storage

log = logging.getLogger(__name__)

TABLE_NAME = "supportsettings"
_PK = "settings"
_RK = "current"

# Azure Support accepts exactly these severities. "moderate" is our default per
# the product decision (Sev B).
VALID_SEVERITIES = ("minimal", "moderate", "critical")

# Fields we persist. Keep this list authoritative — anything not here is dropped
# on save so stray keys never reach the ARM payload.
_FIELDS = (
    "contact_first_name",
    "contact_last_name",
    "primary_email",
    "additional_emails",   # comma-separated CC list (optional)
    "phone",
    "country",             # ISO country code, e.g. "US"
    "preferred_timezone",  # e.g. "Pacific Standard Time"
    "preferred_contact_method",  # "email" | "phone"
    "preferred_language",  # e.g. "en-us"
    "default_severity",    # one of VALID_SEVERITIES
    "validation_resource_group",   # legacy single/global RG (fallback only)
    "validation_resource_groups",  # JSON map {subscription_id: rg_name} — per-subscription
)

DEFAULTS: Dict[str, Any] = {
    "contact_first_name": "",
    "contact_last_name": "",
    "primary_email": "",
    "additional_emails": "",
    "phone": "",
    "country": "US",
    "preferred_timezone": "Pacific Standard Time",
    "preferred_contact_method": "email",
    "preferred_language": "en-us",
    "default_severity": "moderate",
    "validation_resource_group": "",
    "validation_resource_groups": {},
}


def _clean_severity(value: Any) -> str:
    sev = str(value or "").strip().lower()
    return sev if sev in VALID_SEVERITIES else "moderate"


def get_settings() -> Dict[str, Any]:
    """Return the saved support settings merged over built-in defaults.

    Never raises: a missing table/record or bad JSON yields ``DEFAULTS``.
    """
    merged = dict(DEFAULTS)
    try:
        table = storage.get_table_client(TABLE_NAME)
        entity = table.get_entity(_PK, _RK)
    except Exception:
        return merged
    for key in _FIELDS:
        if key in entity and entity[key] is not None:
            merged[key] = entity[key]
    # The per-subscription RG map is persisted as a JSON string; hydrate it back
    # into a dict so callers always get an object.
    vrgs = merged.get("validation_resource_groups")
    if isinstance(vrgs, str):
        try:
            merged["validation_resource_groups"] = json.loads(vrgs) if vrgs.strip() else {}
        except Exception:
            merged["validation_resource_groups"] = {}
    if not isinstance(merged.get("validation_resource_groups"), dict):
        merged["validation_resource_groups"] = {}
    merged["default_severity"] = _clean_severity(merged.get("default_severity"))
    return merged


def save_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    """Upsert the support settings from ``patch`` (unknown keys ignored).

    Returns the full, post-merge settings dict.
    """
    entity: Dict[str, Any] = {"PartitionKey": _PK, "RowKey": _RK}
    for key in _FIELDS:
        if key in patch and patch[key] is not None:
            value = patch[key]
            if key == "default_severity":
                value = _clean_severity(value)
            elif key == "validation_resource_groups":
                # Merge into the existing map (don't clobber other subscriptions).
                # An empty/blank value for a subscription removes its entry.
                current = get_settings().get("validation_resource_groups") or {}
                incoming = value if isinstance(value, dict) else {}
                for sub, rg in incoming.items():
                    sub = str(sub).strip()
                    if not sub:
                        continue
                    rg = str(rg or "").strip()
                    if rg:
                        current[sub] = rg
                    else:
                        current.pop(sub, None)
                value = json.dumps(current)
            elif isinstance(value, str):
                value = value.strip()
            entity[key] = value
    table = storage.get_table_client(TABLE_NAME)
    table.upsert_entity(entity, mode="merge")
    return get_settings()


def resolve_validation_rg(subscription_id: str) -> str:
    """Return the validation RG to use for ``subscription_id``.

    Resolution order: the per-subscription map entry, then the legacy global
    ``validation_resource_group`` as a fallback, else "" (read-only checks).
    An RG only exists inside one subscription, so this is intentionally scoped.
    """
    s = get_settings()
    sub = str(subscription_id or "").strip()
    per_sub = s.get("validation_resource_groups") or {}
    if sub and isinstance(per_sub, dict) and per_sub.get(sub):
        return str(per_sub[sub]).strip()
    return str(s.get("validation_resource_group") or "").strip()


def is_configured() -> bool:
    """True when the minimum fields needed to file a ticket are present."""
    s = get_settings()
    return bool(s.get("primary_email") and s.get("contact_first_name"))

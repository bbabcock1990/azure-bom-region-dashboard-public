"""
CRUD wrapper around the ``subscriptionmetadata`` Azure Table.

One row per **BOM**. A BOM has its own identity (``bom_id``) that is
independent of the subscription it targets, so a single subscription can
own multiple distinct BOMs (e.g. two workloads in one Azure sub). Stores
everything the in-app BOM editor needs: human-friendly tag, customer name +
segments, required SKU families, required services. Designed so the entire
BOM round-trips as JSON via the ``/api/subscription_metadata`` endpoints.

Schema (Table Storage):
    PartitionKey         = "sub"  (fixed — single-tenant deployment)
    RowKey               = bom_id (opaque unique id; legacy rows: the sub GUID)
    subscription_id      : str   (lowercase GUID the BOM targets; primary/first)
    subscription_ids_json: str   (JSON-encoded list of lowercase GUIDs)
    tag                  : str   (optional human label, e.g. "Avaya Prod East")
    customer_name        : str   (optional)
    customer_segments    : str   (CSV, default "EA,ANY")
    required_skus_json   : str   (JSON-encoded list of family dicts)
    services_json        : str   (JSON-encoded list of {name})
    bom_updated_at       : str   (ISO timestamp, UTC)
    bom_updated_by       : str   (local username)

Backward compatibility: rows written before BOMs were decoupled used
``RowKey == subscription_id`` and have no ``subscription_id`` column. Such
rows are read as ``bom_id == RowKey`` with ``subscription_id`` falling back
to the RowKey, so they keep working unchanged.

All persistence happens through :func:`get` / :func:`upsert` / :func:`delete`
which validate inputs before any I/O so the storage layer never holds
garbage. Validation is shared with the legacy xlsx-upload path via
:mod:`compile`'s required-family validators so the schema is enforced
identically.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from . import compile as compile_mod
from . import bom_services
from . import bom_regions
from . import storage

log = logging.getLogger(__name__)

TABLE_NAME = "subscriptionmetadata"
PARTITION = "sub"

# Caps — defensive limits so the editor can't produce a payload that
# blows past Azure Table's 32 KB string-property ceiling.
MAX_TAG_LEN = 80
MAX_CUSTOMER_NAME_LEN = 80
MAX_SEGMENTS_LEN = 200
MAX_SKU_ROWS = 200
MAX_SERVICES = 200
MAX_REGIONS = 200
MAX_JSON_BYTES = 30_000  # leave headroom under Table Storage's 32 KB limit
# Lowercase GUID matches the canonical form we already use as RowKey
# elsewhere (runs table). The frontend always sends GUID strings here.
GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# A bom_id is an opaque key. Generated ones are 32-char uuid hex; legacy
# ones are the subscription GUID (36 chars incl. dashes). Accept both shapes
# plus our own canonical form so validation never rejects a real key.
BOM_ID_RE = re.compile(r"^[0-9a-f]{32}$|^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Tag allows a friendly mix of letters, digits, spaces, dashes, dots,
# underscores, parens. Reject control chars / quotes so we never have
# to escape the tag in the sub picker UI.
_TAG_RE = re.compile(r"^[A-Za-z0-9 _\-\.\(\)\[\]]+$")


class BomStorageError(Exception):
    """Stable error code surfaced to callers (mirrors compile.CompileError)."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_bom_id() -> str:
    """Opaque, collision-resistant id for a freshly created BOM."""
    return uuid.uuid4().hex


def _validate_bom_id(bom_id: str) -> str:
    s = (bom_id or "").strip().lower()
    if not BOM_ID_RE.match(s):
        raise BomStorageError("bad_bom_id", "bom_id is not a valid id.", 400)
    return s


def _validate_sub_id(sub_id: str) -> str:
    s = (sub_id or "").strip().lower()
    if not GUID_RE.match(s):
        raise BomStorageError(
            "bad_subscription", "subscription_id must be a GUID.", 400,
        )
    return s


def _normalize_subscription_ids(
    subscription_id: Optional[str],
    subscription_ids: Optional[List[str]] = None,
) -> List[str]:
    """Return a deduped, validated list of subscription GUIDs.

    ``subscription_ids`` is the new canonical representation. Legacy callers may
    still pass only ``subscription_id``. When both are supplied, the list wins
    and its first element becomes the primary ``subscription_id`` persisted for
    backward compatibility.
    """
    out: List[str] = []
    if subscription_ids is not None:
        if not isinstance(subscription_ids, list):
            raise BomStorageError(
                "bad_subscription",
                "subscription_ids must be a JSON array of GUIDs.",
                400,
            )
        for i, item in enumerate(subscription_ids):
            if not isinstance(item, str):
                raise BomStorageError(
                    "bad_subscription",
                    f"subscription_ids[{i}] must be a GUID string.",
                    400,
                )
            sid = _validate_sub_id(item)
            if sid not in out:
                out.append(sid)
    if not out and subscription_id:
        out.append(_validate_sub_id(subscription_id))
    if not out:
        raise BomStorageError(
            "bad_subscription", "subscription_id must be a GUID.", 400,
        )
    return out


def _validate_tag(tag: Optional[str]) -> Optional[str]:
    if tag is None:
        return None
    s = str(tag).strip()
    if not s:
        return None
    if len(s) > MAX_TAG_LEN:
        raise BomStorageError(
            "bad_tag",
            f"tag is too long ({len(s)} chars; max {MAX_TAG_LEN}).",
            400,
        )
    if not _TAG_RE.match(s):
        raise BomStorageError(
            "bad_tag",
            ("tag may contain only letters, digits, spaces, and "
             "the punctuation . _ - ( ) [ ]."),
            400,
        )
    return s


def _validate_customer_name(name: Optional[str]) -> Optional[str]:
    if name is None:
        return None
    s = str(name).strip()
    if not s:
        return None
    if len(s) > MAX_CUSTOMER_NAME_LEN:
        raise BomStorageError(
            "bad_customer_name",
            f"customer_name is too long ({len(s)} chars; max {MAX_CUSTOMER_NAME_LEN}).",
            400,
        )
    return s


# Per-BOM support-contact override. Mirrors the global support_settings fields
# so each BOM can carry its own ticket owner + contact profile, initialized from
# the global defaults but independently editable. Stored as a compact JSON blob.
_SUPPORT_OVERRIDE_FIELDS = (
    "contact_first_name",
    "contact_last_name",
    "primary_email",
    "additional_emails",
    "phone",
    "country",
    "preferred_timezone",
    "preferred_contact_method",
    "preferred_language",
    "default_severity",
)
_VALID_SEVERITIES = ("minimal", "moderate", "critical")
MAX_SUPPORT_FIELD_LEN = 200


def _validate_support_override(obj) -> Dict[str, str]:
    """Return a cleaned per-BOM support override dict.

    Only known fields are kept (unknown keys dropped), every value is coerced to
    a trimmed string, severity is constrained to the accepted set, and empty
    fields are omitted so the blob only carries genuine overrides. Non-dict
    input yields an empty override (BOM simply inherits the global profile).
    """
    if not isinstance(obj, dict):
        return {}
    cleaned: Dict[str, str] = {}
    for key in _SUPPORT_OVERRIDE_FIELDS:
        if key not in obj or obj[key] is None:
            continue
        val = str(obj[key]).strip()
        if not val:
            continue
        if key == "default_severity":
            val = val.lower()
            if val not in _VALID_SEVERITIES:
                continue
        if len(val) > MAX_SUPPORT_FIELD_LEN:
            raise BomStorageError(
                "bad_support_override",
                f"support_override.{key} is too long ({len(val)} chars; "
                f"max {MAX_SUPPORT_FIELD_LEN}).",
                400,
            )
        cleaned[key] = val
    return cleaned


def _validate_segments(segments_csv: Optional[str]) -> str:
    """Returns a normalized uppercase CSV. Empty / None becomes the
    default "EA,ANY"."""
    if not segments_csv:
        return "EA,ANY"
    s = str(segments_csv).strip()
    if len(s) > MAX_SEGMENTS_LEN:
        raise BomStorageError(
            "bad_segments",
            f"customer_segments is too long ({len(s)} chars; max {MAX_SEGMENTS_LEN}).",
            400,
        )
    parts = [p.strip().upper() for p in s.split(",") if p.strip()]
    if not parts:
        return "EA,ANY"
    # Defensive whitelist — only these segment codes are recognized.
    allowed = {"EA", "ANY", "MOSP", "INTERNAL"}
    bad = [p for p in parts if p not in allowed]
    if bad:
        raise BomStorageError(
            "bad_segments",
            (f"customer_segments contains unknown segment(s): "
             f"{','.join(bad)}. Allowed: {','.join(sorted(allowed))}."),
            400,
        )
    return ",".join(parts)


def _validate_required_skus(items: List[Dict]) -> List[Dict]:
    """Normalize + validate via the same machinery the legacy xlsx path uses.

    Mutates each dict in-place to fill in derived labels and cores, then
    runs _validate_required_families which raises CompileError on bad
    input. We translate that into a BomStorageError so callers don't have
    to know about both exception types.
    """
    if not isinstance(items, list):
        raise BomStorageError(
            "bad_required_skus", "required_skus must be a JSON array.", 400,
        )
    if len(items) > MAX_SKU_ROWS:
        raise BomStorageError(
            "bad_required_skus",
            f"Too many required SKU rows ({len(items)}; max {MAX_SKU_ROWS}).",
            400,
        )
    # An empty list is allowed at the storage layer — the run-time check
    # in compile_snapshot will reject a run with 0 families. This lets a
    # user save a partial BOM while iterating.
    cleaned: List[Dict] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise BomStorageError(
                "bad_required_skus",
                f"required_skus[{i}] is not an object.",
                400,
            )
        cleaned.append({
            "primary_family": (item.get("primary_family") or "").strip(),
            "primary_label": (item.get("primary_label") or "").strip() or None,
            "alt_family": (item.get("alt_family") or "").strip() or None,
            "alt_label": (item.get("alt_label") or "").strip() or None,
            "required_cores": item.get("required_cores"),
        })
    if cleaned:
        try:
            compile_mod._validate_required_families(cleaned)
        except compile_mod.CompileError as ex:
            raise BomStorageError(ex.code, ex.message, ex.status)
    return cleaned


def _validate_services(items: List[Dict]) -> List[Dict]:
    """Validate a saved services list. Each entry must be ``{"name": str}``
    where ``name`` exists in the static catalog. Order is preserved."""
    if not isinstance(items, list):
        raise BomStorageError(
            "bad_services", "services must be a JSON array.", 400,
        )
    if len(items) > MAX_SERVICES:
        raise BomStorageError(
            "bad_services",
            f"Too many services ({len(items)}; max {MAX_SERVICES}).",
            400,
        )
    names: List[str] = []
    seen: set = set()
    for i, item in enumerate(items):
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            raise BomStorageError(
                "bad_services",
                f"services[{i}] must be a string or object with 'name'.",
                400,
            )
        if not name:
            continue
        if name in seen:
            # Drop silently — duplicates are common when users edit-paste.
            continue
        seen.add(name)
        names.append(name)
    if not names:
        return []
    try:
        resolved = bom_services.resolve_services(names)
    except bom_services.BomServicesError as ex:
        raise BomStorageError(ex.code, ex.message, ex.status)
    # Preserve the user's order; emit minimal records (just name) since
    # provider/resource_type/zone_check are looked up from the catalog
    # at every run anyway.
    return [{"name": s["name"]} for s in resolved]


def _validate_regions(items) -> List[str]:
    """Validate a saved regions list. Empty / None means "use defaults"
    (the runner will fall back to the default region list). Each
    entry must be a short-name string (or ``{"name": str}``) present
    in the merged region catalog so we fail loud on typos."""
    if items is None:
        return []
    if not isinstance(items, list):
        raise BomStorageError(
            "bad_regions", "regions must be a JSON array.", 400,
        )
    if len(items) > MAX_REGIONS:
        raise BomStorageError(
            "bad_regions",
            f"Too many regions ({len(items)}; max {MAX_REGIONS}).",
            400,
        )
    names: List[str] = []
    for i, item in enumerate(items):
        if isinstance(item, str):
            names.append(item.strip())
        elif isinstance(item, dict):
            names.append(str(item.get("name") or "").strip())
        else:
            raise BomStorageError(
                "bad_regions",
                f"regions[{i}] must be a string or object with 'name'.",
                400,
            )
    try:
        cleaned = bom_regions.validate_region_names(names)
    # bom_regions raises bom_catalog.BomCatalogError on unknown names; we
    # translate to BomStorageError so callers only have to handle one type.
    except Exception as ex:
        code = getattr(ex, "code", "bad_regions")
        message = getattr(ex, "message", str(ex))
        status = getattr(ex, "status", 400)
        raise BomStorageError(code, message, status)
    return cleaned


def _ensure_json_size(serialized: str, *, what: str) -> None:
    n = len(serialized.encode("utf-8"))
    if n > MAX_JSON_BYTES:
        raise BomStorageError(
            "payload_too_large",
            f"{what} JSON is too large ({n} bytes; max {MAX_JSON_BYTES}). "
            "Reduce the number of entries or shorten labels.",
            413,
        )


def _entity_to_record(e: Dict) -> Dict:
    """Convert a Table entity (flat dict with string fields) into the
    JSON shape we expose over the API."""
    try:
        required_skus = json.loads(e.get("required_skus_json") or "[]")
    except Exception:
        log.warning("subscription_metadata: required_skus_json corrupt for %s",
                    e.get("RowKey"))
        required_skus = []
    try:
        services = json.loads(e.get("services_json") or "[]")
    except Exception:
        log.warning("subscription_metadata: services_json corrupt for %s",
                    e.get("RowKey"))
        services = []
    try:
        regions = json.loads(e.get("regions_json") or "[]")
        if not isinstance(regions, list):
            regions = []
    except Exception:
        log.warning("subscription_metadata: regions_json corrupt for %s",
                    e.get("RowKey"))
        regions = []
    try:
        subscription_ids = json.loads(e.get("subscription_ids_json") or "[]")
        if not isinstance(subscription_ids, list):
            subscription_ids = []
    except Exception:
        log.warning("subscription_metadata: subscription_ids_json corrupt for %s",
                    e.get("RowKey"))
        subscription_ids = []
    if not subscription_ids:
        primary_sub = e.get("subscription_id") or e.get("RowKey")
        subscription_ids = [primary_sub] if primary_sub else []
    try:
        support_override = json.loads(e.get("support_override_json") or "{}")
        if not isinstance(support_override, dict):
            support_override = {}
    except Exception:
        log.warning("subscription_metadata: support_override_json corrupt for %s",
                    e.get("RowKey"))
        support_override = {}
    return {
        "bom_id": e.get("RowKey"),
        "subscription_id": subscription_ids[0] if subscription_ids else (e.get("subscription_id") or e.get("RowKey")),
        "subscription_ids": subscription_ids,
        "tag": e.get("tag") or None,
        "customer_name": e.get("customer_name") or None,
        "customer_segments": e.get("customer_segments") or "EA,ANY",
        "required_skus": required_skus,
        "services": services,
        "regions": regions,
        "support_override": support_override,
        "bom_updated_at": e.get("bom_updated_at") or None,
        "bom_updated_by": e.get("bom_updated_by") or None,
    }


# ─── Public API ──────────────────────────────────────────────────────────────

def list_all() -> List[Dict]:
    """Return every saved BOM record."""
    table = storage.get_table_client(TABLE_NAME)
    out: List[Dict] = []
    try:
        for e in table.query_entities(f"PartitionKey eq '{PARTITION}'"):
            out.append(_entity_to_record(e))
    except Exception:
        log.exception("subscription_metadata list failed")
        raise
    out.sort(key=lambda r: (r.get("tag") or r.get("customer_name")
                            or r.get("subscription_id") or "").lower())
    return out


def get(bom_id: str) -> Optional[Dict]:
    """Fetch a single BOM by its id. Returns None only when the row is absent;
    any other storage error propagates so real failures surface."""
    bom_id = _validate_bom_id(bom_id)
    table = storage.get_table_client(TABLE_NAME)
    try:
        e = table.get_entity(partition_key=PARTITION, row_key=bom_id)
    except KeyError:
        return None
    return _entity_to_record(e)


def create(
    subscription_id: str,
    *,
    subscription_ids: Optional[List[str]] = None,
    tag: Optional[str] = None,
    customer_name: Optional[str] = None,
    customer_segments: Optional[str] = None,
    required_skus: Optional[List[Dict]] = None,
    services: Optional[List[Dict]] = None,
    regions: Optional[List] = None,
    support_override: Optional[Dict] = None,
    updated_by: str,
) -> Dict:
    """Create a brand-new BOM with a freshly allocated bom_id. The same
    subscription may back any number of distinct BOMs."""
    return upsert(
        new_bom_id(),
        subscription_id=subscription_id,
        subscription_ids=subscription_ids,
        tag=tag,
        customer_name=customer_name,
        customer_segments=customer_segments,
        required_skus=required_skus or [],
        services=services or [],
        regions=regions or [],
        support_override=support_override,
        updated_by=updated_by,
    )


def upsert(
    bom_id: str,
    *,
    subscription_id: str,
    subscription_ids: Optional[List[str]] = None,
    tag: Optional[str],
    customer_name: Optional[str],
    customer_segments: Optional[str],
    required_skus: List[Dict],
    services: List[Dict],
    regions: Optional[List] = None,
    support_override: Optional[Dict] = None,
    updated_by: str,
) -> Dict:
    """Validate, persist, and return the saved record keyed by ``bom_id``.
    Last-write-wins for a given bom_id — acceptable for the local
    single-user app."""
    bom_id = _validate_bom_id(bom_id)
    normalized_sub_ids = _normalize_subscription_ids(subscription_id, subscription_ids)
    subscription_id = normalized_sub_ids[0]
    tag = _validate_tag(tag)
    customer_name = _validate_customer_name(customer_name)
    segments_csv = _validate_segments(customer_segments)
    cleaned_skus = _validate_required_skus(required_skus or [])
    cleaned_services = _validate_services(services or [])
    cleaned_regions = _validate_regions(regions or [])
    cleaned_override = _validate_support_override(support_override or {})

    skus_json = json.dumps(cleaned_skus, ensure_ascii=False)
    services_json = json.dumps(cleaned_services, ensure_ascii=False)
    regions_json = json.dumps(cleaned_regions, ensure_ascii=False)
    subscription_ids_json = json.dumps(normalized_sub_ids, ensure_ascii=False)
    support_override_json = json.dumps(cleaned_override, ensure_ascii=False)
    _ensure_json_size(skus_json, what="required_skus")
    _ensure_json_size(services_json, what="services")
    _ensure_json_size(regions_json, what="regions")
    _ensure_json_size(support_override_json, what="support_override")

    entity = {
        "PartitionKey": PARTITION,
        "RowKey": bom_id,
        "subscription_id": subscription_id,
        "subscription_ids_json": subscription_ids_json,
        "tag": tag or "",
        "customer_name": customer_name or "",
        "customer_segments": segments_csv,
        "required_skus_json": skus_json,
        "services_json": services_json,
        "regions_json": regions_json,
        "support_override_json": support_override_json,
        "bom_updated_at": _now_iso(),
        "bom_updated_by": (updated_by or "")[:80],
    }
    table = storage.get_table_client(TABLE_NAME)
    table.upsert_entity(entity, mode="replace")
    log.info("subscription_metadata upsert bom=%s sub=%s subs=%d skus=%d services=%d regions=%d by=%s",
             bom_id, subscription_id, len(normalized_sub_ids), len(cleaned_skus), len(cleaned_services),
             len(cleaned_regions), updated_by)
    return _entity_to_record(entity)


def delete(bom_id: str) -> bool:
    """Returns True if a row was deleted, False if the row didn't exist. Any
    other storage error propagates rather than being silently swallowed."""
    bom_id = _validate_bom_id(bom_id)
    table = storage.get_table_client(TABLE_NAME)
    try:
        table.delete_entity(partition_key=PARTITION, row_key=bom_id)
        return True
    except KeyError:
        return False

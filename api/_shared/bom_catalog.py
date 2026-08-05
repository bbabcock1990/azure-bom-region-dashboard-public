"""Custom-entry persistence for the BOM editor's region and service catalogs.

Both catalogs ship a read-only seed JSON file (``bom_region_catalog.json``,
``bom_service_catalog.json``) and a user-managed "custom" overlay that
lives in the Azurite table ``bomcustomcatalog``. Reads merge built-in +
custom; writes only touch the custom layer. Built-ins can only be
modified by editing the seed JSON and restarting the function host.

Table schema:
    PartitionKey = "region" | "service"
    RowKey       = lowercase canonical name (e.g. "eastus", "Azure Foo")
                   For services we preserve case in a separate column
                   ``display_name`` because some service names are
                   genuinely mixed-case (e.g. "Premium SSD v2"). The
                   RowKey is just a lookup key.
    payload_json = JSON-encoded record matching the catalog shape

Stored payload shapes (validated before writing):

    region:  {"name": str, "display_name": str, "has_az": bool}
    service: {"name": str, "provider": str, "resource_type": str,
              "zone_check": bool}
"""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import Dict, List, Optional

from . import storage

log = logging.getLogger(__name__)

TABLE_NAME = "bomcustomcatalog"

# Limits — defensive caps so the editor can't grow the table unbounded.
MAX_REGION_NAME_LEN = 60
MAX_REGION_DISPLAY_LEN = 100
MAX_SERVICE_NAME_LEN = 100
MAX_PROVIDER_LEN = 80
MAX_RESOURCE_TYPE_LEN = 100
MAX_CUSTOM_PER_KIND = 200

# Azure region short names are letters + digits, lowercase, no separators.
_REGION_NAME_RE = re.compile(r"^[a-z][a-z0-9]{2,59}$")
# Display name is loose: letters/digits/space/parens/dash/dot.
_REGION_DISPLAY_RE = re.compile(r"^[A-Za-z0-9 _\-\.\(\)]+$")
# ARM provider namespace shape: "Microsoft.Foo" — also tolerate the
# microsoft.network style we sometimes see in docs.
_PROVIDER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,49}\.[A-Za-z][A-Za-z0-9]{1,49}$")
# Resource type shape: "virtualMachines", "snapshots", etc. (mixed case allowed).
_RESOURCE_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,99}$")
# Service "display" name (what the user sees in the picker): loose.
_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-\.\(\)+\/]{1,99}$")


class BomCatalogError(Exception):
    """Stable error code so HTTP handlers can surface a friendly message."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# Hot-cache of the merged catalogs keyed by partition. Invalidated on
# every write so the next read picks up the new state. The threading
# lock guards both the cache + the table writes — local-mode has a
# single Functions worker so contention is essentially zero, but the
# Functions host can spawn worker threads for HTTP triggers and we'd
# rather not race.
_LOCK = threading.Lock()
_CACHE: Dict[str, List[Dict]] = {}


def _invalidate(kind: str) -> None:
    with _LOCK:
        _CACHE.pop(kind, None)


def _normalize_region_name(s: str) -> str:
    return (s or "").strip().lower()


def _normalize_service_name(s: str) -> str:
    # Preserve original case but trim whitespace and collapse internal
    # runs of spaces — picker labels look nicer.
    return re.sub(r"\s+", " ", (s or "").strip())


# ─── Region validators ─────────────────────────────────────────────────────

def validate_region(payload: Dict) -> Dict:
    if not isinstance(payload, dict):
        raise BomCatalogError("bad_region", "region payload must be an object.", 400)
    name = _normalize_region_name(payload.get("name") or "")
    display_name = (payload.get("display_name") or "").strip()
    has_az = bool(payload.get("has_az"))
    if not name:
        raise BomCatalogError("bad_region", "region.name is required.", 400)
    if len(name) > MAX_REGION_NAME_LEN or not _REGION_NAME_RE.match(name):
        raise BomCatalogError(
            "bad_region",
            ("region.name must be lowercase letters+digits, 3-60 chars, "
             "starting with a letter (e.g. 'eastus2')."),
            400,
        )
    if not display_name:
        # Fall back to a Title-Cased version of the short name so the UI
        # always has something readable.
        display_name = name.replace("_", " ").title()
    if len(display_name) > MAX_REGION_DISPLAY_LEN or not _REGION_DISPLAY_RE.match(display_name):
        raise BomCatalogError(
            "bad_region",
            (f"region.display_name must be <= {MAX_REGION_DISPLAY_LEN} chars "
             "of letters/digits/spaces/().-_"),
            400,
        )
    return {"name": name, "display_name": display_name, "has_az": has_az}


def validate_service(payload: Dict) -> Dict:
    if not isinstance(payload, dict):
        raise BomCatalogError("bad_service", "service payload must be an object.", 400)
    name = _normalize_service_name(payload.get("name") or "")
    provider = (payload.get("provider") or "").strip()
    resource_type = (payload.get("resource_type") or "").strip()
    zone_check = bool(payload.get("zone_check"))
    if not name:
        raise BomCatalogError("bad_service", "service.name is required.", 400)
    if len(name) > MAX_SERVICE_NAME_LEN or not _SERVICE_NAME_RE.match(name):
        raise BomCatalogError(
            "bad_service",
            (f"service.name must be <= {MAX_SERVICE_NAME_LEN} chars and "
             "contain only letters/digits/spaces and the punctuation "
             "_ - . ( ) + /"),
            400,
        )
    if len(provider) > MAX_PROVIDER_LEN or not _PROVIDER_RE.match(provider):
        raise BomCatalogError(
            "bad_service",
            ("service.provider must look like 'Microsoft.Foo' "
             "(e.g. 'Microsoft.Network')."),
            400,
        )
    if len(resource_type) > MAX_RESOURCE_TYPE_LEN or not _RESOURCE_TYPE_RE.match(resource_type):
        raise BomCatalogError(
            "bad_service",
            ("service.resource_type must be camelCase letters+digits "
             "(e.g. 'virtualMachines')."),
            400,
        )
    return {
        "name": name,
        "provider": provider,
        "resource_type": resource_type,
        "zone_check": zone_check,
    }


# ─── Public read API ───────────────────────────────────────────────────────

def list_custom(kind: str) -> List[Dict]:
    """Return all custom entries for ``kind`` ("region"|"service").

    Reads-through the in-process cache. Each record has the same shape
    as the seed file plus ``is_custom=True``.
    """
    if kind not in ("region", "service"):
        raise BomCatalogError("bad_kind", f"unknown catalog kind: {kind}", 400)
    with _LOCK:
        cached = _CACHE.get(kind)
        if cached is not None:
            return [dict(r) for r in cached]
    out: List[Dict] = []
    try:
        table = storage.get_table_client(TABLE_NAME)
        # Query just our partition. The Functions runtime auto-creates
        # the table on first upsert so we tolerate "table doesn't
        # exist yet" by treating it as empty.
        for e in table.query_entities(f"PartitionKey eq '{kind}'"):
            raw = e.get("payload_json") or ""
            try:
                payload = json.loads(raw)
            except Exception:
                log.warning("bom_catalog: corrupt payload for %s/%s",
                            kind, e.get("RowKey"))
                continue
            if not isinstance(payload, dict):
                continue
            payload["is_custom"] = True
            out.append(payload)
    except Exception:
        log.exception("bom_catalog: list_custom(%s) failed — returning empty", kind)
        return []
    out.sort(key=lambda r: (r.get("name") or "").lower())
    with _LOCK:
        _CACHE[kind] = [dict(r) for r in out]
    return out


# ─── Public write API ──────────────────────────────────────────────────────

def add_region(payload: Dict, *, existing_builtin_names: List[str]) -> Dict:
    rec = validate_region(payload)
    existing_lc = {n.lower() for n in (existing_builtin_names or [])}
    if rec["name"] in existing_lc:
        raise BomCatalogError(
            "duplicate_region",
            (f"Region '{rec['name']}' already exists in the built-in "
             "catalog — edit api/_shared/data/bom_region_catalog.json "
             "to change it instead."),
            409,
        )
    customs = list_custom("region")
    # Enforce per-kind cap to keep the editor responsive.
    if len(customs) >= MAX_CUSTOM_PER_KIND:
        raise BomCatalogError(
            "too_many_custom",
            (f"Too many custom regions ({len(customs)}; max "
             f"{MAX_CUSTOM_PER_KIND}). Remove some first."),
            413,
        )
    # Idempotent upsert on RowKey — re-adding with new display/az just
    # updates in place. Distinct from add-vs-edit at the UI layer.
    entity = {
        "PartitionKey": "region",
        "RowKey": rec["name"],
        "payload_json": json.dumps(rec, ensure_ascii=False),
    }
    table = storage.get_table_client(TABLE_NAME)
    table.upsert_entity(entity, mode="replace")
    log.info("bom_catalog: upsert region name=%s display=%s has_az=%s",
             rec["name"], rec["display_name"], rec["has_az"])
    _invalidate("region")
    rec["is_custom"] = True
    return rec


def delete_region(name: str) -> bool:
    short = _normalize_region_name(name)
    if not short:
        raise BomCatalogError("bad_region", "region name required.", 400)
    table = storage.get_table_client(TABLE_NAME)
    try:
        table.delete_entity(partition_key="region", row_key=short)
    except KeyError:
        return False  # nothing to delete — idempotent
    _invalidate("region")
    log.info("bom_catalog: delete region name=%s", short)
    return True


def add_service(payload: Dict, *, existing_builtin_names: List[str]) -> Dict:
    rec = validate_service(payload)
    # Service uniqueness is case-insensitive on the display name.
    existing_lc = {(n or "").lower() for n in (existing_builtin_names or [])}
    if rec["name"].lower() in existing_lc:
        raise BomCatalogError(
            "duplicate_service",
            (f"Service '{rec['name']}' already exists in the built-in "
             "catalog — edit api/_shared/data/bom_service_catalog.json "
             "to change it instead."),
            409,
        )
    customs = list_custom("service")
    if len(customs) >= MAX_CUSTOM_PER_KIND:
        raise BomCatalogError(
            "too_many_custom",
            (f"Too many custom services ({len(customs)}; max "
             f"{MAX_CUSTOM_PER_KIND})."),
            413,
        )
    # Use lowercased name as RowKey for uniqueness regardless of case.
    entity = {
        "PartitionKey": "service",
        "RowKey": rec["name"].lower(),
        "payload_json": json.dumps(rec, ensure_ascii=False),
    }
    table = storage.get_table_client(TABLE_NAME)
    table.upsert_entity(entity, mode="replace")
    log.info("bom_catalog: upsert service name=%s provider=%s rt=%s zc=%s",
             rec["name"], rec["provider"], rec["resource_type"], rec["zone_check"])
    _invalidate("service")
    rec["is_custom"] = True
    return rec


def delete_service(name: str) -> bool:
    key = _normalize_service_name(name).lower()
    if not key:
        raise BomCatalogError("bad_service", "service name required.", 400)
    table = storage.get_table_client(TABLE_NAME)
    try:
        table.delete_entity(partition_key="service", row_key=key)
    except KeyError:
        return False  # nothing to delete — idempotent
    _invalidate("service")
    log.info("bom_catalog: delete service name=%s", key)
    return True


def get_custom_region(name: str) -> Optional[Dict]:
    short = _normalize_region_name(name)
    for r in list_custom("region"):
        if r.get("name") == short:
            return r
    return None


def get_custom_service(name: str) -> Optional[Dict]:
    key = _normalize_service_name(name).lower()
    for r in list_custom("service"):
        if (r.get("name") or "").lower() == key:
            return r
    return None

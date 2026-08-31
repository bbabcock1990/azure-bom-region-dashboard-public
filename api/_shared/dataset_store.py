"""User-uploadable overrides for the model's reference datasets.

The model ships a set of read-only *seed* datasets under
``api/_shared/data/`` (latency matrix, region/service catalogs, the
regions/skus/family seed lists). Those files are baked into the package
and can't be edited at runtime — but Azure keeps launching new regions
and publishing fresh metrics, so operators need a way to refresh them
without rebuilding the app.

This module adds a thin **override layer**. For every managed dataset we
keep:

    * the packaged **seed** file (``api/_shared/data/<filename>``), and
    * an optional **override** file the user uploaded, stored in the
      writable local-storage area (``<LOCAL_STORAGE_DIR>/datasets/`` or
      ``<repo>/local-storage/datasets/``).

Every loader in the codebase now resolves its path through
:func:`resolve_path`, which returns the override when present and falls
back to the seed otherwise. Uploading a file therefore transparently
replaces the seed for all future reads; deleting the override reverts to
the packaged seed. No seed file is ever mutated.

Each upload is **validated** (correct shape, non-empty, size-capped)
before it is written, and every write invalidates the relevant in-process
caches so the new data takes effect immediately — no restart required.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# Packaged seed directory (read-only): api/_shared/data
_DATA_DIR = Path(__file__).resolve().parent / "data"

# Hard cap on any uploaded dataset. The largest real file (the latency
# matrix) is well under 1 MB; 8 MB leaves generous headroom while keeping
# a bad upload from filling the disk.
MAX_DATASET_BYTES = 8 * 1024 * 1024


class DatasetError(Exception):
    """Stable error code so the HTTP layer can surface a friendly message."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ─── Storage location ────────────────────────────────────────────────────────

def _override_dir() -> Path:
    """Writable directory holding uploaded dataset overrides.

    Mirrors ``storage._storage_root()`` so all local state lives together.
    Created on demand.
    """
    env = os.getenv("LOCAL_STORAGE_DIR", "").strip()
    if env:
        root = Path(env)
    else:
        # api/_shared/dataset_store.py -> api/_shared -> api -> <repo root>
        root = Path(__file__).resolve().parents[2] / "local-storage"
    d = root / "datasets"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        log.exception("dataset_store: could not create override dir %s", d)
    return d


# ─── Provenance metadata sidecar ─────────────────────────────────────────────
# Alongside the override files we keep a tiny JSON sidecar recording where each
# active override came from ("upload" | "arm" | "url"), when it was written, and
# (for URL sources) the URL so it can be re-fetched later. This powers the
# "Source: Azure ARM · fetched <date>" provenance shown in the UI and the
# "Refresh from URL" gesture.

_META_FILENAME = "_sources.json"


def _meta_path() -> Path:
    return _override_dir() / _META_FILENAME


def _load_meta() -> Dict[str, Dict]:
    p = _meta_path()
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        log.exception("dataset_store: could not read sources sidecar")
    return {}


def _write_meta(meta: Dict[str, Dict]) -> None:
    p = _meta_path()
    tmp = str(p) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        os.replace(tmp, p)
    except Exception:
        log.exception("dataset_store: could not write sources sidecar")


def _set_meta(ds_id: str, *, origin: str, url: Optional[str] = None) -> None:
    """Record provenance for a freshly written override. Overwrites any prior
    entry so a plain re-upload drops a stale URL."""
    meta = _load_meta()
    entry: Dict = {
        "origin": origin,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if url:
        entry["url"] = url
    meta[ds_id] = entry
    _write_meta(meta)


def _clear_meta(ds_id: str, *, url_only: bool = False) -> None:
    meta = _load_meta()
    if ds_id not in meta:
        return
    if url_only:
        meta[ds_id].pop("url", None)
        # The fetched file is kept but detached from its URL — treat it as a
        # manual override so the UI doesn't show a "Linked URL" with no link.
        if meta[ds_id].get("origin") == "url":
            meta[ds_id]["origin"] = "upload"
    else:
        meta.pop(ds_id, None)
    _write_meta(meta)


def _meta_for(ds_id: str) -> Dict:
    return _load_meta().get(ds_id) or {}


# ─── Validators ──────────────────────────────────────────────────────────────
# Each returns a short human-readable summary dict on success and raises
# DatasetError("bad_dataset", ...) on any structural problem.

def _decode_text(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise DatasetError("bad_dataset", "File is not valid UTF-8 text.", 400)


def _validate_latency_csv(raw: bytes) -> Dict:
    text = _decode_text(raw)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise DatasetError("bad_dataset", "Latency CSV is empty.", 400)
    if not header or (header[0] or "").strip().lower() != "source":
        raise DatasetError(
            "bad_dataset",
            "Latency CSV must start with a 'Source' column followed by one "
            "column per destination region (matching the exported matrix).",
            400,
        )
    dests = [h.strip() for h in header[1:] if (h or "").strip()]
    if not dests:
        raise DatasetError("bad_dataset",
                           "Latency CSV header has no destination regions.", 400)
    rows = 0
    for row in reader:
        if row and (row[0] or "").strip():
            rows += 1
    if rows == 0:
        raise DatasetError("bad_dataset",
                           "Latency CSV has a header but no data rows.", 400)
    return {"summary": f"{rows} source regions × {len(dests)} destinations",
            "rows": rows, "columns": len(dests)}


def _validate_region_catalog(raw: bytes) -> Dict:
    text = _decode_text(raw)
    try:
        data = json.loads(text)
    except Exception as ex:
        raise DatasetError("bad_dataset", f"Not valid JSON: {ex}", 400)
    if not isinstance(data, dict) or not isinstance(data.get("regions"), list):
        raise DatasetError(
            "bad_dataset",
            'Region catalog must be a JSON object with a "regions" array.',
            400,
        )
    regions = data["regions"]
    if not regions:
        raise DatasetError("bad_dataset", "Region catalog has no regions.", 400)
    valid = 0
    for r in regions:
        if isinstance(r, dict) and (r.get("name") or "").strip():
            valid += 1
    if valid == 0:
        raise DatasetError("bad_dataset",
                           "No region entries have a 'name'.", 400)
    return {"summary": f"{valid} regions", "rows": valid}


def _validate_service_catalog(raw: bytes) -> Dict:
    text = _decode_text(raw)
    try:
        data = json.loads(text)
    except Exception as ex:
        raise DatasetError("bad_dataset", f"Not valid JSON: {ex}", 400)
    if not isinstance(data, dict) or not isinstance(data.get("services"), list):
        raise DatasetError(
            "bad_dataset",
            'Service catalog must be a JSON object with a "services" array.',
            400,
        )
    services = data["services"]
    if not services:
        raise DatasetError("bad_dataset", "Service catalog has no services.", 400)
    valid = 0
    for s in services:
        if (isinstance(s, dict) and (s.get("name") or "").strip()
                and (s.get("provider") or "").strip()
                and (s.get("resource_type") or "").strip()):
            valid += 1
    if valid == 0:
        raise DatasetError(
            "bad_dataset",
            "No service entries have name + provider + resource_type.",
            400,
        )
    return {"summary": f"{valid} services", "rows": valid}


_TOKEN_SPLIT_RE = re.compile(r"[,\r\n]+")


def _validate_token_list(raw: bytes, *, label: str) -> Dict:
    text = _decode_text(raw)
    tokens = [t.strip() for t in _TOKEN_SPLIT_RE.split(text) if t.strip()]
    if not tokens:
        raise DatasetError("bad_dataset", f"{label} list is empty.", 400)
    return {"summary": f"{len(tokens)} entries", "rows": len(tokens)}


# ─── Registry ────────────────────────────────────────────────────────────────

class _Dataset:
    def __init__(self, ds_id: str, *, filename: str, label: str, kind: str,
                 description: str, accept: str,
                 validate: Callable[[bytes], Dict],
                 suggested_url: str = "", suggested_label: str = ""):
        self.id = ds_id
        self.filename = filename
        self.label = label
        self.kind = kind
        self.description = description
        self.accept = accept
        self.validate = validate
        # An optional canonical public source for this dataset, surfaced in the
        # UI as a one-click "refresh from <source>" button.
        self.suggested_url = suggested_url
        self.suggested_label = suggested_label


_REGISTRY: Dict[str, _Dataset] = {
    d.id: d for d in [
        _Dataset(
            "latency", filename="azure_region_latency.csv",
            label="Region latency matrix", kind="csv", accept=".csv",
            description=("Inter-region round-trip latency (ms). A 'Source' "
                         "column followed by one column per destination "
                         "region. Drives the latency map and heatmap. Link the "
                         "Microsoft Docs latency article (a .md URL) and it's "
                         "parsed into this matrix automatically — or link a CSV."),
            validate=_validate_latency_csv,
            suggested_url=("https://raw.githubusercontent.com/MicrosoftDocs/"
                           "azure-docs/main/articles/networking/"
                           "azure-network-latency.md"),
            suggested_label="Microsoft Docs",
        ),
        _Dataset(
            "region_catalog", filename="bom_region_catalog.json",
            label="Region catalog", kind="json", accept=".json",
            description=("Master list of Azure regions with availability-zone "
                         "support. Feeds the BOM region picker and display "
                         "names."),
            validate=_validate_region_catalog,
        ),
        _Dataset(
            "service_catalog", filename="bom_service_catalog.json",
            label="Service catalog", kind="json", accept=".json",
            description=("Azure services with their ARM provider / resource "
                         "type, used for zonal availability checks."),
            validate=_validate_service_catalog,
        ),
        _Dataset(
            "regions_list", filename="regions.txt",
            label="Default region list", kind="list", accept=".txt",
            description=("Default set of regions analysed when a BOM doesn't "
                         "pin its own. One short region name per line."),
            validate=lambda raw: _validate_token_list(raw, label="Region"),
        ),
        _Dataset(
            "skus_list", filename="skus.txt",
            label="Default SKU family list", kind="list", accept=".txt",
            description=("Default VM SKU families paired with the model when "
                         "none are supplied. One family id per line."),
            validate=lambda raw: _validate_token_list(raw, label="SKU family"),
        ),
        _Dataset(
            "sku_families_seed", filename="sku_families_seed.txt",
            label="SKU family picker seed", kind="list", accept=".txt",
            description=("Broad snapshot of canonical VM family ids used to "
                         "seed the SKU picker before a live ARM pull."),
            validate=lambda raw: _validate_token_list(raw, label="SKU family"),
        ),
    ]
}


def _require(ds_id: str) -> _Dataset:
    ds = _REGISTRY.get(ds_id)
    if ds is None:
        raise DatasetError("unknown_dataset", f"Unknown dataset '{ds_id}'.", 404)
    return ds


# ─── Path resolution ─────────────────────────────────────────────────────────

def packaged_path(ds_id: str) -> str:
    return str(_DATA_DIR / _require(ds_id).filename)


def override_path(ds_id: str) -> str:
    return str(_override_dir() / _require(ds_id).filename)


def has_override(ds_id: str) -> bool:
    try:
        return os.path.exists(override_path(ds_id))
    except DatasetError:
        return False


def resolve_path(ds_id: str) -> str:
    """Return the active path for ``ds_id``: the uploaded override if one
    exists, else the packaged seed. This is the single entry point every
    loader in the codebase uses to read a managed dataset."""
    ov = override_path(ds_id)
    if os.path.exists(ov):
        return ov
    return packaged_path(ds_id)


# ─── Cache invalidation ──────────────────────────────────────────────────────

def _invalidate_caches(ds_id: str) -> None:
    """Clear any in-process caches that memoize the affected dataset so a
    fresh upload / reset takes effect without a restart. Best-effort and
    lazily-imported to avoid import cycles."""
    try:
        if ds_id in ("latency",):
            from . import bom_services
            bom_services.reset_dataset_caches()
        elif ds_id == "region_catalog":
            from . import bom_regions
            bom_regions.reset_dataset_caches()
        elif ds_id == "service_catalog":
            from . import bom_services
            bom_services.reset_dataset_caches()
        elif ds_id in ("skus_list", "sku_families_seed"):
            from . import sku_families
            sku_families.reset_dataset_caches()
        # regions_list is read fresh on every run — nothing to clear.
    except Exception:
        log.exception("dataset_store: cache invalidation for %s failed", ds_id)


# ─── Public read/write API ───────────────────────────────────────────────────

def _stat(path: str) -> Tuple[Optional[int], Optional[str]]:
    try:
        st = os.stat(path)
    except OSError:
        return None, None
    modified = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    return st.st_size, modified


def _summarize(ds: _Dataset, path: str) -> str:
    """Best-effort human summary of a file's contents (row counts, etc.)."""
    try:
        with open(path, "rb") as f:
            return ds.validate(f.read()).get("summary", "")
    except DatasetError as ex:
        return f"⚠ {ex.message}"
    except Exception:
        return ""


def _can_refresh_arm(ds_id: str) -> bool:
    try:
        from . import dataset_providers
        return dataset_providers.can_refresh(ds_id)
    except Exception:
        return False


def describe(ds_id: str) -> Dict:
    ds = _require(ds_id)
    active = resolve_path(ds_id)
    is_custom = has_override(ds_id)
    size, modified = _stat(active)
    meta = _meta_for(ds_id) if is_custom else {}
    origin = meta.get("origin") or ("upload" if is_custom else "builtin")
    return {
        "id": ds.id,
        "label": ds.label,
        "filename": ds.filename,
        "kind": ds.kind,
        "accept": ds.accept,
        "description": ds.description,
        "source": "custom" if is_custom else "builtin",
        "is_custom": is_custom,
        "origin": origin,
        "source_url": meta.get("url"),
        "fetched_at": meta.get("fetched_at"),
        "can_refresh_arm": _can_refresh_arm(ds.id),
        "supports_url": True,
        "suggested_url": ds.suggested_url or None,
        "suggested_label": ds.suggested_label or None,
        "size": size,
        "modified": modified,
        "summary": _summarize(ds, active),
    }


def list_datasets() -> List[Dict]:
    return [describe(ds_id) for ds_id in _REGISTRY]


def read_current_bytes(ds_id: str) -> Tuple[bytes, str]:
    """Return ``(content, filename)`` for the active file (override or seed).
    Used to let the user download the current dataset."""
    ds = _require(ds_id)
    with open(resolve_path(ds_id), "rb") as f:
        return f.read(), ds.filename


def save_override(ds_id: str, raw: bytes, *, origin: str = "upload",
                  url: Optional[str] = None) -> Dict:
    """Validate and persist ``raw`` as the active override for ``ds_id``.

    ``origin`` records where the bytes came from ("upload" | "arm" | "url")
    and ``url`` (for URL sources) is stored so the dataset can be re-fetched.
    Raises :class:`DatasetError` on any validation failure — the seed and any
    previous override are left untouched in that case."""
    ds = _require(ds_id)
    if not raw:
        raise DatasetError("empty_upload", "Uploaded file is empty.", 400)
    if len(raw) > MAX_DATASET_BYTES:
        raise DatasetError(
            "file_too_large",
            f"File exceeds {MAX_DATASET_BYTES // (1024 * 1024)} MB.",
            413,
        )
    info = ds.validate(raw)  # raises on bad shape
    dest = override_path(ds_id)
    tmp = dest + ".tmp"
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, dest)  # atomic swap so a reader never sees a half file
    _set_meta(ds_id, origin=origin, url=url)
    _invalidate_caches(ds_id)
    log.info("dataset_store: saved override for %s (%d bytes, origin=%s) — %s",
             ds_id, len(raw), origin, info.get("summary"))
    return describe(ds_id)


def reset_override(ds_id: str) -> Dict:
    """Delete the uploaded override, reverting to the packaged seed.
    Idempotent — a no-op if no override exists."""
    _require(ds_id)
    ov = override_path(ds_id)
    if os.path.exists(ov):
        try:
            os.remove(ov)
        except OSError as ex:
            raise DatasetError("reset_failed",
                               f"Could not remove override: {ex}", 500)
    _clear_meta(ds_id)
    _invalidate_caches(ds_id)
    log.info("dataset_store: reset override for %s to packaged seed", ds_id)
    return describe(ds_id)


# ─── Refresh from Azure (ARM) ────────────────────────────────────────────────

def refresh_from_azure(ds_id: str) -> Dict:
    """Regenerate ``ds_id`` live from ARM and persist it as the override.

    Only datasets with a registered provider (region catalog, SKU family seed)
    can be refreshed this way; others raise a friendly error."""
    _require(ds_id)
    from . import dataset_providers
    try:
        raw = dataset_providers.refresh_bytes(ds_id)
    except dataset_providers.ProviderError as ex:
        status = 502 if ex.code in ("arm_error", "empty_result",
                                    "http_unavailable") else 400
        raise DatasetError(ex.code, ex.message, status)
    return save_override(ds_id, raw, origin="arm")


# ─── Fetch from a URL (e.g. a GitHub raw file) ───────────────────────────────

_GITHUB_BLOB_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/blob/(.+)$", re.IGNORECASE)


def _normalize_github_url(url: str) -> str:
    """Rewrite a GitHub *blob* (HTML) URL to its raw-content equivalent so a
    user can paste the URL straight from the browser address bar."""
    m = _GITHUB_BLOB_RE.match(url)
    if m:
        return (f"https://raw.githubusercontent.com/"
                f"{m.group(1)}/{m.group(2)}/{m.group(3)}")
    return url


def _http_get(url: str) -> bytes:
    try:
        import httpx
    except Exception:  # pragma: no cover
        raise DatasetError("http_unavailable", "HTTP client unavailable.", 500)
    try:
        with httpx.Client(timeout=45.0, follow_redirects=True) as client:
            resp = client.get(
                url, headers={"user-agent": "azure-bom-region-dashboard/1.0"})
    except Exception as ex:
        raise DatasetError("fetch_failed", f"Could not fetch URL: {ex}", 502)
    if resp.status_code >= 400:
        raise DatasetError(
            "fetch_failed",
            f"The URL returned HTTP {resp.status_code}.", 502)
    raw = resp.content
    if not raw:
        raise DatasetError("empty_upload", "The URL returned no content.", 400)
    if len(raw) > MAX_DATASET_BYTES:
        raise DatasetError(
            "file_too_large",
            f"Fetched file exceeds {MAX_DATASET_BYTES // (1024 * 1024)} MB.",
            413)
    return raw


def _transform_fetched(ds_id: str, raw: bytes, *, url: str = "") -> bytes:
    """Normalize fetched bytes into the dataset's expected on-disk format.

    The latency matrix is published by Microsoft as a *markdown* article rather
    than a CSV, so when the latency source looks like markdown we parse its
    tables into the CSV the model expects. Everything else passes through."""
    if ds_id != "latency":
        return raw
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")
    is_markdown = url.lower().split("?")[0].endswith(".md")
    if not is_markdown:
        from . import latency_markdown
        is_markdown = latency_markdown.looks_like_markdown_latency(text)
    if not is_markdown:
        return raw
    from . import latency_markdown
    try:
        csv_text = latency_markdown.markdown_to_latency_csv(text)
    except ValueError as ex:
        raise DatasetError(
            "bad_dataset",
            f"Could not parse the latency markdown document: {ex}", 400)
    return csv_text.encode("utf-8")


def fetch_from_url(ds_id: str, url: str) -> Dict:
    """Fetch ``url`` (validated + size-capped), store it as the override, and
    remember the URL so the dataset can be re-fetched on demand.

    For the latency dataset a linked Microsoft Docs markdown article is parsed
    into the CSV matrix automatically."""
    _require(ds_id)
    url = (url or "").strip()
    if not re.match(r"^https://", url, re.IGNORECASE):
        raise DatasetError(
            "bad_url",
            "Provide an https:// URL (e.g. a GitHub raw file link).", 400)
    url = _normalize_github_url(url)
    raw = _http_get(url)
    raw = _transform_fetched(ds_id, raw, url=url)
    return save_override(ds_id, raw, origin="url", url=url)


def refresh_source(ds_id: str) -> Dict:
    """Re-fetch a dataset from its previously configured source URL."""
    _require(ds_id)
    url = _meta_for(ds_id).get("url")
    if not url:
        raise DatasetError(
            "no_source_url",
            "No source URL is linked for this dataset.", 400)
    return fetch_from_url(ds_id, url)


def clear_source(ds_id: str) -> Dict:
    """Forget the linked source URL (the current override file is kept)."""
    _require(ds_id)
    _clear_meta(ds_id, url_only=True)
    return describe(ds_id)

"""HTTP API for managing the model's reference datasets.

Lets the operator refresh the packaged seed data (latency matrix, region
/ service catalogs, region / SKU seed lists) as Azure ships new regions
and metrics — without rebuilding the app. Uploads are validated, stored
as a local override, and take effect immediately (see
``_shared.dataset_store``).

Routes:
    GET    /api/datasets                     → list all managed datasets + status
    GET    /api/datasets/{dataset_id}        → download the active file
    POST   /api/datasets/{dataset_id}        → upload a new override (multipart 'file')
    DELETE /api/datasets/{dataset_id}        → revert to the packaged seed
"""
from __future__ import annotations

import json
import logging
from typing import Optional
from urllib.parse import urlparse

from .._shared import httpfunc as func
from .._shared import activity_log, auth, csrf, dataset_store

log = logging.getLogger(__name__)


def _err(code: str, message: str, status: int = 400) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": code, "message": message}),
        status_code=status, mimetype="application/json",
    )


def _ok(payload, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload), status_code=status, mimetype="application/json",
    )


def _action(req: func.HttpRequest) -> str:
    """Sub-action encoded as the trailing URL segment.

    ``/api/datasets/{id}``          → "" (upload / reset / download / list)
    ``/api/datasets/{id}/refresh``  → "refresh" (regenerate from ARM)
    ``/api/datasets/{id}/source``   → "source"  (link / re-fetch / unlink URL)
    """
    try:
        path = urlparse(req.url or "").path.rstrip("/")
    except Exception:
        return ""
    if path.endswith("/refresh"):
        return "refresh"
    if path.endswith("/source"):
        return "source"
    return ""


def main(req: func.HttpRequest) -> func.HttpResponse:
    method = (req.method or "GET").upper()
    dataset_id: Optional[str] = req.route_params.get("dataset_id")
    action = _action(req)
    principal = auth.get_local_user(req)

    def _event() -> str:
        if action == "refresh":
            return "dataset_refresh"
        if action == "source":
            return "dataset_source" if method == "POST" else "dataset_source_clear"
        return "dataset_upload" if method == "POST" else "dataset_reset"

    def _log(status: str, message: str, **details) -> None:
        activity_log.record(
            _event(),
            actor_email=principal.email,
            actor_oid=principal.oid,
            api_scope="local",
            status=status,
            message=message,
            details=details or None,
        )

    try:
        # ─── Read paths ──────────────────────────────────────────────────
        if method == "GET":
            if not dataset_id:
                return _ok({"datasets": dataset_store.list_datasets()})
            # Download the active file (override or seed).
            try:
                content, filename = dataset_store.read_current_bytes(dataset_id)
            except dataset_store.DatasetError as ex:
                return _err(ex.code, ex.message, ex.status)
            return func.HttpResponse(
                content,
                status_code=200,
                mimetype="application/octet-stream",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

        # ─── Mutating paths need an origin check ─────────────────────────
        try:
            csrf.assert_safe_origin(req)
        except csrf.OriginError as ex:
            log.warning("origin check rejected datasets: %s", ex)
            _log("error", "Dataset change rejected by origin policy",
                 dataset=dataset_id, code="origin_rejected")
            return _err("origin_rejected", str(ex), 403)

        if not dataset_id:
            return _err("missing_dataset",
                        "Dataset id is required in the route.", 400)

        # ─── Refresh from Azure (ARM) ────────────────────────────────────
        if method == "POST" and action == "refresh":
            try:
                info = dataset_store.refresh_from_azure(dataset_id)
            except dataset_store.DatasetError as ex:
                _log("error", f"Dataset refresh failed: {ex.code}",
                     dataset=dataset_id, code=ex.code)
                return _err(ex.code, ex.message, ex.status)
            _log("ok", f"Refreshed '{dataset_id}' from Azure",
                 dataset=dataset_id, summary=info.get("summary"))
            return _ok(info, status=200)

        # ─── Link / re-fetch / unlink a source URL ───────────────────────
        if action == "source":
            if method == "POST":
                body = {}
                try:
                    body = req.get_json() or {}
                except Exception:
                    body = {}
                url = (body.get("url") or "").strip()
                try:
                    if url:
                        info = dataset_store.fetch_from_url(dataset_id, url)
                    else:
                        info = dataset_store.refresh_source(dataset_id)
                except dataset_store.DatasetError as ex:
                    _log("error", f"Dataset URL fetch failed: {ex.code}",
                         dataset=dataset_id, code=ex.code)
                    return _err(ex.code, ex.message, ex.status)
                _log("ok", f"Fetched '{dataset_id}' from URL",
                     dataset=dataset_id, url=info.get("source_url"),
                     summary=info.get("summary"))
                return _ok(info, status=200)
            if method == "DELETE":
                try:
                    info = dataset_store.clear_source(dataset_id)
                except dataset_store.DatasetError as ex:
                    return _err(ex.code, ex.message, ex.status)
                _log("ok", f"Unlinked source URL for '{dataset_id}'",
                     dataset=dataset_id)
                return _ok(info, status=200)
            return _err("method_not_allowed",
                        f"{method} not supported here.", 405)

        if method == "POST":
            try:
                f = req.files.get("file")
            except Exception as ex:
                _log("error", "Dataset upload multipart parse failed",
                     dataset=dataset_id)
                return _err("bad_request",
                            f"Could not parse multipart body: {ex}", 400)
            if f is None:
                _log("error", "Dataset upload missing file", dataset=dataset_id)
                return _err("missing_file",
                            "Upload a file in the 'file' field.", 400)
            raw = f.read()
            try:
                info = dataset_store.save_override(dataset_id, raw)
            except dataset_store.DatasetError as ex:
                _log("error", f"Dataset upload failed: {ex.code}",
                     dataset=dataset_id, code=ex.code,
                     filename=getattr(f, "filename", ""))
                return _err(ex.code, ex.message, ex.status)
            _log("ok", f"Uploaded new '{dataset_id}' dataset",
                 dataset=dataset_id, filename=getattr(f, "filename", ""),
                 summary=info.get("summary"))
            return _ok(info, status=200)

        if method == "DELETE":
            try:
                info = dataset_store.reset_override(dataset_id)
            except dataset_store.DatasetError as ex:
                _log("error", f"Dataset reset failed: {ex.code}",
                     dataset=dataset_id, code=ex.code)
                return _err(ex.code, ex.message, ex.status)
            _log("ok", f"Reset '{dataset_id}' dataset to packaged seed",
                 dataset=dataset_id)
            return _ok(info, status=200)

        return _err("method_not_allowed", f"{method} not supported here.", 405)
    except Exception as ex:
        log.exception("datasets crash")
        if method != "GET":
            _log("error", "Dataset change failed with an internal error",
                 dataset=dataset_id)
        return _err("internal_error", f"Unhandled exception: {ex!r}", 500)

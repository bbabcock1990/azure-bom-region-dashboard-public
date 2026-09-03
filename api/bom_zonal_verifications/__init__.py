"""GET/POST /api/bom/zonal-verifications

Persist and rehydrate the results of a "Verify all regions" scan so the raised
verdict confidence survives a page reload or re-opening the same snapshot.

The scan itself is read-only (it calls ``/api/bom/zonal-capability`` per region
and creates nothing). This endpoint only stores the *results* of those probes,
keyed by ``run_id`` + ``subscription_id`` so they stay scoped to the snapshot
and subscription they were gathered against.

POST body::

    {
      "run_id": "2024-01-01T00-00-00Z-abcd",
      "subscription_id": "<sub guid>",
      "results": {
        "eastus": {"map": {"Azure Blob Storage||zrs": {...verdict...}}, "ts": "..."}
      }
    }

GET query::

    ?run_id=<run>&subscription_id=<sub>  ->  {"results": { ... }}
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict

from .._shared import auth, csrf
from .._shared import httpfunc as func
from .._shared import storage

log = logging.getLogger(__name__)

_CONTAINER = "verifications"
_MAX_REGIONS = 200
_ID_RE = re.compile(r"[^A-Za-z0-9._-]")


def _ok(payload: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(payload), status_code=status, mimetype="application/json")


def _err(code: str, message: str, status: int = 400) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": code, "message": message}), status_code=status, mimetype="application/json"
    )


def _blob_name(run_id: str, subscription_id: str) -> str:
    safe_run = _ID_RE.sub("_", str(run_id or ""))
    safe_sub = _ID_RE.sub("_", str(subscription_id or ""))
    return f"{safe_run}__{safe_sub}.json"


async def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)

    method = (req.method or "GET").upper()

    if method == "GET":
        run_id = req.params.get("run_id", "")
        subscription_id = req.params.get("subscription_id", "")
        if not run_id or not subscription_id:
            return _err("missing_params", "run_id and subscription_id are required.", 400)
        container = storage.get_blob_container(_CONTAINER)
        try:
            downloader = container.download_blob(_blob_name(run_id, subscription_id))
            payload = json.loads(downloader.readall().decode("utf-8"))
        except FileNotFoundError:
            return _ok({"results": {}})
        except Exception:
            log.exception("failed to read verification blob")
            return _ok({"results": {}})
        return _ok({"results": payload.get("results", {})})

    if method == "POST":
        try:
            csrf.assert_safe_origin(req)
        except csrf.OriginError as ex:
            return _err("origin_rejected", str(ex), 403)
        try:
            body = req.get_json()
        except ValueError:
            return _err("bad_json", "Body must be a JSON object.", 400)
        if not isinstance(body, dict):
            return _err("bad_json", "Body must be a JSON object.", 400)
        run_id = str(body.get("run_id") or "")
        subscription_id = str(body.get("subscription_id") or "")
        results = body.get("results")
        if not run_id or not subscription_id:
            return _err("missing_params", "run_id and subscription_id are required.", 400)
        if not isinstance(results, dict):
            return _err("bad_results", "results must be an object keyed by region.", 400)
        if len(results) > _MAX_REGIONS:
            return _err("too_many", f"At most {_MAX_REGIONS} regions may be persisted.", 400)

        record: Dict[str, Any] = {
            "run_id": run_id,
            "subscription_id": subscription_id,
            "results": results,
        }
        container = storage.get_blob_container(_CONTAINER)
        try:
            container.upload_blob(
                _blob_name(run_id, subscription_id),
                json.dumps(record),
                overwrite=True,
                content_type="application/json",
            )
        except Exception:
            log.exception("failed to persist verification blob")
            return _err("persist_failed", "Could not persist verification results.", 500)
        return _ok({"ok": True, "regions": len(results)})

    return _err("method_not_allowed", f"{method} not supported.", 405)

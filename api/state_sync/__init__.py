"""Browser-held state sync — the durable store for the stateless hosted app.

In in-memory (zero-disk) mode the server keeps nothing on disk, so the signed-in
customer's browser is the durable store. These endpoints let the browser replay
its saved state on load and mirror it back on change:

    GET  /api/state/export  -> the current user's whole store as a JSON document
    POST /api/state/import  -> load a previously exported document into RAM
    POST /api/state/clear   -> wipe the current user's in-memory store

The document is opaque to the browser; it just round-trips it to localStorage
(namespaced by the signed-in user), so concurrent customers never share data and
nothing customer-specific ever lands on the server disk.
"""
from __future__ import annotations

import json
import logging

from .._shared import csrf, storage
from .._shared import httpfunc as func

log = logging.getLogger(__name__)


def _json(payload, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload), status_code=status, mimetype="application/json"
    )


def _action(req: func.HttpRequest) -> str:
    action = (req.route_params.get("action") or "").strip().lower()
    if action:
        return action
    # Fall back to the last path segment for literal routes.
    path = (req.url or "").split("?", 1)[0].rstrip("/")
    return path.rsplit("/", 1)[-1].lower()


def main(req: func.HttpRequest) -> func.HttpResponse:
    action = _action(req)

    if req.method == "GET" and action == "export":
        try:
            return _json(storage.export_state())
        except Exception:
            log.exception("state export failed")
            return _json({"error": "export_failed"}, 500)

    # Everything below mutates or reads on behalf of a POST — require a
    # same-origin request to defeat CSRF.
    try:
        csrf.assert_safe_origin(req)
    except csrf.OriginError as ex:
        return _json({"error": "origin_rejected", "message": str(ex)}, 403)

    if action == "import":
        try:
            doc = req.get_json()
        except ValueError as ex:
            return _json({"error": "bad_json", "message": str(ex)}, 400)
        try:
            summary = storage.import_state(doc)
        except Exception:
            log.exception("state import failed")
            return _json({"error": "import_failed"}, 500)
        return _json({"ok": True, **summary})

    if action == "clear":
        try:
            storage.clear_state()
        except Exception:
            log.exception("state clear failed")
            return _json({"error": "clear_failed"}, 500)
        return _json({"ok": True})

    return _json({"error": "unknown_action", "action": action}, 404)

"""HTTP API for the BOM region catalog (built-in + custom).

Routes:
    GET    /bom/region_catalog                → merged list (with is_custom flag)
    POST   /bom/region_catalog                → add a custom region
    DELETE /bom/region_catalog/{name}         → remove a custom region

Built-in entries (from ``api/_shared/data/bom_region_catalog.json``)
can only be edited by modifying the seed file and restarting the
function host. The HTTP API only manages custom overlays.

POST body (JSON):
    {
        "name":         "polandcentral",   // lowercase short name
        "display_name": "Poland Central",  // friendly name for the picker
        "has_az":       true               // whether the region has AZs
    }
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from .._shared import httpfunc as func

from .._shared import activity_log, auth, bom_catalog, bom_regions, csrf

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


def main(req: func.HttpRequest) -> func.HttpResponse:
    method = (req.method or "GET").upper()
    name: Optional[str] = req.route_params.get("name")
    principal = auth.get_local_user(req)

    def _log(status: str, message: str, *, details=None) -> None:
        activity_log.record(
            "bom_region_add" if method == "POST" else "bom_region_remove",
            actor_email=principal.email,
            actor_oid=principal.oid,
            api_scope="local",
            status=status,
            message=message,
            details=details,
        )

    if method != "GET":
        try:
            csrf.assert_safe_origin(req)
        except csrf.OriginError as ex:
            log.warning("origin check rejected bom_region_catalog: %s", ex)
            _log("error", "Region catalog change rejected by origin policy", details={"name": name})
            return _err("origin_rejected", str(ex), 403)

    try:
        if method == "GET":
            regions = bom_regions.load_merged_catalog()
            return _ok({"regions": regions})

        if method == "POST":
            try:
                body = req.get_json()
            except ValueError:
                _log("error", "Region catalog add rejected: invalid JSON")
                return _err("bad_json", "Body is not valid JSON.", 400)
            if not isinstance(body, dict):
                _log("error", "Region catalog add rejected: body was not an object")
                return _err("bad_json", "Body must be a JSON object.", 400)
            try:
                rec = bom_catalog.add_region(
                    body,
                    existing_builtin_names=bom_regions.list_builtin_names(),
                )
            except bom_catalog.BomCatalogError as ex:
                _log("error", f"Region catalog add failed: {ex.code}", details={"name": body.get("name"), "code": ex.code})
                return _err(ex.code, ex.message, ex.status)
            _log("ok", f"Added region catalog entry {rec.get('name')}", details={"name": rec.get("name")})
            return _ok(rec, status=201)

        if method == "DELETE":
            if not name:
                _log("error", "Region catalog delete rejected: missing name")
                return _err("missing_name",
                            "DELETE requires the region name in the route.", 400)
            try:
                bom_catalog.delete_region(name)
            except bom_catalog.BomCatalogError as ex:
                _log("error", f"Region catalog delete failed: {ex.code}", details={"name": name, "code": ex.code})
                return _err(ex.code, ex.message, ex.status)
            _log("ok", f"Removed region catalog entry {name}", details={"name": name})
            return func.HttpResponse(status_code=204)

        return _err("method_not_allowed", f"{method} not supported here.", 405)
    except Exception as ex:
        log.exception("bom_region_catalog crash")
        if method != "GET":
            _log("error", "Region catalog change failed with an internal error", details={"name": name})
        return _err("internal_error", f"Unhandled exception: {ex!r}", 500)

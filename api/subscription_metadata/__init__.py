"""HTTP API for the in-app BOM metadata table.

A BOM has its own identity (``bom_id``) independent of the subscription it
targets, so one subscription can own multiple BOMs.

Routes (mounted at /api/...):
    GET    /subscription_metadata                  → list all BOMs
    POST   /subscription_metadata                  → create a new BOM (JSON body),
                                                       server allocates the bom_id
    GET    /subscription_metadata/{bom_id}         → fetch one (404 if absent)
    PUT    /subscription_metadata/{bom_id}         → update existing BOM from JSON
    DELETE /subscription_metadata/{bom_id}         → delete (204 even if absent)

POST/PUT body (JSON):
    {
        "subscription_id": "...guid...",           // required on POST unless subscription_ids is provided
        "subscription_ids": ["...guid...", ...],   // optional multi-sub list; first is primary
        "tag": "Avaya Prod East",                  // optional
        "customer_name": "Avaya",                  // optional
        "customer_segments": "EA,ANY",             // optional CSV
        "required_skus": [{...}, ...],             // list of family dicts
        "services": [{"name": "Azure Automation"}, ...],   // names from catalog
        "regions": ["eastus", ...]                 // optional
    }

All inputs are validated by ``bom_storage`` before any I/O. Mutating
verbs go through ``csrf.assert_safe_origin`` since the app is anonymous
local-only.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from .._shared import httpfunc as func

from .._shared import activity_log, auth, bom_storage, csrf

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
    bom_id: Optional[str] = req.route_params.get("bom_id")
    principal = auth.get_local_user(req)

    def _log(event_type: str, status: str, message: str, *,
             subscription_id: Optional[str] = None, details=None) -> None:
        activity_log.record(
            event_type,
            actor_email=principal.email,
            actor_oid=principal.oid,
            subscription_id=subscription_id,
            api_scope="local",
            status=status,
            message=message,
            details=details,
        )

    if method != "GET":
        try:
            csrf.assert_safe_origin(req)
        except csrf.OriginError as ex:
            log.warning("origin check rejected subscription_metadata: %s", ex)
            _log(
                "subscription_metadata_update" if method in ("POST", "PUT") else "subscription_metadata_delete",
                "error",
                "BOM change rejected by origin policy",
                details={"bom_id": bom_id, "method": method},
            )
            return _err("origin_rejected", str(ex), 403)

    try:
        if method == "GET":
            if not bom_id:
                items = bom_storage.list_all()
                return _ok({"items": items})
            rec = bom_storage.get(bom_id)
            if rec is None:
                return _err("not_found", f"No BOM with id {bom_id}.", 404)
            return _ok(rec)

        if method == "POST":
            # Create a brand-new BOM (server allocates the bom_id). Reusing the
            # same subscription is fine — each POST yields a distinct BOM.
            if bom_id:
                _log(
                    "subscription_metadata_update",
                    "error",
                    "BOM create rejected: bom_id was provided in route",
                    details={"bom_id": bom_id, "action": "create"},
                )
                return _err("unexpected_bom_id",
                            "POST creates a new BOM; do not include a bom_id in "
                            "the route. Use PUT /{bom_id} to update.", 400)
            body = _parse_body(req)
            if isinstance(body, func.HttpResponse):
                _log(
                    "subscription_metadata_update",
                    "error",
                    "BOM create rejected: invalid JSON body",
                    details={"action": "create"},
                )
                return body
            sub_ids = body.get("subscription_ids")
            sub = (body.get("subscription_id") or "").strip()
            if not sub and isinstance(sub_ids, list) and sub_ids:
                sub = str(sub_ids[0] or "").strip()
            if not sub:
                _log(
                    "subscription_metadata_update",
                    "error",
                    "BOM create rejected: subscription_id missing",
                    details={"action": "create", "bom_id": bom_id},
                )
                return _err("missing_subscription_id",
                            "POST body must include subscription_id or subscription_ids.", 400)
            try:
                rec = bom_storage.create(
                    sub,
                    subscription_ids=sub_ids,
                    tag=body.get("tag"),
                    customer_name=body.get("customer_name"),
                    customer_segments=body.get("customer_segments"),
                    resilience=body.get("resilience"),
                    required_skus=body.get("required_skus") or [],
                    services=body.get("services") or [],
                    regions=body.get("regions") or [],
                    support_override=body.get("support_override") or {},
                    updated_by=principal.email,
                )
            except bom_storage.BomStorageError as ex:
                _log(
                    "subscription_metadata_update",
                    "error",
                    f"BOM create failed: {ex.code}",
                    subscription_id=sub,
                    details={"action": "create", "bom_id": bom_id, "code": ex.code},
                )
                return _err(ex.code, ex.message, ex.status)
            _log(
                "subscription_metadata_update",
                "ok",
                f"Created BOM {rec.get('tag') or rec.get('bom_id')}",
                subscription_id=rec.get("subscription_id"),
                details={
                    "action": "create",
                    "bom_id": rec.get("bom_id"),
                    "subscription_ids": rec.get("subscription_ids") or [rec.get("subscription_id")],
                    "service_count": len(rec.get("services") or []),
                    "region_count": len(rec.get("regions") or []),
                },
            )
            return _ok(rec, status=201)

        if method == "PUT":
            if not bom_id:
                _log(
                    "subscription_metadata_update",
                    "error",
                    "BOM update rejected: missing bom_id in route",
                    details={"action": "update"},
                )
                return _err("missing_bom_id",
                            "PUT requires the bom_id in the route. To create a "
                            "new BOM, POST /subscription_metadata instead.", 400)
            body = _parse_body(req)
            if isinstance(body, func.HttpResponse):
                _log(
                    "subscription_metadata_update",
                    "error",
                    "BOM update rejected: invalid JSON body",
                    details={"action": "update", "bom_id": bom_id},
                )
                return body
            # subscription_id may come from the body; legacy callers that key by
            # subscription default it to the route id.
            sub_ids = body.get("subscription_ids")
            sub = (body.get("subscription_id") or "").strip()
            if not sub and isinstance(sub_ids, list) and sub_ids:
                sub = str(sub_ids[0] or "").strip()
            sub = sub or bom_id
            try:
                rec = bom_storage.upsert(
                    bom_id,
                    subscription_id=sub,
                    subscription_ids=sub_ids,
                    tag=body.get("tag"),
                    customer_name=body.get("customer_name"),
                    customer_segments=body.get("customer_segments"),
                    resilience=body.get("resilience"),
                    required_skus=body.get("required_skus") or [],
                    services=body.get("services") or [],
                    regions=body.get("regions") or [],
                    support_override=body.get("support_override") or {},
                    updated_by=principal.email,
                )
            except bom_storage.BomStorageError as ex:
                _log(
                    "subscription_metadata_update",
                    "error",
                    f"BOM update failed: {ex.code}",
                    subscription_id=sub,
                    details={"action": "update", "bom_id": bom_id, "code": ex.code},
                )
                return _err(ex.code, ex.message, ex.status)
            _log(
                "subscription_metadata_update",
                "ok",
                f"Updated BOM {rec.get('tag') or rec.get('bom_id')}",
                subscription_id=rec.get("subscription_id"),
                details={
                    "action": "update",
                    "bom_id": rec.get("bom_id"),
                    "subscription_ids": rec.get("subscription_ids") or [rec.get("subscription_id")],
                    "service_count": len(rec.get("services") or []),
                    "region_count": len(rec.get("regions") or []),
                },
            )
            return _ok(rec)

        if method == "DELETE":
            if not bom_id:
                _log(
                    "subscription_metadata_delete",
                    "error",
                    "BOM delete rejected: missing bom_id in route",
                )
                return _err("missing_bom_id",
                            "DELETE requires the bom_id in the route.", 400)
            try:
                bom_storage.delete(bom_id)
            except bom_storage.BomStorageError as ex:
                _log(
                    "subscription_metadata_delete",
                    "error",
                    f"BOM delete failed: {ex.code}",
                    details={"bom_id": bom_id, "code": ex.code},
                )
                return _err(ex.code, ex.message, ex.status)
            _log(
                "subscription_metadata_delete",
                "ok",
                f"Deleted BOM {bom_id}",
                details={"bom_id": bom_id},
            )
            return func.HttpResponse(status_code=204)

        return _err("method_not_allowed", f"{method} not supported here.", 405)
    except Exception as ex:
        log.exception("subscription_metadata crash")
        if method != "GET":
            _log(
                "subscription_metadata_update" if method in ("POST", "PUT") else "subscription_metadata_delete",
                "error",
                "BOM change failed with an internal error",
                subscription_id=None,
                details={"bom_id": bom_id, "method": method},
            )
        return _err("internal_error",
                    f"Unhandled exception: {ex!r}", 500)


def _parse_body(req: func.HttpRequest):
    """Return the parsed JSON dict, or an HttpResponse describing the error."""
    try:
        body = req.get_json()
    except ValueError:
        return _err("bad_json", "Body is not valid JSON.", 400)
    if not isinstance(body, dict):
        return _err("bad_json", "Body must be a JSON object.", 400)
    return body

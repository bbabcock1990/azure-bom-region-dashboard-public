"""GET/POST /api/support/tickets/{ticket_name}

GET  — return the locally tracked ticket record.
POST — refresh a *submitted* ticket's status from Azure (needs an ARM token).
       Dry-run/preview tickets are returned as-is.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from .._shared import auth, auth_token, csrf, support_tickets
from .._shared import httpfunc as func

log = logging.getLogger(__name__)


def _ok(payload: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload), status_code=status, mimetype="application/json"
    )


def _err(code: str, message: str, status: int = 400, **extra: Any) -> func.HttpResponse:
    body = {"error": code, "message": message}
    body.update(extra)
    return func.HttpResponse(
        json.dumps(body), status_code=status, mimetype="application/json"
    )


async def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)
    ticket_name = str(req.route_params.get("ticket_name") or "").strip()
    if not ticket_name:
        return _err("bad_name", "ticket_name is required.", 400)

    if req.method == "GET":
        record = support_tickets.get_ticket(ticket_name)
        if record is None:
            return _err("not_found", "Ticket not found.", 404)
        return _ok({"ticket": record})

    # POST → refresh status from Azure
    try:
        csrf.assert_safe_origin(req)
    except csrf.OriginError as ex:
        return _err("origin_rejected", str(ex), 403)

    record = support_tickets.get_ticket(ticket_name)
    if record is None:
        return _err("not_found", "Ticket not found.", 404)
    if record.get("dry_run"):
        return _ok({"ticket": record})

    subscription_id = str(record.get("subscription_id") or "").lower()
    token_getter = getattr(
        auth_token, "get_arm_token_for_subscription", auth_token.get_arm_token
    )
    try:
        token_info = await asyncio.to_thread(token_getter, subscription_id)
    except auth_token.AuthError as ex:
        return _err("auth_error", ex.message, 401, auth_code=ex.code)

    try:
        refreshed = await asyncio.to_thread(
            support_tickets.refresh_status, ticket_name, token_info.token
        )
    except support_tickets.SupportError as ex:
        return _err(ex.code, ex.message, ex.status)
    return _ok({"ticket": refreshed})

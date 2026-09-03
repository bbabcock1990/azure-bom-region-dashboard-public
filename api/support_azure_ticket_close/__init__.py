"""POST /api/support/azure-tickets/close — close an Azure support ticket.

Request body::

    {
      "subscription_id": "<guid>",
      "ticket_name": "<azure support ticket name>"
    }

Closes the ticket via the ``Microsoft.Support`` ARM provider
(``PATCH … { "status": "Closed" }``). Works for both dashboard-created and
externally created tickets. Disabled in demo mode. Azure only allows closing a
ticket that is not actively assigned to an engineer; otherwise the ARM error is
surfaced to the caller.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

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


def _demo_mode() -> bool:
    return os.getenv("DEMO_MODE", "").strip().lower() in ("true", "1", "yes")


async def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)

    try:
        csrf.assert_safe_origin(req)
    except csrf.OriginError as ex:
        return _err("origin_rejected", str(ex), 403)

    if _demo_mode():
        return _err("demo_mode", "Closing tickets is disabled in demo mode.", 403)

    try:
        body = req.get_json()
    except ValueError:
        return _err("bad_json", "Body must be a JSON object.", 400)
    if not isinstance(body, dict):
        return _err("bad_json", "Body must be a JSON object.", 400)

    subscription_id = str(body.get("subscription_id") or "").strip().lower()
    ticket_name = str(body.get("ticket_name") or "").strip()
    if not subscription_id:
        return _err("bad_subscription", "subscription_id is required.", 400)
    if not ticket_name:
        return _err("bad_name", "ticket_name is required.", 400)

    token_getter = getattr(
        auth_token, "get_arm_token_for_subscription", auth_token.get_arm_token
    )
    try:
        token_info = await asyncio.to_thread(token_getter, subscription_id)
    except auth_token.AuthError as ex:
        return _err("auth_error", ex.message, 401, auth_code=ex.code)

    try:
        result = await asyncio.to_thread(
            support_tickets.close_azure_ticket,
            subscription_id,
            ticket_name,
            token_info.token,
        )
    except support_tickets.SupportError as ex:
        extra = {"details": ex.details} if getattr(ex, "details", None) is not None else {}
        return _err(ex.code, ex.message, ex.status, **extra)
    except Exception as ex:  # pragma: no cover - defensive
        log.exception("support ticket close failed")
        return _err("unexpected", f"Support ticket close failed: {ex!r}", 500)

    return _ok({"ticket": result}, status=200)

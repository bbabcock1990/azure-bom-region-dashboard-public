"""GET /api/support/azure-tickets?subscription_id=<guid>[&open_only=1][&limit=100]

Real-time list of Azure support tickets that already exist on a subscription
(via the ``Microsoft.Support`` ARM provider), independent of the tickets this
dashboard created locally. Requires an ARM token for the subscription.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from .._shared import auth, auth_token, support_tickets
from .._shared import httpfunc as func


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

    subscription_id = str(req.params.get("subscription_id") or "").strip().lower()
    if not subscription_id:
        return _err("bad_subscription", "subscription_id is required.", 400)

    open_only = str(req.params.get("open_only") or "1").lower() not in ("0", "false", "no")
    try:
        limit = int(req.params.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100

    token_getter = getattr(
        auth_token, "get_arm_token_for_subscription", auth_token.get_arm_token
    )
    try:
        token_info = await asyncio.to_thread(token_getter, subscription_id)
    except auth_token.AuthError as ex:
        return _err("auth_error", ex.message, 401, auth_code=ex.code)

    try:
        tickets = await asyncio.to_thread(
            support_tickets.list_azure_tickets,
            subscription_id,
            token_info.token,
            open_only=open_only,
            limit=limit,
        )
    except support_tickets.SupportError as ex:
        return _err(ex.code, ex.message, ex.status)

    return _ok({"tickets": tickets, "subscription_id": subscription_id})

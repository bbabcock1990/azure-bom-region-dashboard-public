"""POST /api/support/tickets — create (or preview) an Azure support ticket for a
BOM deployment blocker.

Request body::

    {
      "kind": "quota" | "technical",
      "subscription_id": "<guid>",
      "region": "eastus",
      "family": "standardDav6Family",
      "family_label": "Dadv6",          # optional display label
      "new_limit": 500,                  # required for kind=quota
      "zones": ["1","2"],               # optional, kind=technical
      "severity": "moderate",           # optional; defaults to Support settings
      "detail": "…",                    # optional free text
      "bom_id": "…",                    # optional correlation
      "dry_run": true                    # default true — no Azure call
    }

Dry-run (the default, and always in demo mode) builds and returns the exact ARM
request without contacting Azure. ``dry_run: false`` submits the real ticket via
``Microsoft.Support`` using the app's ARM token.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
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


def _demo_mode() -> bool:
    return os.getenv("DEMO_MODE", "").strip().lower() in ("true", "1", "yes")


async def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)

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

    kind = str(body.get("kind") or "").strip().lower()
    subscription_id = str(body.get("subscription_id") or "").strip().lower()
    region = str(body.get("region") or "").strip().lower()
    family = str(body.get("family") or "").strip()
    family_label = body.get("family_label")
    zones = body.get("zones") if isinstance(body.get("zones"), list) else None
    severity = body.get("severity")
    detail = body.get("detail")
    bom_id = body.get("bom_id")
    dry_run = bool(body.get("dry_run", True))
    demo = _demo_mode()
    try:
        new_limit = int(body.get("new_limit") or 0)
    except (TypeError, ValueError):
        new_limit = 0

    token: Optional[str] = None
    if not dry_run and not demo:
        token_getter = getattr(
            auth_token, "get_arm_token_for_subscription", auth_token.get_arm_token
        )
        try:
            token_info = await asyncio.to_thread(token_getter, subscription_id)
            token = token_info.token
        except auth_token.AuthError as ex:
            return _err("auth_error", ex.message, 401, auth_code=ex.code)

    try:
        result = await asyncio.to_thread(
            support_tickets.create_ticket,
            kind=kind,
            subscription_id=subscription_id,
            region=region,
            family=family,
            family_label=family_label,
            new_limit=new_limit,
            zones=zones,
            severity=severity,
            detail=detail,
            bom_id=bom_id,
            dry_run=dry_run,
            token=token,
            demo_mode=demo,
        )
    except support_tickets.SupportError as ex:
        return _err(ex.code, ex.message, ex.status)
    except Exception as ex:  # pragma: no cover - defensive
        log.exception("support ticket create failed")
        return _err("unexpected", f"Support ticket create failed: {ex!r}", 500)

    return _ok({"ticket": result}, status=200)

"""POST /api/providers/register

Register an Azure resource provider on a subscription so its regional
availability can be evaluated. Azure resource providers are opt-in per
subscription; a provider that returns 404 on a provider-show is simply
un-registered (availability unknown), NOT unavailable.

Request body:
    {
        "subscription_id": "...",
        "provider": "Microsoft.ContainerStorage"
    }

On success ARM starts an asynchronous registration (state Registering →
Registered). If the caller's identity lacks the
``{ns}/register/action`` permission ARM returns 403; we surface that with a
copy-paste ``az provider register`` fallback command so the user (or their
subscription owner) can complete it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional
from urllib.parse import quote

import httpx

from .._shared import auth, auth_token, csrf
from .._shared import httpfunc as func
from .._shared import activity_log

log = logging.getLogger(__name__)

GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
PROVIDER_RE = re.compile(r"^Microsoft(?:\.[A-Za-z0-9]+)+$")
API_VERSION = "2021-04-01"
ARM_BASE = "https://management.azure.com"


def _err(code: str, message: str, status: int = 400, **extra: Any) -> func.HttpResponse:
    payload = {"error": code, "message": message}
    payload.update(extra)
    return func.HttpResponse(
        json.dumps(payload), status_code=status, mimetype="application/json"
    )


def _ok(payload: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload), status_code=status, mimetype="application/json"
    )


def _parse_request_body(req: func.HttpRequest) -> Optional[dict]:
    try:
        body = req.get_json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _extract_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("details")
            if message:
                return str(message)
        if payload.get("message"):
            return str(payload["message"])
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    return fallback


async def main(req: func.HttpRequest) -> func.HttpResponse:
    _principal = auth.get_local_user(req)  # kept for parity / future logging

    try:
        csrf.assert_safe_origin(req)
    except csrf.OriginError as ex:
        log.warning("origin check rejected provider registration request: %s", ex)
        return _err("origin_rejected", str(ex), 403)

    body = _parse_request_body(req)
    if body is None:
        return _err("bad_json", "Body must be a JSON object.", 400)

    subscription_id = str(body.get("subscription_id") or "").strip().lower()
    provider = str(body.get("provider") or "").strip()

    if not GUID_RE.match(subscription_id):
        return _err("bad_subscription", "subscription_id must be a GUID.", 400)
    if not PROVIDER_RE.match(provider):
        return _err(
            "bad_provider",
            "provider must be a resource provider namespace, e.g. Microsoft.ContainerStorage.",
            400,
        )

    cli_command = f"az provider register --namespace {provider} --subscription {subscription_id}"

    token_getter = getattr(
        auth_token, "get_arm_token_for_subscription", auth_token.get_arm_token
    )
    try:
        token_info = await asyncio.to_thread(token_getter, subscription_id)
    except auth_token.AuthError as ex:
        return _err(
            "auth_error", ex.message, 401, auth_code=ex.code, cli_command=cli_command
        )

    token = getattr(token_info, "token", None) or str(token_info)
    url = (
        f"{ARM_BASE}/subscriptions/{subscription_id}/providers/"
        f"{quote(provider, safe='')}/register"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "azure-bom-region-dashboard/1.0",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0, http2=False) as client:
            activity_log.record(
                event_type="provider_register_start",
                api_scope="subscription",
                subscription_id=subscription_id,
                message=f"Register resource provider: {provider}",
                details={"provider": provider},
            )
            resp = await client.post(
                url, params={"api-version": API_VERSION}, headers=headers
            )
    except Exception as ex:
        log.exception("provider registration request failed unexpectedly")
        return _err(
            "request_failed",
            f"Provider registration failed: {ex!r}",
            502,
            cli_command=cli_command,
        )

    try:
        response_payload: Any = resp.json()
    except Exception:
        response_payload = resp.text

    if resp.status_code >= 400:
        # 403 = the identity can't self-register; hand off the CLI command.
        code = "forbidden" if resp.status_code == 403 else "register_failed"
        activity_log.record(
            event_type="provider_register_failed",
            api_scope="subscription",
            subscription_id=subscription_id,
            status="error",
            message=f"Provider registration FAILED: {provider} (HTTP {resp.status_code})",
            details={"provider": provider, "status_code": resp.status_code},
        )
        return _err(
            code,
            _extract_message(
                response_payload,
                f"Provider registration failed with status {resp.status_code}.",
            ),
            resp.status_code,
            provider=provider,
            azure_status_code=resp.status_code,
            cli_command=cli_command,
            response=response_payload,
        )

    registration_state = None
    if isinstance(response_payload, dict):
        registration_state = response_payload.get("registrationState")

    activity_log.record(
        event_type="provider_register_ok",
        api_scope="subscription",
        subscription_id=subscription_id,
        message=f"Provider registration started: {provider} ({registration_state or 'Registering'})",
        details={"provider": provider, "registration_state": registration_state},
    )
    return _ok(
        {
            "status": "registering",
            "provider": provider,
            "registration_state": registration_state or "Registering",
            "azure_status_code": resp.status_code,
            "cli_command": cli_command,
            "response": response_payload,
        },
        status=resp.status_code,
    )

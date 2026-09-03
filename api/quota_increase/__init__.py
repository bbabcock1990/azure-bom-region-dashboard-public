"""POST /api/quota/request-increase

Request body:
    {
        "subscription_id": "...",
        "region": "eastus",
        "family": "standardDav6Family",
        "new_limit": 500
    }
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
API_VERSION = "2023-09-01"
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


def _extract_request_id(payload: Any, headers: httpx.Headers) -> Optional[str]:
    for key in ("x-ms-request-id", "x-ms-correlation-request-id"):
        value = headers.get(key)
        if value:
            return value
    if isinstance(payload, dict):
        for key in ("id", "name"):
            value = payload.get(key)
            if value:
                return str(value)
    return None


async def main(req: func.HttpRequest) -> func.HttpResponse:
    _principal = auth.get_local_user(req)  # kept for parity / future logging

    try:
        csrf.assert_safe_origin(req)
    except csrf.OriginError as ex:
        log.warning("origin check rejected quota increase request: %s", ex)
        return _err("origin_rejected", str(ex), 403)

    body = _parse_request_body(req)
    if body is None:
        return _err("bad_json", "Body must be a JSON object.", 400)

    subscription_id = str(body.get("subscription_id") or "").strip().lower()
    region = str(body.get("region") or "").strip().lower()
    family = str(body.get("family") or "").strip()
    try:
        new_limit = int(body.get("new_limit"))
    except (TypeError, ValueError):
        new_limit = 0

    if not GUID_RE.match(subscription_id):
        return _err("bad_subscription", "subscription_id must be a GUID.", 400)
    if not region:
        return _err("bad_region", "region is required.", 400)
    if not family:
        return _err("bad_family", "family is required.", 400)
    if new_limit <= 0:
        return _err("bad_limit", "new_limit must be a positive integer.", 400)

    token_getter = getattr(auth_token, "get_arm_token_for_subscription", auth_token.get_arm_token)
    try:
        token_info = await asyncio.to_thread(token_getter, subscription_id)
    except auth_token.AuthError as ex:
        return _err("auth_error", ex.message, 401, auth_code=ex.code)

    scope = f"subscriptions/{subscription_id}/providers/Microsoft.Compute/locations/{quote(region, safe='')}"
    url = (
        f"{ARM_BASE}/{scope}/providers/Microsoft.Quota/"
        f"quotas/{quote(family, safe='')}"
    )
    payload = {
        "properties": {
            "limit": {
                "limitObjectType": "LimitValue",
                "limitType": "Independent",
                "value": new_limit,
            },
            "name": {"value": family},
        }
    }
    headers = {
        "Authorization": f"Bearer {token_info.token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "azure-bom-region-dashboard/1.0",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0, http2=False) as client:
            activity_log.record(
                event_type="quota_request_start",
                api_scope="subscription",
                subscription_id=subscription_id,
                message=f"Quota increase request: {family} → {new_limit} vCPUs in {region}",
                details={"family": family, "region": region, "new_limit": new_limit},
            )
            resp = await client.put(
                url,
                params={"api-version": API_VERSION},
                headers=headers,
                json=payload,
            )
    except Exception as ex:
        log.exception("quota increase request failed unexpectedly")
        return _err("request_failed", f"Quota increase request failed: {ex!r}", 502)

    try:
        response_payload: Any = resp.json()
    except Exception:
        response_payload = resp.text

    request_id = _extract_request_id(response_payload, resp.headers)
    if resp.status_code >= 400:
        activity_log.record(
            event_type="quota_request_failed",
            api_scope="subscription",
            subscription_id=subscription_id,
            status="error",
            message=f"Quota increase FAILED: {family} in {region} (HTTP {resp.status_code})",
            details={"family": family, "region": region, "new_limit": new_limit,
                     "status_code": resp.status_code, "request_id": request_id},
        )
        return _err(
            "quota_request_failed",
            _extract_message(
                response_payload,
                f"Quota increase request failed with status {resp.status_code}.",
            ),
            resp.status_code,
            azure_status_code=resp.status_code,
            request_id=request_id,
            response=response_payload,
        )

    status = "pending" if resp.status_code == 202 else "success"
    activity_log.record(
        event_type="quota_request_ok",
        api_scope="subscription",
        subscription_id=subscription_id,
        message=f"Quota increase submitted: {family} → {new_limit} vCPUs in {region} ({status})",
        details={"family": family, "region": region, "new_limit": new_limit,
                 "status": status, "request_id": request_id,
                 "azure_status_code": resp.status_code},
    )
    return _ok(
        {
            "status": status,
            "request_id": request_id,
            "azure_status_code": resp.status_code,
            "response": response_payload,
        },
        status=resp.status_code,
    )

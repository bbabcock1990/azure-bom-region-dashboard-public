"""GET /api/az/resource-groups?subscription_id=<guid>

Lists the resource groups in a subscription so the Settings UI can offer a
**picker** for the deep-check validation resource group (rather than a free-text
box where a typo silently 404s at validate time). Read-only; creates nothing.
"""
from __future__ import annotations

import json
import logging

import httpx

from .._shared import httpfunc as func
from .._shared import auth_token

log = logging.getLogger(__name__)

ARM_BASE = "https://management.azure.com"
RG_API_VERSION = "2021-04-01"


def _err(code: str, message: str, status: int = 400) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": code, "message": message}),
        status_code=status, mimetype="application/json",
    )


def main(req: func.HttpRequest) -> func.HttpResponse:
    subscription_id = str(req.params.get("subscription_id") or "").strip()
    if not subscription_id:
        return _err("no_subscription", "subscription_id query parameter is required.", 400)

    try:
        token_info = auth_token.get_arm_token(subscription_id)
    except auth_token.AuthError as ex:
        status = 401 if ex.code == "not_signed_in" else 502
        return _err(ex.code, ex.message, status)

    groups = []
    url = f"{ARM_BASE}/subscriptions/{subscription_id}/resourcegroups"
    params = {"api-version": RG_API_VERSION}
    headers = {"Authorization": f"Bearer {token_info.token}", "Accept": "application/json"}
    try:
        next_url = url
        with httpx.Client(timeout=20.0, http2=False) as client:
            while next_url:
                r = client.get(next_url, params=params if next_url == url else None, headers=headers)
                if r.status_code in (401, 403):
                    return _err("forbidden",
                                "Not authorized to list resource groups in this subscription.", 403)
                if r.status_code >= 400:
                    return _err("arm_error", f"ARM returned {r.status_code} listing resource groups.", 502)
                data = r.json() or {}
                for g in data.get("value") or []:
                    name = g.get("name")
                    if name:
                        groups.append({"name": name, "location": g.get("location") or ""})
                next_url = data.get("nextLink") or None
    except Exception as ex:  # pragma: no cover - defensive
        log.warning("resource-group list failed: %r", ex)
        return _err("request_failed", "Could not list resource groups.", 502)

    groups.sort(key=lambda x: x["name"].lower())
    return func.HttpResponse(
        json.dumps({"subscription_id": subscription_id, "resource_groups": groups}),
        status_code=200, mimetype="application/json",
    )

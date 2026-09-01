"""GET/POST /api/az/resource-groups

GET  ?subscription_id=<guid>
    Lists the resource groups in a subscription so the Settings UI can offer a
    **picker** for the deep-check validation resource group (rather than a
    free-text box where a typo silently 404s at validate time). Read-only.

POST {subscription_id, name, location}
    Creates the named resource group if it does not already exist (idempotent).
    A resource group is free and holds nothing until resources are deployed into
    it — the deep check only ever *validates* against it, never deploys. This is
    the one write this feature performs, and only on explicit user action.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from .._shared import httpfunc as func
from .._shared import auth_token, csrf, activity_log

log = logging.getLogger(__name__)

ARM_BASE = "https://management.azure.com"
RG_API_VERSION = "2021-04-01"

# ARM resource-group name rules: 1-90 chars, alphanumerics, unicode, '.', '_',
# '-', '(' , ')'; cannot end with a period. Keep a conservative ASCII subset.
_RG_NAME_RE = re.compile(r"^[A-Za-z0-9._()\-]{1,90}$")


def _err(code: str, message: str, status: int = 400) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": code, "message": message}),
        status_code=status, mimetype="application/json",
    )


def _ok(payload: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(payload), status_code=status, mimetype="application/json")


def _token(subscription_id: str):
    return auth_token.get_arm_token(subscription_id)


def _list_groups(subscription_id: str, token: str) -> func.HttpResponse:
    groups = []
    url = f"{ARM_BASE}/subscriptions/{subscription_id}/resourcegroups"
    params = {"api-version": RG_API_VERSION}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
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
    return _ok({"subscription_id": subscription_id, "resource_groups": groups})


def _create_group(subscription_id: str, token: str, name: str, location: str) -> func.HttpResponse:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json",
               "Content-Type": "application/json"}
    base = f"{ARM_BASE}/subscriptions/{subscription_id}/resourcegroups/{name}"
    params = {"api-version": RG_API_VERSION}
    try:
        with httpx.Client(timeout=30.0, http2=False) as client:
            # Idempotent: if it already exists, return it without changing location.
            existing = client.get(base, params=params, headers=headers)
            if existing.status_code == 200:
                loc = (existing.json() or {}).get("location") or location
                return _ok({"created": False, "name": name, "location": loc,
                            "message": "Resource group already exists."})
            if existing.status_code in (401, 403):
                return _err("forbidden", "Not authorized to manage resource groups in this subscription.", 403)

            r = client.put(base, params=params, headers=headers, json={"location": location})
            if r.status_code in (401, 403):
                return _err("forbidden",
                            "Not authorized to create a resource group in this subscription.", 403)
            if r.status_code >= 400:
                detail = ""
                try:
                    detail = ((r.json() or {}).get("error") or {}).get("message") or ""
                except Exception:
                    detail = r.text or ""
                return _err("create_failed",
                            f"Could not create resource group: {detail[:240] or r.status_code}", 502)
            created = r.json() or {}
    except Exception as ex:  # pragma: no cover - defensive
        log.warning("resource-group create failed: %r", ex)
        return _err("request_failed", "Could not create resource group.", 502)

    try:
        activity_log.record(event_type="validation_rg_create", api_scope="arm",
                            subscription_id=subscription_id,
                            message=f"Created validation resource group '{name}' in {location}")
    except Exception:
        pass
    return _ok({"created": True, "name": created.get("name") or name,
                "location": created.get("location") or location,
                "message": "Resource group created."}, status=201)


def main(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "POST":
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
        subscription_id = str(body.get("subscription_id") or "").strip()
        name = str(body.get("name") or "").strip()
        location = str(body.get("location") or "").strip().lower().replace(" ", "")
        if not subscription_id:
            return _err("no_subscription", "subscription_id is required.", 400)
        if not name or not _RG_NAME_RE.match(name) or name.endswith("."):
            return _err("bad_name", "Provide a valid resource group name (letters, numbers, . _ - () ).", 400)
        if not location:
            return _err("no_location", "A location is required to create a resource group.", 400)
        try:
            token_info = _token(subscription_id)
        except auth_token.AuthError as ex:
            status = 401 if ex.code == "not_signed_in" else 502
            return _err(ex.code, ex.message, status)
        return _create_group(subscription_id, token_info.token, name, location)

    subscription_id = str(req.params.get("subscription_id") or "").strip()
    if not subscription_id:
        return _err("no_subscription", "subscription_id query parameter is required.", 400)
    try:
        token_info = _token(subscription_id)
    except auth_token.AuthError as ex:
        status = 401 if ex.code == "not_signed_in" else 502
        return _err(ex.code, ex.message, status)
    return _list_groups(subscription_id, token_info.token)

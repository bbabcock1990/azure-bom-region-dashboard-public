"""GET /api/quota/request-status

Query parameters:
    subscription_id : GUID
    region          : Azure region short name
    family          : VM family identifier
    requested_limit : positive integer
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional
from urllib.parse import quote

import httpx

from .._shared import auth, auth_token
from .._shared import httpfunc as func
from .._shared import activity_log

log = logging.getLogger(__name__)

GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
USAGE_API_VERSION = "2023-09-01"
QUOTA_API_VERSION = "2023-06-01-preview"
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


def _pick_usage_name(obj: dict) -> Optional[str]:
    name = obj.get("name")
    if isinstance(name, dict):
        for key in ("value", "localizedValue"):
            value = name.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def _as_number(value) -> Optional[float]:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except Exception:
        return None


def _as_int(value) -> Optional[int]:
    number = _as_number(value)
    if number is None:
        return None
    return int(number) if float(number).is_integer() else int(round(number))


def _find_family_limit(payload: Any, family: str) -> Optional[int]:
    items = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None
    family_key = str(family or "").strip().lower()
    for item in items:
        if not isinstance(item, dict):
            continue
        name_value = _pick_usage_name(item)
        if str(name_value or "").strip().lower() != family_key:
            continue
        return _as_int(item.get("limit"))
    return None


def _check_quota_request_status(
    client: httpx.Client, headers: dict, subscription_id: str, region: str, family: str
) -> Optional[str]:
    """Query Microsoft.Quota quotaRequests API for the most recent request's provisioning state.

    Returns one of: "Succeeded", "Failed", "InProgress", or None if not determinable.
    """
    scope = (
        f"subscriptions/{subscription_id}/providers/"
        f"Microsoft.Compute/locations/{quote(region, safe='')}"
    )
    url = f"{ARM_BASE}/{scope}/providers/Microsoft.Quota/quotaRequests"
    try:
        resp = client.get(url, params={"api-version": QUOTA_API_VERSION, "$top": "5"}, headers=headers)
        if resp.status_code >= 400:
            log.debug("Quota requests list returned %d", resp.status_code)
            return None
        data = resp.json()
        items = data.get("value") or []
        family_lower = family.strip().lower()
        # Find the most recent request for this family
        for item in items:
            props = item.get("properties") or {}
            # Check sub-requests for matching family
            for sub_req in (props.get("value") or []):
                sub_props = sub_req.get("properties") or {}
                name_obj = sub_props.get("name") or {}
                name_val = str(name_obj.get("value") or "").strip().lower()
                if name_val == family_lower:
                    state = props.get("provisioningState")
                    if isinstance(state, str) and state.strip():
                        return state.strip()
            # If sub-requests don't have family details, check the top-level name
            top_name = str(item.get("name") or "").strip().lower()
            if not top_name:
                continue
            state = props.get("provisioningState")
            message = str(props.get("message") or "").lower()
            if family_lower in message or family_lower in top_name:
                if isinstance(state, str) and state.strip():
                    return state.strip()
        # If no family-specific match found, check most recent request state anyway
        if items:
            props = items[0].get("properties") or {}
            state = props.get("provisioningState")
            if isinstance(state, str) and state.strip():
                return state.strip()
    except Exception as ex:
        log.debug("Quota request status check failed: %s", ex)
    return None


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)

    subscription_id = str(req.params.get("subscription_id") or "").strip().lower()
    region = str(req.params.get("region") or "").strip().lower()
    family = str(req.params.get("family") or "").strip()
    try:
        requested_limit = int(req.params.get("requested_limit"))
    except (TypeError, ValueError):
        requested_limit = 0

    if not GUID_RE.match(subscription_id):
        return _err("bad_subscription", "subscription_id must be a GUID.", 400)
    if not region:
        return _err("bad_region", "region is required.", 400)
    if not family:
        return _err("bad_family", "family is required.", 400)
    if requested_limit <= 0:
        return _err("bad_requested_limit", "requested_limit must be a positive integer.", 400)

    token_getter = getattr(auth_token, "get_arm_token_for_subscription", auth_token.get_arm_token)
    try:
        token_info = token_getter(subscription_id)
    except auth_token.AuthError as ex:
        return _err("auth_error", ex.message, 401, auth_code=ex.code)

    token = token_info.token
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "azure-bom-region-dashboard/1.0",
    }

    try:
        with httpx.Client(timeout=30.0, http2=False) as client:
            # First: check the Microsoft.Quota API for provisioning state
            provisioning_state = _check_quota_request_status(
                client, headers, subscription_id, region, family
            )

            # If we got a definitive failure, return immediately
            if provisioning_state and provisioning_state.lower() == "failed":
                return _ok({
                    "status": "failed",
                    "current_limit": None,
                    "requested_limit": requested_limit,
                    "provisioning_state": provisioning_state,
                    "message": "Quota increase request was denied by Azure. Open a support ticket in the Azure portal to request this increase.",
                })

            # Second: check current usage limit
            url = (
                f"{ARM_BASE}/subscriptions/{subscription_id}/providers/"
                f"Microsoft.Compute/locations/{region}/usages"
            )
            resp = client.get(url, params={"api-version": USAGE_API_VERSION}, headers=headers)
    except Exception as ex:
        log.exception("quota request status lookup failed unexpectedly")
        return _err("request_failed", f"Quota status lookup failed: {ex!r}", 502)

    try:
        payload: Any = resp.json()
    except Exception:
        payload = resp.text

    if resp.status_code >= 400:
        return _err(
            "quota_status_failed",
            f"Quota status lookup failed with status {resp.status_code}.",
            resp.status_code,
            azure_status_code=resp.status_code,
            response=payload,
        )

    current_limit = _find_family_limit(payload, family)
    status = "unknown"
    if current_limit is not None:
        status = "approved" if current_limit >= requested_limit else "pending"

    result: dict = {
        "status": status,
        "current_limit": current_limit,
        "requested_limit": requested_limit,
    }
    if provisioning_state:
        result["provisioning_state"] = provisioning_state
        # If provisioning says Succeeded but limit has not propagated yet, trust the API
        if provisioning_state.lower() == "succeeded":
            if status == "pending":
                result["status"] = "approved"
            # Use requested_limit as current if Usage API hasn't caught up
            if current_limit is not None and current_limit < requested_limit:
                result["current_limit"] = requested_limit

    activity_log.record(
        event_type="quota_status_check",
        api_scope="subscription",
        subscription_id=subscription_id,
        message=f"Quota status check: {family} in {region} → {result['status']}",
        details={"family": family, "region": region, "status": result["status"],
                 "current_limit": result.get("current_limit"),
                 "requested_limit": requested_limit},
    )

    return _ok(result)

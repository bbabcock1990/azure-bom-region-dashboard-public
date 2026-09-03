"""GET /api/permissions/check?subscription_id=<guid>

Reads the **signed-in user's effective permissions** at the subscription scope
and evaluates them against the set of ARM actions the BOM tool actually needs.
The result powers the Settings → Permissions blade, which shows a "Verified" vs
"Check" state per capability so a customer can confirm — before they rely on the
dashboard — whether their account can perform every read (and optional write)
the tool may attempt.

This is entirely **read-only**: it lists the caller's own permissions via
``Microsoft.Authorization/permissions`` (a right every role, including Reader,
already has) and matches action strings locally. It never mutates anything.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from .._shared import httpfunc as func
from .._shared import auth_token

log = logging.getLogger(__name__)

ARM_BASE = "https://management.azure.com"
PERMISSIONS_API_VERSION = "2022-04-01"

# The capabilities the dashboard exercises, mapped to representative ARM control-
# plane actions. `required` capabilities must pass for the core analysis (region
# readiness, quota, SKU/zonal availability) to work; the rest unlock optional
# automation (deployment pre-flight, quota increases, support tickets). Every
# action in `actions` must be permitted for a capability to be "granted".
CAPABILITIES = [
    {
        "key": "list_subscriptions",
        "title": "List subscriptions",
        "why": "Populate the subscription picker and target the analysis at your subscription.",
        "required": True,
        "actions": ["Microsoft.Resources/subscriptions/read"],
    },
    {
        "key": "read_skus",
        "title": "Read compute SKUs & region capabilities",
        "why": "Determine which VM families / SKUs and availability zones exist in each region.",
        "required": True,
        "actions": ["Microsoft.Compute/skus/read"],
    },
    {
        "key": "read_usage",
        "title": "Read compute quota & usage",
        "why": "Compare the BOM's vCPU needs against your current quota per region (Quota tab).",
        "required": True,
        "actions": ["Microsoft.Compute/locations/usages/read"],
    },
    {
        "key": "read_resource_groups",
        "title": "List resource groups",
        "why": "Offer a picker for the optional deployment-validation resource group.",
        "required": False,
        "actions": ["Microsoft.Resources/subscriptions/resourceGroups/read"],
    },
    {
        "key": "read_quota_service",
        "title": "Read Quota service limits & requests",
        "why": "Read current limits and the status of any quota-increase requests you file.",
        "required": False,
        "actions": [
            "Microsoft.Quota/quotas/read",
            "Microsoft.Quota/quotaRequests/read",
        ],
    },
    {
        "key": "register_providers",
        "title": "Register resource providers",
        "why": "Auto-register a resource provider (e.g. Microsoft.Compute) when it isn't yet registered.",
        "required": False,
        "actions": ["Microsoft.Compute/register/action"],
    },
    {
        "key": "create_resource_group",
        "title": "Create validation resource group",
        "why": "Create the free, empty resource group used only for deployment pre-flight.",
        "required": False,
        "actions": ["Microsoft.Resources/subscriptions/resourceGroups/write"],
    },
    {
        "key": "deployment_preflight",
        "title": "Deployment pre-flight (deep check)",
        "why": "Run ARM validate/what-if to confirm a SKU can actually deploy — creates nothing.",
        "required": False,
        "actions": ["Microsoft.Resources/deployments/write"],
    },
    {
        "key": "request_quota_increase",
        "title": "Request quota increases",
        "why": "File quota-increase requests directly from the Quota tab.",
        "required": False,
        "actions": ["Microsoft.Quota/quotas/write"],
    },
    {
        "key": "manage_support_tickets",
        "title": "Create & track support tickets",
        "why": "Open, read, and update Azure support tickets from the Remediation tab.",
        "required": False,
        "actions": [
            "Microsoft.Support/supportTickets/write",
            "Microsoft.Support/supportTickets/read",
        ],
    },
]


def _err(code: str, message: str, status: int = 400) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": code, "message": message}),
        status_code=status, mimetype="application/json",
    )


def _ok(payload: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(payload), status_code=status, mimetype="application/json")


def _pattern_to_regex(pattern: str) -> re.Pattern:
    """Convert an ARM action pattern (which may contain '*') to a full-match,
    case-insensitive regex. '*' matches any run of characters including '/'."""
    escaped = re.escape(pattern).replace(r"\*", ".*")
    return re.compile("^" + escaped + "$", re.IGNORECASE)


def _action_allowed(action: str, permission_sets: list) -> bool:
    """Standard RBAC evaluation for a single management action string: the action
    must match an ``actions`` entry of some permission set and not be subtracted
    by that same set's ``notActions``. (dataActions are a separate plane and none
    of the capabilities here are data actions.)"""
    for perm in permission_sets:
        acts = perm.get("actions") or []
        not_acts = perm.get("notActions") or []
        if any(_pattern_to_regex(a).match(action) for a in acts):
            if not any(_pattern_to_regex(n).match(action) for n in not_acts):
                return True
    return False


def _fetch_permissions(subscription_id: str, token: str):
    """Return (permission_sets, error_response). permission_sets is a list of
    {actions, notActions, dataActions, notDataActions} dicts for the caller."""
    url = (f"{ARM_BASE}/subscriptions/{subscription_id}"
           f"/providers/Microsoft.Authorization/permissions")
    params = {"api-version": PERMISSIONS_API_VERSION}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    sets = []
    try:
        next_url = url
        with httpx.Client(timeout=20.0, http2=False) as client:
            while next_url:
                r = client.get(next_url, params=params if next_url == url else None, headers=headers)
                if r.status_code in (401, 403):
                    return None, _err(
                        "forbidden",
                        "Not authorized to read your own permissions on this subscription. "
                        "You likely lack any role assignment here.", 403)
                if r.status_code >= 400:
                    return None, _err(
                        "arm_error",
                        f"ARM returned {r.status_code} reading permissions.", 502)
                data = r.json() or {}
                for item in data.get("value") or []:
                    sets.append({
                        "actions": item.get("actions") or [],
                        "notActions": item.get("notActions") or [],
                        "dataActions": item.get("dataActions") or [],
                        "notDataActions": item.get("notDataActions") or [],
                    })
                next_url = data.get("nextLink") or None
    except Exception as ex:  # pragma: no cover - defensive
        log.warning("permissions read failed: %r", ex)
        return None, _err("request_failed", "Could not read permissions.", 502)
    return sets, None


def _evaluate(permission_sets: list) -> dict:
    results = []
    required_ok = required_total = optional_ok = optional_total = 0
    for cap in CAPABILITIES:
        missing = [a for a in cap["actions"] if not _action_allowed(a, permission_sets)]
        granted = not missing
        results.append({
            "key": cap["key"],
            "title": cap["title"],
            "why": cap["why"],
            "required": cap["required"],
            "actions": cap["actions"],
            "granted": granted,
            "missing": missing,
        })
        if cap["required"]:
            required_total += 1
            if granted:
                required_ok += 1
        else:
            optional_total += 1
            if granted:
                optional_ok += 1
    return {
        "capabilities": results,
        "summary": {
            "required_ok": required_ok,
            "required_total": required_total,
            "optional_ok": optional_ok,
            "optional_total": optional_total,
            "all_required_ok": required_ok == required_total,
        },
    }


def main(req: func.HttpRequest) -> func.HttpResponse:
    subscription_id = str(req.params.get("subscription_id") or "").strip()
    if not subscription_id:
        return _err("no_subscription", "subscription_id query parameter is required.", 400)

    try:
        token_info = auth_token.get_arm_token(subscription_id)
    except auth_token.AuthError as ex:
        status = 401 if ex.code == "not_signed_in" else 502
        return _err(ex.code, ex.message, status)

    permission_sets, err = _fetch_permissions(subscription_id, token_info.token)
    if err is not None:
        return err

    payload = _evaluate(permission_sets)
    payload["subscription_id"] = subscription_id
    return _ok(payload)

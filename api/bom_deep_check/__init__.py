"""POST /api/bom/deep-check

**Opt-in, non-destructive** deep deployability verification for the zone-redundant
service tiers a user selected, for services that expose no read-only capability
API (App Service, Redis, Service Bus, Event Hubs) plus advisory-only services
(Cosmos DB). Unlike ``/api/bom/zonal-capability`` (read-only, runs automatically),
this endpoint issues an ARM *template validation* — Resource Manager runs the
same pre-flight checks a real deployment would (quota, SKU, region offer) but
**creates nothing and costs nothing**.

Because ARM ``validate`` is resource-group scoped, the caller must have a
validation resource group configured (Settings → Ticket owner → Validation
resource group), or pass ``resource_group`` explicitly in the body. If neither
is present, per-service results come back ``no_resource_group`` so the UI can
prompt. The frontend gates this behind an explicit user action + confirmation.

Request body::

    {
      "subscription_id": "<sub guid>",
      "region": "eastus",
      "resource_group": "my-rg",          # optional; falls back to settings
      "services": [
        {"name": "Azure App Service", "tier": "premium_v3"},
        {"name": "Azure Cache for Redis", "tier": "premium"}
      ]
    }
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

from .._shared import auth, csrf, auth_token, support_settings, activity_log
from .._shared import httpfunc as func
from .._shared import bom_services, deploy_validation

log = logging.getLogger(__name__)

_MAX_SERVICES = 40


def _ok(payload: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(payload), status_code=status, mimetype="application/json")


def _err(code: str, message: str, status: int = 400) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": code, "message": message}), status_code=status, mimetype="application/json"
    )


def _zone_redundant_tier(name: str, tier: str) -> bool:
    for t in bom_services.tiers_for_service(name):
        if str(t.get("id")) == str(tier):
            return bool(t.get("zone_redundant"))
    return False


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

    region = str(body.get("region") or "").strip().lower()
    subscription_id = str(body.get("subscription_id") or "").strip()
    raw_services = body.get("services")
    if not region:
        return _err("no_region", "Body.region is required.", 400)
    if not isinstance(raw_services, list):
        return _err("no_services", "Body.services must be an array.", 400)

    # Resolve the validation RG: explicit body override, else saved setting.
    resource_group = str(body.get("resource_group") or "").strip()
    if not resource_group:
        try:
            resource_group = str(support_settings.get_settings().get("validation_resource_group") or "").strip()
        except Exception:
            resource_group = ""

    # Only zone-redundant tier selections that have a deep-check path.
    selections: List[Dict[str, str]] = []
    seen: set = set()
    for item in raw_services[:_MAX_SERVICES]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        tier = str(item.get("tier") or "").strip()
        if not name or not tier:
            continue
        if not deploy_validation.service_validate_kind(name):
            continue
        # Validate-kind services must be zone-redundant tiers; advisory services
        # (Cosmos) have no tiers so they pass through regardless.
        if name in deploy_validation._VALIDATE_SERVICES and not _zone_redundant_tier(name, tier):
            continue
        key = (name, tier)
        if key in seen:
            continue
        seen.add(key)
        selections.append({"name": name, "tier": tier})

    if not selections:
        return _ok({"region": region, "subscription_id": subscription_id,
                    "resource_group": resource_group, "results": []})

    if not subscription_id:
        return _ok({
            "region": region, "subscription_id": "", "resource_group": resource_group,
            "results": [
                {"name": s["name"], "tier": s["tier"], "checkable": True,
                 "verdict": "no_subscription",
                 "message": "Select a subscription to run the deep deployability check."}
                for s in selections
            ],
        })

    token_getter = getattr(auth_token, "get_arm_token_for_subscription", auth_token.get_arm_token)
    try:
        token_info = await asyncio.to_thread(token_getter, subscription_id)
    except auth_token.AuthError as ex:
        return _err("auth_error", str(ex), 401)

    try:
        results = await asyncio.to_thread(
            deploy_validation.evaluate_deep,
            services=selections,
            region=region,
            resource_group=resource_group,
            subscription_id=subscription_id,
            arm_token=token_info.token,
        )
    except Exception as ex:  # pragma: no cover - defensive
        log.exception("deep deployability check failed")
        return _err("evaluation_failed", f"Deep validation failed: {ex!r}", 502)

    try:
        activity_log.record(
            event_type="bom_deep_check",
            api_scope="arm",
            message=f"Deep (validate-only) deployability check in {region} for {len(selections)} service(s)",
        )
    except Exception:
        pass

    return _ok({"region": region, "subscription_id": subscription_id,
                "resource_group": resource_group, "results": results})

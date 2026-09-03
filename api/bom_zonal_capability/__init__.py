"""POST /api/bom/zonal-capability

Live per-subscription verification that the zone-redundant service tiers a user
selected in the BOM can actually be deployed in a given region. Rather than only
telling the user "this region has Availability Zones", for the subset of services
with an authoritative capability/SKU API we call ARM with the customer's own
subscription token and return a concrete per-service verdict
(``available`` / ``blocked`` / ``unavailable`` / ``not_verifiable``).

Request body::

    {
      "subscription_id": "<sub guid>",
      "region": "eastus",
      "services": [
        {"name": "Azure Blob Storage", "tier": "zrs"},
        {"name": "Azure SQL Database", "tier": "business_critical"}
      ]
    }

Only selections whose tier is zone-redundant (per the catalog) are evaluated;
the rest are dropped server-side so the UI only asks about tiers that matter.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List

from .._shared import auth, csrf, auth_token
from .._shared import httpfunc as func
from .._shared import bom_services, zonal_capability

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

    # Keep only zone-redundant tier selections — those are the ones worth a live
    # capability check. De-dupe on (name, tier).
    selections: List[Dict[str, str]] = []
    seen: set = set()
    for item in raw_services[:_MAX_SERVICES]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        tier = str(item.get("tier") or "").strip()
        if not name or not tier:
            continue
        if not _zone_redundant_tier(name, tier):
            continue
        key = (name, tier)
        if key in seen:
            continue
        seen.add(key)
        selections.append({"name": name, "tier": tier})

    if not selections:
        return _ok({"region": region, "subscription_id": subscription_id, "results": []})

    if not subscription_id:
        return _ok({
            "region": region,
            "subscription_id": "",
            "results": [
                {"name": s["name"], "tier": s["tier"],
                 "checkable": bool(zonal_capability.service_check_kind(s["name"])),
                 "verdict": "no_subscription",
                 "message": "Select a subscription to verify zone-redundant deployment live."}
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
            zonal_capability.evaluate,
            services=selections,
            region=region,
            arm_token=token_info.token,
            subscription_id=subscription_id,
        )
    except Exception as ex:  # pragma: no cover - defensive
        log.exception("zonal capability evaluation failed")
        return _err("evaluation_failed", f"Live capability check failed: {ex!r}", 502)

    return _ok({"region": region, "subscription_id": subscription_id, "results": results})

"""Scan quota for non-BOM subscriptions to find potential donor capacity."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict, List

from .._shared import auth_token
from .._shared import httpfunc as func
from .._shared import activity_log
from .._shared.quota_groups import check_subscription_quota

log = logging.getLogger(__name__)


def _err(code: str, message: str, status: int = 400) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"code": code, "message": message}),
        status_code=status,
        mimetype="application/json",
    )


async def main(req: func.HttpRequest) -> func.HttpResponse:
    """POST /api/donor-quota-scan

    Body: {
      "subscription_ids": ["sub-id-1", "sub-id-2"],
      "region": "australiaeast",
      "families": ["standardDav6Family", "standardAv2Family"]
    }

    Returns per-subscription quota for the requested families in the given region.
    """
    try:
        body = req.get_json()
    except Exception:
        return _err("bad_request", "Invalid JSON body.")

    subscription_ids: List[str] = body.get("subscription_ids") or []
    region: str = body.get("region") or ""
    families: List[str] = body.get("families") or []

    if not subscription_ids:
        return _err("bad_request", "subscription_ids is required.")
    if not region:
        return _err("bad_request", "region is required.")
    if not families:
        return _err("bad_request", "families is required.")

    # Cap to avoid abuse
    subscription_ids = subscription_ids[:20]

    token_getter = getattr(auth_token, "get_arm_token_for_subscription", auth_token.get_arm_token)
    try:
        token_info = await asyncio.to_thread(token_getter, subscription_ids[0])
    except auth_token.AuthError as ex:
        return _err("auth_error", str(ex), 401)

    arm_token = token_info.token

    results: Dict[str, dict] = {}
    for sub_id in subscription_ids:
        sub_id = str(sub_id).strip()
        if not sub_id:
            continue
        quota = await asyncio.to_thread(
            check_subscription_quota,
            arm_token,
            sub_id,
            [region],
            families,
            timeout_s=15.0,
        )
        # Extract just the region data we need
        region_data = (quota.get("regions") or {}).get(region.lower(), {})
        region_families = region_data.get("families") or {}
        results[sub_id] = {
            "status": region_data.get("status") or quota.get("status") or "unknown",
            "families": {
                fam: {
                    "limit": info.get("limit"),
                    "usage": info.get("usage"),
                    "headroom": info.get("headroom"),
                }
                for fam, info in region_families.items()
            },
        }

    activity_log.record(
        event_type="donor_quota_scan",
        message=f"Scanned {len(results)} subscriptions in {region} for {len(families)} families",
    )

    return func.HttpResponse(
        json.dumps({"results": results}),
        status_code=200,
        mimetype="application/json",
    )

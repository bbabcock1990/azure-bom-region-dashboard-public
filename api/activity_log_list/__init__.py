"""GET /api/activity_log

Returns recent activity log entries, newest-first. Since this app is
single-user / local, there's no per-user scoping — every caller sees
every event.

Query parameters (all optional):
  limit            : int, default 200, max 1000
  max_days         : int, default 7, max 90 — how many daily partitions to scan
  subscription_id  : GUID — filter to events stamped with this sub
  event_type       : str — filter to a single event_type
"""
from __future__ import annotations

import json
import logging

from .._shared import httpfunc as func

from .._shared import activity_log, auth

log = logging.getLogger(__name__)


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)

    params = req.params or {}

    def _int_param(name: str, default: int, lo: int, hi: int) -> int:
        try:
            v = int(params.get(name, default))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    limit = _int_param("limit", 200, 1, 1000)
    max_days = _int_param("max_days", 7, 1, 90)
    subscription_id = (params.get("subscription_id") or "").strip() or None
    event_type = (params.get("event_type") or "").strip() or None

    events = activity_log.query(
        limit=limit,
        max_days=max_days,
        subscription_id=subscription_id,
        event_type=event_type,
        actor_oid=None,
    )

    return func.HttpResponse(
        json.dumps({
            "events": events,
            "count": len(events),
            "filters": {
                "limit": limit,
                "max_days": max_days,
                "subscription_id": subscription_id,
                "event_type": event_type,
            },
            "known_event_types": sorted(activity_log.KNOWN_EVENT_TYPES),
        }),
        status_code=200, mimetype="application/json",
    )

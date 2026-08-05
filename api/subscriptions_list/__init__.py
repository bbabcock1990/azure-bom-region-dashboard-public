"""GET /api/subscriptions

Returns the distinct subscription IDs we have any successful snapshot for,
plus the user's last-active subscription.
"""
from __future__ import annotations

import json
import logging

from .._shared import httpfunc as func

from .._shared import auth, storage

log = logging.getLogger(__name__)


def main(req: func.HttpRequest) -> func.HttpResponse:
    principal = auth.get_local_user(req)

    runs = storage.get_table_client("runs")
    seen = {}
    try:
        for e in runs.list_entities():
            if e.get("status") != "succeeded":
                continue
            sub = e["PartitionKey"]
            ts = e.get("ended_at") or e.get("started_at") or ""
            if sub not in seen or ts > seen[sub]["last_run_at"]:
                seen[sub] = {"subscription_id": sub, "last_run_at": ts}
    except Exception:
        log.exception("subscriptions list failed")

    subs = sorted(seen.values(), key=lambda s: s["last_run_at"], reverse=True)

    last_active = None
    state = storage.get_table_client("user_state")
    try:
        e = state.get_entity(partition_key="user", row_key=principal.email)
        last_active = e.get("last_subscription_id")
    except Exception:
        pass

    return func.HttpResponse(
        json.dumps({"subscriptions": subs, "last_active_subscription": last_active}),
        status_code=200, mimetype="application/json",
    )

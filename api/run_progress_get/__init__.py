"""GET /api/run_progress?token=<uuid>

Returns the in-process progress record for a long-running compile job.
Used by the dashboard's Refresh Analysis modal to render a live
progress bar while ``POST /api/runs`` is still in flight.

Always responds with HTTP 200 — unknown tokens return
``{found: false}`` so the polling frontend doesn't have to interpret
4xx/5xx as "stop polling".
"""
from __future__ import annotations

import json
import logging

from .._shared import httpfunc as func

from .._shared import auth, run_progress

log = logging.getLogger(__name__)


def main(req: func.HttpRequest) -> func.HttpResponse:
    principal = auth.get_local_user(req)

    token = (req.params.get("token") or "").strip()
    if not token:
        return func.HttpResponse(
            json.dumps({"found": False, "reason": "missing_token"}),
            mimetype="application/json", status_code=200,
        )

    snapshot = run_progress.get(
        token,
        requesting_actor_oid=principal.oid,
        is_admin=True,
    )
    return func.HttpResponse(
        json.dumps(snapshot),
        mimetype="application/json",
        status_code=200,
    )

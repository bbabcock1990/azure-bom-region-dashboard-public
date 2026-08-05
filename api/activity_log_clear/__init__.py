"""POST /api/activity_log/clear  — admin only.

Drops and recreates the activitylog table. Logs a final
``log_cleared`` event AFTER the clear so the audit trail still shows
who wiped the log and when.
"""
from __future__ import annotations

import json
import logging

from .._shared import httpfunc as func

from .._shared import activity_log, auth, csrf

log = logging.getLogger(__name__)


def _err(code: str, message: str, status: int = 400) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": code, "message": message}),
        status_code=status, mimetype="application/json",
    )


def main(req: func.HttpRequest) -> func.HttpResponse:
    principal = auth.get_local_user(req)
    try:
        csrf.assert_safe_origin(req)
    except csrf.OriginError as ex:
        return _err("origin_rejected", str(ex), 403)

    summary = activity_log.clear()
    log.warning("activity_log cleared by %s (%s)", principal.email, principal.oid)
    # Post-clear marker so the wiper is auditable.
    activity_log.record(
        "log_cleared",
        actor_email=principal.email, actor_oid=principal.oid,
        api_scope="local", status="ok",
        message=f"Activity log cleared by {principal.email}",
        details=summary,
    )
    return func.HttpResponse(
        json.dumps({"ok": True, "summary": summary}),
        status_code=200, mimetype="application/json",
    )

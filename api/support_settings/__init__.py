"""GET/POST /api/support/settings

Read or update the support-contact settings (name, email, country, severity, …)
used as defaults when creating Azure support tickets. Persisted locally.
"""
from __future__ import annotations

import json
import logging

from .._shared import auth, csrf, support_settings, activity_log
from .._shared import httpfunc as func

log = logging.getLogger(__name__)


def _ok(payload: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload), status_code=status, mimetype="application/json"
    )


def _err(code: str, message: str, status: int = 400) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": code, "message": message}),
        status_code=status,
        mimetype="application/json",
    )


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)

    if req.method == "GET":
        return _ok({"settings": support_settings.get_settings(),
                    "configured": support_settings.is_configured()})

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

    saved = support_settings.save_settings(body)
    activity_log.record(
        event_type="support_settings_update",
        api_scope="local",
        message="Support settings updated",
    )
    return _ok({"settings": saved, "configured": support_settings.is_configured()})

"""GET /api/az/subscriptions

LOCAL_MODE only: returns the list of subscriptions the signed-in user can see
(via ARM REST). Lets the dashboard populate a picker without making the user
copy a GUID.
"""
from __future__ import annotations

import json
import logging

from .._shared import httpfunc as func

from .._shared import auth_token

log = logging.getLogger(__name__)


def _err(code: str, message: str, status: int = 400) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": code, "message": message}),
        status_code=status, mimetype="application/json",
    )


def main(req: func.HttpRequest) -> func.HttpResponse:
    if not auth_token.is_local_mode() and not auth_token.managed_identity_mode() \
            and not auth_token.delegated_mode():
        return _err("not_local_mode",
                    "/api/az/subscriptions is only available when LOCAL_MODE=true.", 403)
    try:
        subs = auth_token.list_subscriptions()
    except auth_token.AuthError as ex:
        log.warning("subscription list failed: %s", ex.code)
        status = 401 if ex.code == "not_signed_in" else 502
        return _err(ex.code, ex.message, status)
    return func.HttpResponse(
        json.dumps({"subscriptions": subs}),
        status_code=200, mimetype="application/json",
    )
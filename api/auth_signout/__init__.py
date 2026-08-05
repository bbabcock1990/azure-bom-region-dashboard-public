"""POST /api/auth/signout

Clears all cached credentials and tokens. The next sign-in will open a fresh
browser prompt, allowing the user to pick a different account or directory.
"""
from __future__ import annotations

import json
import logging

from .._shared import httpfunc as func

from .._shared import activity_log, auth, auth_token

log = logging.getLogger(__name__)


def main(req: func.HttpRequest) -> func.HttpResponse:
    principal = auth.get_local_user(req)
    if not auth_token.is_local_mode():
        activity_log.record(
            "auth_signout",
            actor_email=principal.email,
            actor_oid=principal.oid,
            api_scope="local",
            status="error",
            message="Sign-out rejected outside LOCAL_MODE",
        )
        return func.HttpResponse(
            json.dumps({"error": "not_local_mode",
                        "message": "/api/auth/signout is only available in LOCAL_MODE."}),
            status_code=403, mimetype="application/json",
        )

    auth_token.sign_out()
    log.info("User signed out — credentials cleared")
    activity_log.record(
        "auth_signout",
        actor_email=principal.email,
        actor_oid=principal.oid,
        api_scope="local",
        status="ok",
        message="Signed out and cleared cached credentials",
    )
    return func.HttpResponse(
        json.dumps({"status": "signed_out",
                    "message": "Signed out. Next sign-in will open a fresh browser prompt."}),
        status_code=200, mimetype="application/json",
    )

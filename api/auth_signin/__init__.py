"""GET/POST /api/auth/signin

Compatibility handler for the frontend sign-in flow. It returns an ARM bearer
token sourced from the local browser sign-in.
"""
from __future__ import annotations

import json
import logging

from .._shared import httpfunc as func

from .._shared import activity_log, auth, auth_token

log = logging.getLogger(__name__)


def _err(code: str, message: str, status: int = 400) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": code, "message": message}),
        status_code=status, mimetype="application/json",
    )


def main(req: func.HttpRequest) -> func.HttpResponse:
    principal = auth.get_local_user(req)
    mi_mode = auth_token.managed_identity_mode()
    if not auth_token.is_local_mode() and not mi_mode:
        activity_log.record(
            "auth_signin_error",
            actor_email=principal.email,
            actor_oid=principal.oid,
            api_scope="local",
            status="error",
            message="Sign-in rejected outside LOCAL_MODE",
            details={"interactive": req.method.upper() == "POST", "code": "not_local_mode"},
        )
        return _err(
            "not_local_mode",
            "/api/auth/signin is only available when LOCAL_MODE=true.",
            403,
        )

    interactive = req.method.upper() == "POST"
    activity_log.record(
        "auth_signin",
        actor_email=principal.email,
        actor_oid=principal.oid,
        api_scope="local",
        message="Interactive sign-in requested" if interactive else "Sign-in status checked",
        details={"interactive": interactive},
    )
    try:
        if mi_mode:
            # Hosted mode: no browser. Both GET (status) and POST (sign in)
            # resolve to the managed identity's ARM token.
            info = auth_token.get_arm_default_token(force_refresh=interactive)
        elif interactive:
            # Opens the browser for sign-in (or refreshes) - single-flighted.
            info = auth_token.ensure_signed_in(force=True)
        else:
            info = auth_token.get_token(allow_interactive=False)
    except auth_token.AuthError as ex:
        log.warning("ARM token fetch failed: %s", ex.code)
        activity_log.record(
            "auth_signin_error",
            actor_email=principal.email,
            actor_oid=principal.oid,
            api_scope="local",
            status="error",
            message=f"Sign-in failed: {ex.code}",
            details={"interactive": interactive, "code": ex.code},
        )
        status = 401 if ex.code == "not_signed_in" else 502
        return _err(ex.code, ex.message, status)

    body = info.to_public(include_token=True)
    body["forced_refresh"] = interactive
    activity_log.record(
        "auth_signin_ok",
        actor_email=principal.email,
        actor_oid=principal.oid,
        api_scope="local",
        status="ok",
        message="Sign-in available" if interactive else "Cached sign-in available",
        details={
            "interactive": interactive,
            "az_user": body.get("az_user"),
            "expires_at": body.get("expires_at"),
        },
    )
    return func.HttpResponse(
        json.dumps(body), status_code=200, mimetype="application/json",
    )
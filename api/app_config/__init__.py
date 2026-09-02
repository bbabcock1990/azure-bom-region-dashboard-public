"""GET /api/app-config — small, unauthenticated bootstrap config for the SPA.

Lets the frontend adapt without hard-coding server state: whether demo/sample
mode is on (drives the demo banner and forces ticket dry-run), whether support
contact details are configured, and the snapshot retention count.
"""
from __future__ import annotations

import json
import os

from .._shared import support_settings
from .._shared import snapshot_store
from .._shared import storage
from .._shared import auth_token
from .._shared import httpfunc as func


def _demo_mode() -> bool:
    return os.getenv("DEMO_MODE", "").strip().lower() in ("true", "1", "yes")


def main(req: func.HttpRequest) -> func.HttpResponse:
    payload = {
        "demo_mode": _demo_mode(),
        "local_mode": os.getenv("LOCAL_MODE", "").lower() in ("true", "1", "yes"),
        "managed_identity_mode": auth_token.managed_identity_mode(),
        "delegated_mode": auth_token.delegated_mode(),
        # MSAL.js bootstrap for delegated (multi-customer) mode: the SPA signs
        # the customer in silently against the same app registration Easy Auth
        # uses and mints an ARM token it forwards per request. Empty unless set.
        "entra_client_id": os.getenv("AAD_CLIENT_ID", "").strip()
        or os.getenv("MSAL_CLIENT_ID", "").strip(),
        "entra_authority": os.getenv(
            "MSAL_AUTHORITY", "https://login.microsoftonline.com/organizations"
        ).strip(),
        "arm_scope": os.getenv(
            "ARM_SCOPE", "https://management.azure.com/user_impersonation"
        ).strip(),
        "support_configured": support_settings.is_configured(),
        "snapshot_retention": snapshot_store.SNAPSHOT_RETENTION,
        "storage_dir": storage.storage_root(),
        "snapshots_dir": storage.snapshots_dir(),
    }
    return func.HttpResponse(
        json.dumps(payload), status_code=200, mimetype="application/json"
    )

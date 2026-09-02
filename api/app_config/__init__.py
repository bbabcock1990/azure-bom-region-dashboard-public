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
        "support_configured": support_settings.is_configured(),
        "snapshot_retention": snapshot_store.SNAPSHOT_RETENTION,
        "storage_dir": storage.storage_root(),
        "snapshots_dir": storage.snapshots_dir(),
    }
    return func.HttpResponse(
        json.dumps(payload), status_code=200, mimetype="application/json"
    )

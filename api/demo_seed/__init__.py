"""POST /api/demo/seed — load the bundled sample BOM + analysis on demand.

Powers the onboarding "Explore with sample data" button so a brand-new,
signed-out user can see a fully populated dashboard before touching Azure.
Seeds only when no BOM exists yet (so an existing workspace is never
disturbed) unless ``{"force": true}`` is posted. Does not touch Azure and
requires a same-origin POST.
"""
from __future__ import annotations

import json
import logging

from .._shared import auth, csrf, activity_log, demo_seed
from .._shared import httpfunc as func

log = logging.getLogger(__name__)


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)
    try:
        csrf.assert_safe_origin(req)
    except csrf.OriginError as ex:
        return func.HttpResponse(
            json.dumps({"error": "origin_rejected", "message": str(ex)}),
            status_code=403, mimetype="application/json",
        )

    force = False
    try:
        body = req.get_json()
        if isinstance(body, dict):
            force = bool(body.get("force"))
    except Exception:
        pass

    seeded = demo_seed.seed(force=force)
    bom_id = demo_seed.sample_bom_id()

    activity_log.record(
        event_type="demo_seed",
        api_scope="local",
        status="ok",
        message="Sample data loaded" if seeded else "Sample data already present",
        details={"seeded": seeded, "force": force, "bom_id": bom_id},
    )
    return func.HttpResponse(
        json.dumps({"ok": True, "seeded": seeded, "bom_id": bom_id}),
        status_code=200, mimetype="application/json",
    )

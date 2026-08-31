"""POST /api/local-state/wipe — delete all local snapshots + reset local tables.

A convenience for the "Wipe local state" button in Settings so a customer can
clear demo/sample data (or stale runs) without hunting for the ``local-storage``
folder on disk. Does not touch Azure. Requires a same-origin POST.
"""
from __future__ import annotations

import json
import logging

from .._shared import auth, csrf, storage, activity_log
from .._shared import httpfunc as func

log = logging.getLogger(__name__)

# Local tables that are safe to drop on a wipe. Support settings are preserved
# on purpose so the user does not have to re-enter contact details.
_TABLES = (
    "boms", "bomstore", "snapshots", "snapshotindex", "runs",
    "activitylog", "quotahistory", "supporttickets",
    "subscriptionmetadata", "bomservicecatalog", "bomregioncatalog",
)


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)
    try:
        csrf.assert_safe_origin(req)
    except csrf.OriginError as ex:
        return func.HttpResponse(
            json.dumps({"error": "origin_rejected", "message": str(ex)}),
            status_code=403, mimetype="application/json",
        )

    dropped = []
    try:
        service = storage.get_table_service()
        for name in _TABLES:
            try:
                service.delete_table(name)
                dropped.append(name)
            except Exception:
                log.debug("wipe: could not drop %s", name)
    except Exception:
        log.exception("local-state wipe failed")

    snapshots_removed = 0
    try:
        snapshots_removed = storage.wipe_snapshot_blobs()
    except Exception:
        log.debug("wipe: snapshot blob removal failed", exc_info=True)

    activity_log.record(
        event_type="local_state_wipe",
        api_scope="local",
        message=f"Local state wiped ({len(dropped)} tables, {snapshots_removed} snapshots)",
    )
    return func.HttpResponse(
        json.dumps({"ok": True, "tables_dropped": dropped,
                    "snapshots_removed": snapshots_removed}),
        status_code=200, mimetype="application/json",
    )

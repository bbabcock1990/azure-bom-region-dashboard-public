"""POST /api/local-state/open-folder — open the snapshots folder in the OS file
browser.

Only meaningful when the dashboard runs **locally** on the user's own machine
(``LOCAL_MODE``): the server process and the user share a desktop, so launching
the OS file explorer at :func:`storage.snapshots_dir` opens the real folder. In
the hosted container deployment there is no such folder (snapshots live in the
customer's in-memory/browser store), so this refuses with ``not_local`` and the
UI offers "Download snapshots" instead. Requires a same-origin POST.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

from .._shared import auth, csrf, storage
from .._shared import httpfunc as func

log = logging.getLogger(__name__)


def _local_mode() -> bool:
    return os.getenv("LOCAL_MODE", "").strip().lower() in ("true", "1", "yes")


def _err(code: str, message: str, status: int) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": code, "message": message}),
        status_code=status, mimetype="application/json",
    )


def _open_in_explorer(path: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606 - trusted local path
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)
    try:
        csrf.assert_safe_origin(req)
    except csrf.OriginError as ex:
        return _err("origin_rejected", str(ex), 403)

    if not _local_mode():
        return _err(
            "not_local",
            "Opening the folder is only available when running the dashboard "
            "locally. In the hosted app, use “Download snapshots” instead.",
            400,
        )

    path = storage.snapshots_dir()
    try:
        os.makedirs(path, exist_ok=True)
        _open_in_explorer(path)
    except Exception as ex:  # pragma: no cover - platform dependent
        log.warning("open-folder failed: %r", ex)
        return _err("open_failed", f"Could not open the folder: {ex}", 500)

    return func.HttpResponse(
        json.dumps({"ok": True, "path": path}),
        status_code=200, mimetype="application/json",
    )

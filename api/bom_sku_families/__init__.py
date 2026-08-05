"""HTTP API for the BOM editor's SKU family picker.

Route:
    GET /bom/sku_families[?refresh=true]

Returns the canonical, case-sensitive Azure VM SKU family IDs the BOM editor's
family dropdowns are populated from:

    { "families": ["standardDASv5Family", "standardDav6Family", ...],
      "source": "arm+builtin" | "builtin" }

The list comes from ARM ``Microsoft.Compute/skus`` (merged with a bundled seed
so it's never empty). ``refresh=true`` forces a live ARM re-pull — useful after
the user signs in, since the first modal open often happens pre-auth.
"""
from __future__ import annotations

import json
import logging

from .._shared import httpfunc as func

from .._shared import auth, sku_families

log = logging.getLogger(__name__)


def _truthy(s) -> bool:
    return str(s or "").strip().lower() in ("true", "1", "yes", "on")


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)
    refresh = _truthy(req.params.get("refresh"))
    try:
        result = sku_families.load_families(refresh=refresh)
    except Exception as ex:
        log.exception("bom_sku_families crash")
        return func.HttpResponse(
            json.dumps({"error": "internal_error", "message": f"{ex!r}"}),
            status_code=500, mimetype="application/json",
        )
    return func.HttpResponse(
        json.dumps(result), status_code=200, mimetype="application/json",
    )

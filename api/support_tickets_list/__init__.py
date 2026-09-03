"""GET /api/support/tickets — list locally tracked support tickets (newest first).

Optional query params:
    limit : max rows (default 100)
"""
from __future__ import annotations

import json

from .._shared import auth, support_tickets
from .._shared import httpfunc as func


def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)
    try:
        limit = int(req.params.get("limit") or 100)
    except (TypeError, ValueError):
        limit = 100
    tickets = support_tickets.list_tickets(limit=limit)
    return func.HttpResponse(
        json.dumps({"tickets": tickets}),
        status_code=200,
        mimetype="application/json",
    )

"""
Lightweight Origin/Referer guard for state-changing requests.

With sign-in removed, this is the only authorization layer left in the
API surface. It exists to defend against drive-by cross-origin POSTs to
``localhost`` while the user has the dev stack running — without it, a
malicious tab could trigger a refresh or clear the activity log without
the user's knowledge.

If ``ALLOWED_ORIGIN`` env is unset (e.g. unit tests, ad-hoc curl), the
check is a no-op. In production / dev stack starts, ``start-local.ps1``
and ``local.settings.example.json`` set it to the SWA emulator URL.
"""
from __future__ import annotations

import os
from typing import Optional
from urllib.parse import urlsplit

from . import httpfunc as func


def _allowed_origin() -> Optional[str]:
    return os.getenv("ALLOWED_ORIGIN")


class OriginError(Exception):
    """Raised by ``assert_safe_origin`` when a write came from an
    unexpected origin/referer."""
    pass


def _origin_of(url: str) -> str:
    """Return the ``scheme://host[:port]`` origin of a URL, or '' if unparseable."""
    try:
        parts = urlsplit(url)
    except Exception:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def assert_safe_origin(req: func.HttpRequest) -> None:
    """Raise ``OriginError`` if a non-GET request did not come from the
    configured ``ALLOWED_ORIGIN``. Read-only methods always pass.
    No-op when ``ALLOWED_ORIGIN`` is unset.

    Origins are compared **exactly** (scheme + host + port). A prefix match
    would wrongly accept e.g. ``http://localhost:42800`` for an allowed
    ``http://localhost:4280``.
    """
    if req.method.upper() in ("GET", "HEAD", "OPTIONS"):
        return
    allowed = _allowed_origin()
    if not allowed:
        return
    allowed_origin = _origin_of(allowed) or allowed.rstrip("/")
    origin = req.headers.get("Origin") or req.headers.get("origin") or ""
    referer = req.headers.get("Referer") or req.headers.get("referer") or ""
    if origin and _origin_of(origin) == allowed_origin:
        return
    # Referer carries a full URL (with path); compare just its origin.
    if referer and _origin_of(referer) == allowed_origin:
        return
    raise OriginError(
        f"request origin not allowed (origin={origin!r}, referer={referer!r})"
    )

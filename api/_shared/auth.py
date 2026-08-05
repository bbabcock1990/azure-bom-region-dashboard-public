"""
Local-user attribution helper.

This dashboard runs as a single-user local app — there is no sign-in,
no allowlist, and no role-based access control. The ``Principal`` returned
here exists only to populate "triggered_by" / "actor_*" fields on runs
and activity-log entries so legacy records and downstream queries keep
working.

Attribution: the OS username (e.g. ``alice`` on Windows) is used as
the ``email`` field, with ``oid`` and ``tenant_id`` set to ``"local"``.
If the username cannot be determined, the helper falls back to
``"local-user"`` so writes never fail on bad input.
"""
from __future__ import annotations

import getpass
import os
import re
from dataclasses import dataclass
from typing import Optional

from . import httpfunc as func


@dataclass(frozen=True)
class Principal:
    email: str           # OS username (lowercased), e.g. 'alice'
    oid: str             # always 'local'
    tenant_id: str       # always 'local'
    name: str            # same as email
    roles: tuple         # always ('admin',) — kept for back-compat
    raw_provider: str    # always 'local'


# ASCII letters/digits/dot/underscore/hyphen — anything else gets stripped
# so we never accidentally write strange characters into Table Storage
# keys downstream.
_USERNAME_OK = re.compile(r"[A-Za-z0-9._-]+")
_MAX_USERNAME_LEN = 64
_FALLBACK = "local-user"


def _safe_username() -> str:
    """Resolve a sane OS-level identifier for activity-log attribution.

    Tries USERNAME (Windows), USER (POSIX), then getpass.getuser(). Any
    exception or empty/garbage result short-circuits to the next source.
    Always returns a non-empty, ASCII-safe string.
    """
    for getter in (
        lambda: os.environ.get("USERNAME"),
        lambda: os.environ.get("USER"),
        lambda: getpass.getuser(),
    ):
        try:
            raw = (getter() or "").strip()
        except Exception:
            continue
        if not raw:
            continue
        parts = _USERNAME_OK.findall(raw)
        if not parts:
            continue
        cleaned = "-".join(parts).lower()[:_MAX_USERNAME_LEN]
        if cleaned:
            return cleaned
    return _FALLBACK


# Cache the resolved username for the lifetime of the worker process.
# Functions can spin up many workers but the OS user never changes within
# one process, and recomputing on every request would needlessly hit env
# lookups in the hot path.
_CACHED_USERNAME: Optional[str] = None


def get_local_user(req: Optional[func.HttpRequest] = None) -> Principal:
    """Return the fixed local ``Principal`` for activity-log attribution.

    The ``req`` argument is accepted (and ignored) for signature
    compatibility with the previous ``assert_allowed(req, require_role=...)``
    call shape — call sites can pass it without caring.
    """
    global _CACHED_USERNAME
    if _CACHED_USERNAME is None:
        _CACHED_USERNAME = _safe_username()
    user = _CACHED_USERNAME
    return Principal(
        email=user,
        oid="local",
        tenant_id="local",
        name=user,
        roles=("admin",),
        raw_provider="local",
    )

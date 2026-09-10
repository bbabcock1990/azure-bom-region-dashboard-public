"""Detect Azure Conditional-Access / MFA step-up rejections on ARM writes.

When a subscription enforces **"Require MFA for Azure management"**, ARM rejects
control-plane *writes* (create/update/delete) made with a non-MFA token — even
for a subscription Owner — with a 401/403 whose body and/or ``WWW-Authenticate``
header signal that multi-factor authentication is required (``aka.ms/MFAforAzure``,
``RequestDisallowedByAzure``, ``insufficient_claims``, AADSTS50076/50079).

Reads are not gated on MFA, which is why listing subscriptions, resource groups
and permissions succeed while a resource-group create is refused. Endpoints turn
a positive detection into an ``mfa_required`` error carrying any ``claims``
challenge, so the SPA can step the user up (re-acquire an MFA token) and retry
the write once.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

# Signatures Azure uses when a write is blocked pending an MFA step-up.
MFA_MARKERS = (
    "requestdisallowedbyazure",   # ARM CA block: "...without authenticating through MFA"
    "multi-factor",
    "multifactor",
    "insufficient_claims",
    "aka.ms/mfaforazure",
    "mfaforazure",
    "50076",                      # AADSTS50076 — MFA required
    "50079",                      # AADSTS50079 — MFA enrollment required
)


def mfa_challenge(resp: Any, body: Any) -> Optional[Dict[str, Any]]:
    """Detect an MFA / conditional-access step-up rejection on an ARM write.

    Returns a dict (optionally carrying the base64 ``claims`` challenge from the
    ``WWW-Authenticate`` header) when Azure demands an MFA-authenticated token,
    else ``None``. Callers turn this into an ``mfa_required`` error so the SPA
    can re-acquire an MFA token and retry.
    """
    try:
        status = int(getattr(resp, "status_code", 0) or 0)
    except Exception:
        status = 0
    if status not in (401, 403):
        return None

    hay = ""
    if isinstance(body, dict):
        hay = json.dumps(body, ensure_ascii=False)
    elif body:
        hay = str(body)
    hay = hay.lower()

    www = ""
    claims: Optional[str] = None
    try:
        www = str((getattr(resp, "headers", {}) or {}).get("WWW-Authenticate", "") or "")
    except Exception:
        www = ""
    if www:
        hay += " " + www.lower()
        m = re.search(r'claims="([^"]+)"', www)
        if m:
            claims = m.group(1)

    if any(marker in hay for marker in MFA_MARKERS):
        return {"claims": claims}
    return None

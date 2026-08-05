"""
In-process token acquisition via MSAL (azure-identity), replacing the
``az account get-access-token`` subprocess in ``azcli_token``.

Uses ``InteractiveBrowserCredential`` whose default client id is the Azure CLI
first-party client (``04b07795-8ddb-461a-bbee-02f9e1bf7b46``) — the same client
``az`` uses — so it mints first-party **ARM** tokens with no new app
registration and no ``az`` binary. A persistent,
OS-encrypted token cache makes sign-in a one-time browser prompt; later launches
acquire tokens silently.

Public surface mirrors ``azcli_token`` so call sites are drop-in:
    get_token(resource=, tenant=, subscription=, force_refresh=) -> TokenInfo
    get_arm_token(subscription_id, force_refresh=) -> TokenInfo
    get_arm_default_token(force_refresh=) -> TokenInfo
    list_subscriptions() -> list
    is_local_mode() -> bool
    ensure_signed_in(force=) -> TokenInfo        # triggers interactive sign-in
    has_cached_account() -> bool
    AuthError / (alias) AzCliError, TokenInfo
"""
from __future__ import annotations

import base64
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

ARM_RESOURCE_ID = "https://management.azure.com"
EARLY_REFRESH_SECONDS = 5 * 60  # treat as stale if < 5 min left

_lock = threading.Lock()
_signin_lock = threading.Lock()
_credential = None  # lazily-built InteractiveBrowserCredential
_silent_credential = None  # lazily-built non-interactive credential (never prompts)
_auth_record = None  # AuthenticationRecord captured at sign-in, for silent reuse
_signed_in = False  # set True after any successful token acquisition this process
_cache: Dict[Tuple[str, str, str], "TokenInfo"] = {}
_sub_tenant_map: Dict[str, str] = {}  # subscription_id -> tenant_id (populated by list_subscriptions)

def is_local_mode() -> bool:
    import os
    return os.getenv("LOCAL_MODE", "").lower() in ("true", "1", "yes")


class AuthError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# Back-compat alias so existing ``except azcli_token.AzCliError`` call sites keep
# working when they import this module as the auth provider.
AzCliError = AuthError


@dataclass(frozen=True)
class TokenInfo:
    token: str
    expires_at: datetime  # UTC
    az_user: str          # UPN/preferred_username decoded from the token
    az_tenant: str        # tenant id (tid) from the token
    az_subscription: str  # subscription id this token was requested for, if any
    resource: str = ARM_RESOURCE_ID

    @property
    def expires_in_seconds(self) -> int:
        return max(0, int((self.expires_at - datetime.now(timezone.utc)).total_seconds()))

    @property
    def is_fresh(self) -> bool:
        return self.expires_in_seconds > EARLY_REFRESH_SECONDS

    def to_public(self, *, include_token: bool = True) -> dict:
        out = {
            "expires_at": self.expires_at.isoformat(),
            "expires_in_seconds": self.expires_in_seconds,
            "az_user": self.az_user,
            "az_tenant": self.az_tenant,
            "az_subscription": self.az_subscription,
            "is_fresh": self.is_fresh,
            "resource": self.resource,
        }
        preview = (self.token[:16] + "..." + self.token[-8:]) if self.token else ""
        out["token_preview"] = preview
        if include_token:
            out["token"] = self.token
        return out


def _scope_for(resource: str) -> str:
    """Translate an ``az --resource`` value into an MSAL ``.default`` scope."""
    r = resource.rstrip("/")
    return r + "/.default" if not r.endswith("/.default") else r


def _decode_claims(token: str) -> dict:
    """Best-effort decode of a JWT payload (no signature check — display only)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Custom redirect server that auto-closes the browser tab after auth.
# ---------------------------------------------------------------------------
_AutoCloseRedirectServer = None  # type: ignore

try:
    from http.server import BaseHTTPRequestHandler
    from urllib.parse import parse_qs as _parse_qs
    from azure.identity._internal.auth_code_redirect_handler import (
        AuthCodeRedirectServer as _BaseRedirectServer,
    )

    _AUTO_CLOSE_HTML = (
        b"<html><head><title>Sign-in complete</title></head>"
        b"<body style='font-family:system-ui,sans-serif;padding:2rem;text-align:center;'>"
        b"<h2 style='color:#2da44e;'>&#10004; Authentication complete</h2>"
        b"<p>You can close this tab and return to the dashboard.</p>"
        b"<script>try{window.close()}catch(e){}</script>"
        b"</body></html>"
    )

    class _AutoCloseHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.endswith("/favicon.ico"):
                self.send_response(204)
                return
            query = self.path.split("?", 1)[-1]
            parsed = _parse_qs(query, keep_blank_values=True)
            self.server.query_params = {
                k: v[0] if isinstance(v, list) and len(v) == 1 else v
                for k, v in parsed.items()
            }
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_AUTO_CLOSE_HTML)

        def log_message(self, format, *args):
            pass

    class _AutoCloseRedirectServer(_BaseRedirectServer):
        """Drop-in for azure-identity's redirect server, auto-closes the browser tab."""
        def __init__(self, hostname, port, timeout):
            super().__init__(hostname, port, timeout)
            # Replace the handler class used by HTTPServer
            self.RequestHandlerClass = _AutoCloseHandler

except Exception:
    pass  # fallback: _AutoCloseRedirectServer stays None


def _credentials():
    global _credential
    if _credential is not None:
        return _credential
    with _lock:
        if _credential is not None:
            return _credential
        try:
            from azure.identity import (
                InteractiveBrowserCredential,
                TokenCachePersistenceOptions,
            )
        except Exception as ex:  # pragma: no cover - import guard
            raise AuthError(
                "msal_unavailable",
                f"azure-identity is required for sign-in but failed to import: {ex}",
            )
        kwargs = {
            # All tenants allowed so per-customer-subscription ARM tokens (which
            # live in foreign tenants) can be requested via get_token(tenant_id=).
            "additionally_allowed_tenants": ["*"],
        }
        try:
            kwargs["cache_persistence_options"] = TokenCachePersistenceOptions(
                name="azure-bom-region-dashboard"
            )
        except Exception:
            # Persistent cache is best-effort; fall back to in-memory.
            pass
        # Use a custom server class that auto-closes the browser tab after sign-in.
        if _AutoCloseRedirectServer is not None:
            kwargs["_server_class"] = _AutoCloseRedirectServer
        _credential = InteractiveBrowserCredential(**kwargs)
        return _credential


def _acquire(*, scope: str, tenant: Optional[str], allow_interactive: bool) -> "object":
    """Return an azure-core AccessToken for ``scope`` (optionally in ``tenant``).

    ``allow_interactive=False`` requests a silent acquisition; if no cached
    account exists azure-identity raises, which we translate to a stable code so
    callers can prompt the user to sign in.
    """
    cred = _credentials()
    kwargs = {}
    if tenant:
        kwargs["tenant_id"] = tenant
    try:
        from azure.identity import CredentialUnavailableError
    except Exception:  # pragma: no cover
        CredentialUnavailableError = Exception  # type: ignore

    # azure-identity has no public "silent only" switch on get_token; we rely on
    # a cached account for silent paths. When interactive isn't allowed and there
    # is no account, surface a not-signed-in error instead of popping a browser.
    if not allow_interactive and not has_cached_account():
        raise AuthError(
            "not_signed_in",
            "Not signed in. Use the dashboard's Sign in action (or POST "
            "/api/auth/signin) to start the one-time browser sign-in.",
        )
    try:
        info = cred.get_token(scope, **kwargs)
        global _signed_in, _auth_record
        _signed_in = True
        # Opportunistically capture the AuthenticationRecord so a later
        # non-interactive refresh can silently reuse this account (covers
        # returning users whose session loads from the persistent cache and
        # never calls the explicit sign-in endpoint).
        if _auth_record is None:
            try:
                rec = getattr(cred, "_auth_record", None)
                if rec is not None:
                    _auth_record = rec
            except Exception:
                pass
        return info
    except CredentialUnavailableError as ex:  # type: ignore
        raise AuthError("not_signed_in", f"Sign-in required: {ex}")
    except Exception as ex:
        low = str(ex).lower()
        if "aadsts50020" in low:
            raise AuthError(
                "cross_tenant_not_guest",
                "User is not a guest in the target tenant.",
            )
        if "aadsts700016" in low:
            raise AuthError(
                "cross_tenant_no_consent",
                "Application not registered in target tenant.",
            )
        if "aadsts65001" in low:
            raise AuthError(
                "cross_tenant_no_consent",
                "Consent required in target tenant.",
            )
        if "status code 403" in low or "403 client error" in low or "forbidden" in low:
            raise AuthError(
                "subscription_no_reader",
                "Need Reader role on the subscription.",
            )
        if "aadsts53003" in low or "conditional access" in low:
            raise AuthError(
                "ca_consent_required",
                "Conditional Access requires interactive sign-in. Use the "
                "dashboard's Sign in action and complete the browser prompt.",
            )
        raise AuthError("token_acquire_failed", f"Token acquisition failed: {ex}")


def has_cached_account() -> bool:
    """True if a usable account is already cached (silent acquisition possible).

    The primary, reliable signal is ``_signed_in`` — set whenever any token
    acquisition succeeds this process (including the interactive sign-in). Once
    the user has signed in, every subsequent silent ARM acquisition (in any
    tenant) reuses the same in-memory/persistent cache, so we must report the
    account as available. The MSAL attribute-probe below is only a best-effort
    fallback for a fresh process that has a populated persistent cache; its
    internal paths are version-fragile, so we never let it veto ``_signed_in``.
    """
    if _signed_in:
        return True
    global _credential
    if _credential is None:
        # Building the credential is cheap and reads the persistent cache.
        try:
            _credentials()
        except AuthError:
            return False
    try:
        # azure-identity exposes the cached MSAL accounts via the private client;
        # treat any cached account as "signed in". Best-effort — on any error we
        # assume not signed in and let the interactive path handle it.
        get_app = getattr(_credential, "_get_app", None)
        msal_app = None
        if callable(get_app):
            try:
                msal_app = get_app()
            except Exception:
                msal_app = None
        if msal_app is None:
            app = getattr(_credential, "_client", None)
            msal_app = (
                getattr(_credential, "_msal_app", None)
                or getattr(app, "_msal_app", None)
                or getattr(app, "_client", None)
            )
        if msal_app and hasattr(msal_app, "get_accounts"):
            return bool(msal_app.get_accounts())
    except Exception:
        pass
    return False


def _to_info(access_token, *, scope_resource: str, subscription: str = "") -> "TokenInfo":
    token = getattr(access_token, "token", "") or ""
    expires_on = getattr(access_token, "expires_on", None)
    if expires_on:
        expires_at = datetime.fromtimestamp(int(expires_on), tz=timezone.utc)
    else:
        expires_at = datetime.now(timezone.utc)
    claims = _decode_claims(token)
    user = claims.get("upn") or claims.get("preferred_username") or claims.get("unique_name") or ""
    tenant = claims.get("tid") or ""
    return TokenInfo(
        token=token,
        expires_at=expires_at,
        az_user=user,
        az_tenant=tenant,
        az_subscription=subscription or claims.get("xms_az_rid", "") or "",
        resource=scope_resource,
    )


def get_token(
    *,
    resource: str = ARM_RESOURCE_ID,
    tenant: Optional[str] = None,
    subscription: Optional[str] = None,
    force_refresh: bool = False,
    allow_interactive: bool = False,
) -> TokenInfo:
    """Mirror of ``azcli_token.get_token``.

    Call with no args for the default ARM token. For a subscription-home-tenant
    ARM token, pass ``resource=ARM_RESOURCE_ID`` plus the resolved tenant or
    subscription.
    """
    cache_key = (resource, tenant or "default", subscription or "")
    with _lock:
        cached = _cache.get(cache_key)
        if not force_refresh and cached is not None and cached.is_fresh:
            return cached
    access = _acquire(scope=_scope_for(resource), tenant=tenant,
                      allow_interactive=allow_interactive)
    info = _to_info(access, scope_resource=resource, subscription=subscription or "")
    with _lock:
        _cache[cache_key] = info
    return info


def ensure_signed_in(*, force: bool = False) -> TokenInfo:
    """Trigger the one-time interactive browser sign-in (or refresh) and return
    the ARM TokenInfo. Serialized so concurrent callers don't open multiple
    browser windows. Call this from the explicit sign-in endpoint, off the
    silent GET path."""
    with _signin_lock:
        info = get_token(force_refresh=force, allow_interactive=True)
        # Best-effort: capture the AuthenticationRecord azure-identity stored on
        # the credential after sign-in, so a later non-interactive refresh
        # (``try_silent_refresh``) can silently reuse this exact account. This is
        # read post-hoc (no extra prompt); if the attribute isn't present the
        # silent refresh simply stays a safe no-op.
        global _auth_record
        try:
            rec = getattr(_credentials(), "_auth_record", None)
            if rec is not None:
                _auth_record = rec
        except Exception:
            pass
        return info


def try_silent_refresh(
    *, resource: str = ARM_RESOURCE_ID, tenant: Optional[str] = None,
) -> Optional["TokenInfo"]:
    """Best-effort, **guaranteed non-interactive** token refresh.

    Returns a freshly-acquired :class:`TokenInfo` (full TTL) or ``None`` if a
    sign-in would be required or anything fails. Uses a dedicated credential
    built with ``disable_automatic_authentication`` plus the
    :class:`AuthenticationRecord` captured at sign-in, so it can **never** pop a
    browser — it raises ``AuthenticationRequiredError`` instead, which we
    swallow. Safe to call on hot paths (e.g. before a long run) to ensure the
    token outlives the work without risking an interactive prompt.
    """
    global _silent_credential
    record = _auth_record
    if record is None:
        # No captured account this session — can't silently target one. Caller
        # keeps its existing token.
        return None
    try:
        from azure.identity import (
            InteractiveBrowserCredential,
            TokenCachePersistenceOptions,
        )
    except Exception:
        return None
    with _lock:
        if _silent_credential is None:
            kwargs = {
                "additionally_allowed_tenants": ["*"],
                # Never start an interactive flow — raise instead of prompting.
                "disable_automatic_authentication": True,
                "authentication_record": record,
            }
            try:
                kwargs["cache_persistence_options"] = TokenCachePersistenceOptions(
                    name="azure-bom-region-dashboard"
                )
            except Exception:
                pass
            try:
                _silent_credential = InteractiveBrowserCredential(**kwargs)
            except Exception:
                return None
        cred = _silent_credential
    kw = {}
    if tenant:
        kw["tenant_id"] = tenant
    try:
        access = cred.get_token(_scope_for(resource), **kw)
    except Exception:
        # AuthenticationRequiredError, CredentialUnavailableError, CA, etc. —
        # all mean "can't refresh silently"; caller keeps its existing token.
        return None
    return _to_info(access, scope_resource=resource)


def get_arm_token(subscription_id: str, *, force_refresh: bool = False) -> TokenInfo:
    """ARM token scoped to the subscription's home tenant.

    az auto-resolved the tenant from ``--subscription``; MSAL needs the tenant
    explicitly, so we resolve sub -> tenant via ARM first (using the default
    home-tenant token), then request the ARM token in that tenant.
    """
    tenant = _resolve_subscription_tenant(subscription_id, force_refresh=force_refresh)
    return get_token(
        resource=ARM_RESOURCE_ID,
        tenant=tenant,
        subscription=subscription_id,
        force_refresh=force_refresh,
    )


def get_arm_default_token(*, force_refresh: bool = False) -> TokenInfo:
    """ARM token in the signed-in user's home tenant — for tenant-agnostic ARM
    calls (provider-show, global SKU lookup)."""
    try:
        return get_token(resource=ARM_RESOURCE_ID, force_refresh=force_refresh)
    except AuthError as ex:
        if ex.code != "not_signed_in" or not (_signed_in or _credential is not None):
            raise
        try:
            access = _credentials().get_token(_scope_for(ARM_RESOURCE_ID))
        except Exception:
            raise ex
        info = _to_info(access, scope_resource=ARM_RESOURCE_ID)
        with _lock:
            _cache[(ARM_RESOURCE_ID, "default", "")] = info
        return info


def _resolve_subscription_tenant(subscription_id: str, *, force_refresh: bool = False) -> Optional[str]:
    """Look up a subscription's home tenant via ARM. Falls back to None (default
    tenant) on any failure — the per-sub overlay is best-effort."""
    # Fast path: sub→tenant map populated by list_subscriptions().
    cached_tid = _sub_tenant_map.get(subscription_id)
    if cached_tid and not force_refresh:
        return cached_tid
    try:
        import httpx
    except Exception:  # pragma: no cover
        return None
    try:
        default = get_arm_default_token(force_refresh=force_refresh)
    except AuthError:
        return None
    url = f"{ARM_RESOURCE_ID}/subscriptions/{subscription_id}?api-version=2022-12-01"
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(url, headers={"Authorization": f"Bearer {default.token}"})
        if resp.status_code == 200:
            return resp.json().get("tenantId") or None
    except Exception:
        return None
    return None


def list_subscriptions() -> list:
    """Subscriptions visible to the signed-in user across ALL tenants.

    Enumerates tenants the user belongs to, acquires a token for each, and
    aggregates subscriptions so guest-account subs in foreign tenants appear.
    """
    try:
        import httpx
    except Exception:  # pragma: no cover
        raise AuthError("httpx_unavailable", "httpx is required to list subscriptions")
    import logging as _log
    log = _log.getLogger(__name__)

    info = get_arm_default_token()

    # 1. Enumerate tenants the signed-in user can see.
    tenants_url = f"{ARM_RESOURCE_ID}/tenants?api-version=2022-12-01"
    tenant_ids = []
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(tenants_url, headers={"Authorization": f"Bearer {info.token}"})
        if resp.status_code == 200:
            for t in resp.json().get("value", []):
                tid = t.get("tenantId")
                if tid:
                    tenant_ids.append(tid)
    except Exception:
        pass

    # If we couldn't enumerate tenants, fall back to just the home tenant.
    if not tenant_ids:
        tenant_ids = [info.az_tenant] if info.az_tenant else []

    # 2. For each tenant, get a token and list subscriptions.
    subs = []
    seen_ids = set()
    for tid in tenant_ids:
        try:
            if tid == info.az_tenant:
                token = info.token
            else:
                t_info = get_token(resource=ARM_RESOURCE_ID, tenant=tid,
                                   allow_interactive=False)
                token = t_info.token
        except AuthError:
            log.debug("Skipping tenant %s - could not acquire token", tid)
            continue

        url = f"{ARM_RESOURCE_ID}/subscriptions?api-version=2022-12-01"
        try:
            with httpx.Client(timeout=20.0) as client:
                resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
            if resp.status_code != 200:
                continue
            for entry in resp.json().get("value", []):
                sub_id = entry.get("subscriptionId")
                if sub_id and sub_id not in seen_ids:
                    seen_ids.add(sub_id)
                    tenant_for_sub = entry.get("tenantId") or tid
                    _sub_tenant_map[sub_id] = tenant_for_sub
                    subs.append({
                        "id": sub_id,
                        "name": entry.get("displayName"),
                        "tenantId": tenant_for_sub,
                        "isDefault": False,
                        "state": entry.get("state"),
                    })
        except Exception:
            log.debug("Failed to list subs in tenant %s", tid)
            continue

    subs.sort(key=lambda s: (s.get("name") or "").lower())
    return subs

def reset_for_tests() -> None:
    """Test helper — clear the cached credential + token cache."""
    global _credential, _silent_credential, _auth_record, _signed_in
    with _lock:
        _credential = None
        _silent_credential = None
        _auth_record = None
        _signed_in = False
        _cache.clear()
        _sub_tenant_map.clear()


def sign_out() -> None:
    """Clear all cached credentials and tokens so the next acquire triggers
    a fresh interactive sign-in. Also removes the persistent token cache file
    so a restart doesn't silently resume the old session."""
    global _credential, _silent_credential, _auth_record, _signed_in
    with _lock:
        # Try to remove persistent cache accounts before discarding the cred
        if _credential is not None:
            try:
                get_app = getattr(_credential, "_get_app", None)
                msal_app = get_app() if callable(get_app) else None
                if msal_app and hasattr(msal_app, "get_accounts"):
                    for acct in msal_app.get_accounts():
                        msal_app.remove_account(acct)
            except Exception:
                pass
        _credential = None
        _silent_credential = None
        _auth_record = None
        _signed_in = False
        _cache.clear()

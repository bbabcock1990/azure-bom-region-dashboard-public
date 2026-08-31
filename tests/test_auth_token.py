"""Unit tests for auth_token (MSAL-backed) — no real sign-in, no network."""
import base64
import json
import time

import pytest

from _shared import auth_token

# Capture the real implementation before the autouse fixture stubs it out, so a
# regression test can exercise the genuine has_cached_account() logic.
_REAL_HAS_CACHED_ACCOUNT = auth_token.has_cached_account


def _fake_jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


class _FakeAccessToken:
    def __init__(self, token, expires_on):
        self.token = token
        self.expires_on = expires_on


class _FakeCredential:
    def __init__(self):
        self.calls = []

    def get_token(self, scope, **kwargs):
        self.calls.append((scope, kwargs))
        claims = {"upn": "testuser@example.com", "tid": kwargs.get("tenant_id") or "home-tenant"}
        return _FakeAccessToken(_fake_jwt(claims), int(time.time()) + 3600)


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    # Isolate the on-disk AuthenticationRecord so tests never read a developer's
    # real persisted sign-in (which would make silent paths spuriously succeed).
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    auth_token.reset_for_tests()
    # Always pretend a usable account exists so silent paths don't raise.
    monkeypatch.setattr(auth_token, "has_cached_account", lambda: True)
    yield
    auth_token.reset_for_tests()


@pytest.fixture
def cred(monkeypatch):
    c = _FakeCredential()
    monkeypatch.setattr(auth_token, "_credentials", lambda: c)
    return c


def test_scope_translation():
    assert auth_token._scope_for("b1866aa0-x") == "b1866aa0-x/.default"
    assert auth_token._scope_for("https://management.azure.com") == "https://management.azure.com/.default"
    assert auth_token._scope_for("https://management.azure.com/.default") == "https://management.azure.com/.default"


def test_decode_claims_roundtrip():
    tok = _fake_jwt({"upn": "u@x.com", "tid": "t1"})
    c = auth_token._decode_claims(tok)
    assert c["upn"] == "u@x.com" and c["tid"] == "t1"
    assert auth_token._decode_claims("garbage") == {}


def test_default_token_uses_arm_scope(cred):
    info = auth_token.get_token()
    scope, kwargs = cred.calls[0]
    assert scope == "https://management.azure.com/.default"
    assert "tenant_id" not in kwargs
    assert info.az_user == "testuser@example.com"
    assert info.resource == auth_token.ARM_RESOURCE_ID
    assert info.is_fresh and info.expires_in_seconds > 3000


def test_token_is_cached(cred):
    auth_token.get_token()
    auth_token.get_token()
    assert len(cred.calls) == 1  # second call served from cache


def test_force_refresh_bypasses_cache(cred):
    auth_token.get_token()
    auth_token.get_token(force_refresh=True)
    assert len(cred.calls) == 2


def test_try_silent_refresh_returns_token(monkeypatch):
    """Happy path: with a captured auth record, a non-interactive silent
    acquisition yields a fresh token."""
    c = _FakeCredential()
    monkeypatch.setattr(auth_token, "_auth_record", object())  # signed-in marker
    monkeypatch.setattr(auth_token, "_silent_credential", c)
    info = auth_token.try_silent_refresh()
    assert info is not None and info.token
    scope, kwargs = c.calls[0]
    assert scope == "https://management.azure.com/.default"
    assert "tenant_id" not in kwargs


def test_try_silent_refresh_none_without_auth_record(monkeypatch):
    """No captured account this session → safe no-op (never touches a credential)."""
    monkeypatch.setattr(auth_token, "_auth_record", None)
    assert auth_token.try_silent_refresh() is None


def test_try_silent_refresh_never_raises_returns_none(monkeypatch):
    """If a sign-in would be required (credential raises), we return None — and
    crucially never propagate or pop a browser."""
    class _RaisingCred:
        def get_token(self, scope, **kwargs):
            raise RuntimeError("AuthenticationRequiredError: interaction needed")
    monkeypatch.setattr(auth_token, "_auth_record", object())
    monkeypatch.setattr(auth_token, "_silent_credential", _RaisingCred())
    assert auth_token.try_silent_refresh() is None


def test_arm_default_token_scope(cred):
    info = auth_token.get_arm_default_token()
    scope, kwargs = cred.calls[0]
    assert scope == "https://management.azure.com/.default"
    assert "tenant_id" not in kwargs  # home tenant
    assert info.resource == auth_token.ARM_RESOURCE_ID


def test_arm_token_resolves_subscription_tenant(cred, monkeypatch):
    monkeypatch.setattr(
        auth_token,
        "_resolve_subscription_tenant",
        lambda sub, force_refresh=False: "foreign-tenant",
    )
    info = auth_token.get_arm_token("sub-123")
    scope, kwargs = cred.calls[-1]
    assert scope == "https://management.azure.com/.default"
    assert kwargs["tenant_id"] == "foreign-tenant"
    assert info.az_subscription == "sub-123"


def test_arm_token_cache_key_separates_unresolved_and_resolved_tenants(cred, monkeypatch):
    tenants = [None, "foreign-tenant"]
    monkeypatch.setattr(
        auth_token,
        "_resolve_subscription_tenant",
        lambda sub, force_refresh=False: tenants.pop(0),
    )

    first = auth_token.get_arm_token("sub-123")
    second = auth_token.get_arm_token("sub-123", force_refresh=True)

    assert first.az_tenant == "home-tenant"
    assert second.az_tenant == "foreign-tenant"
    assert len(cred.calls) == 2
    assert cred.calls[0][1] == {}
    assert cred.calls[1][1] == {"tenant_id": "foreign-tenant"}


@pytest.mark.parametrize(
    ("message", "code", "text"),
    [
        ("AADSTS50020: boom", "cross_tenant_not_guest", "not a guest"),
        ("AADSTS700016: boom", "cross_tenant_no_consent", "Application not registered"),
        ("AADSTS65001: boom", "cross_tenant_no_consent", "Consent required"),
        ("403 Client Error: Forbidden", "subscription_no_reader", "Reader role"),
    ],
)
def test_acquire_maps_cross_tenant_errors(monkeypatch, message, code, text):
    class _BoomCred:
        def get_token(self, scope, **kwargs):
            raise RuntimeError(message)

    monkeypatch.setattr(auth_token, "_credentials", lambda: _BoomCred())
    monkeypatch.setattr(auth_token, "has_cached_account", lambda: True)

    with pytest.raises(auth_token.AuthError) as ex:
        auth_token.get_token()

    assert ex.value.code == code
    assert text in ex.value.message


def test_to_public_shapes(cred):
    pub = auth_token.get_token().to_public(include_token=True)
    assert set(["expires_at", "expires_in_seconds", "az_user", "az_tenant",
                "az_subscription", "is_fresh", "resource", "token_preview", "token"]).issubset(pub)
    pub2 = auth_token.get_token().to_public(include_token=False)
    assert "token" not in pub2 and "token_preview" in pub2


def test_not_signed_in_when_no_account(monkeypatch, cred):
    monkeypatch.setattr(auth_token, "has_cached_account", lambda: False)
    with pytest.raises(auth_token.AuthError) as ex:
        auth_token.get_token(allow_interactive=False)
    assert ex.value.code == "not_signed_in"


def test_silent_succeeds_after_interactive_sign_in(monkeypatch, cred):
    """Regression: after an interactive sign-in, silent acquisition (e.g. the ARM
    overlay) must succeed. Previously a fragile has_cached_account() probe could
    return False even when signed in, blocking the ARM token with not_signed_in."""
    # Use the real has_cached_account (the autouse fixture stubs it to True).
    monkeypatch.setattr(auth_token, "has_cached_account", _REAL_HAS_CACHED_ACCOUNT)
    assert auth_token._signed_in is False
    assert _REAL_HAS_CACHED_ACCOUNT() is False  # no probe-able account on the fake

    # Interactive ARM sign-in marks the process as signed in.
    auth_token.get_token(allow_interactive=True)
    assert auth_token._signed_in is True
    assert _REAL_HAS_CACHED_ACCOUNT() is True

    # The silent ARM-default acquisition now succeeds instead of raising.
    arm = auth_token.get_arm_default_token()
    assert arm.token


def test_azclierror_alias():
    assert auth_token.AzCliError is auth_token.AuthError


def test_list_subscriptions(monkeypatch, cred):
    import httpx

    def handler(request):
        return httpx.Response(200, json={"value": [
            {"subscriptionId": "s2", "displayName": "Beta", "tenantId": "t", "state": "Enabled"},
            {"subscriptionId": "s1", "displayName": "Alpha", "tenantId": "t", "state": "Enabled"},
        ]})

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def fake_client(*a, **k):
        k["transport"] = transport
        return real_client(*a, **k)

    monkeypatch.setattr(httpx, "Client", fake_client)
    subs = auth_token.list_subscriptions()
    assert [s["name"] for s in subs] == ["Alpha", "Beta"]  # sorted
    assert subs[0]["id"] == "s1"


def test_arm_default_token_falls_back_to_silent_probe(monkeypatch):
    """If the primary silent path reports "not signed in" but the persistent
    cache probe can still mint a token, reuse it instead of failing — and
    without ever launching an interactive browser flow."""
    auth_token._signed_in = True

    def fake_get_token(**kwargs):
        raise auth_token.AuthError("not_signed_in", "not signed in")

    monkeypatch.setattr(auth_token, "get_token", fake_get_token)

    probe_calls = []

    def fake_probe(scope):
        probe_calls.append(scope)
        claims = {"upn": "testuser@example.com", "tid": "home-tenant"}
        return _FakeAccessToken(_fake_jwt(claims), int(time.time()) + 3600)

    monkeypatch.setattr(auth_token, "_silent_probe_token", fake_probe)

    info = auth_token.get_arm_default_token()

    assert info.token
    assert probe_calls == ["https://management.azure.com/.default"]


def test_arm_default_token_probe_miss_raises_not_signed_in(monkeypatch):
    """When neither the primary silent path nor the cache probe can mint a
    token, surface not_signed_in — never pop a browser (which would race the
    serialized ensure_signed_in flow and collide the OAuth state)."""
    auth_token._signed_in = True

    def fake_get_token(**kwargs):
        raise auth_token.AuthError("not_signed_in", "not signed in")

    monkeypatch.setattr(auth_token, "get_token", fake_get_token)
    monkeypatch.setattr(auth_token, "_silent_probe_token", lambda scope: None)

    with pytest.raises(auth_token.AuthError) as exc:
        auth_token.get_arm_default_token()
    assert exc.value.code == "not_signed_in"

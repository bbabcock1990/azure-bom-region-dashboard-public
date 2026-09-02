"""Tests for the stateless multi-customer delegated-token path.

In the hosted multi-tenant deployment the server holds no customer tokens or
data across requests: each call carries the signed-in customer's ARM access
token (minted in their browser) plus a stable user key, bound to context vars
for the request only. These tests cover that path in ``auth_token`` and the
per-customer storage-root isolation in ``storage``.
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from api._shared import auth_token
from api._shared import storage


def _fake_jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("MANAGED_IDENTITY_MODE", raising=False)
    monkeypatch.delenv("DELEGATED_MODE", raising=False)
    monkeypatch.delenv("MULTIUSER_ISOLATION", raising=False)
    auth_token.reset_for_tests()
    yield
    auth_token.reset_for_tests()


def test_delegated_mode_flag(monkeypatch):
    assert auth_token.delegated_mode() is False
    monkeypatch.setenv("DELEGATED_MODE", "true")
    assert auth_token.delegated_mode() is True


def test_delegated_token_short_circuits_acquire(monkeypatch):
    """When a delegated ARM token is bound, get_token() returns it verbatim and
    never touches the interactive credential."""
    monkeypatch.setattr(
        auth_token, "_credentials",
        lambda: (_ for _ in ()).throw(AssertionError("interactive path used")),
    )
    jwt = _fake_jwt({"upn": "cust@contoso.com", "tid": "cust-tenant",
                     "exp": int(time.time()) + 3600})
    auth_token.set_request_context(arm_token=jwt, user_key="oid-123")
    try:
        info = auth_token.get_token()
        assert info.token == jwt
        assert info.az_user == "cust@contoso.com"
        assert info.az_tenant == "cust-tenant"
        assert info.expires_in_seconds > 60
    finally:
        auth_token.clear_request_context()


def test_delegated_token_bypasses_shared_cache(monkeypatch):
    """Two different customers' tokens must never be served from the shared
    process cache, and the delegated path never invokes the credential."""
    class _RecordingCred:
        def __init__(self):
            self.calls = []

        def get_token(self, scope, **kwargs):
            self.calls.append((scope, kwargs))
            raise AssertionError("credential should not be used on delegated path")

    rec = _RecordingCred()
    monkeypatch.setattr(auth_token, "_credentials", lambda: rec)
    jwt_a = _fake_jwt({"upn": "a@a.com", "tid": "ta", "exp": int(time.time()) + 3600})
    jwt_b = _fake_jwt({"upn": "b@b.com", "tid": "tb", "exp": int(time.time()) + 3600})

    auth_token.set_request_context(arm_token=jwt_a, user_key="oid-a")
    assert auth_token.get_token().token == jwt_a
    auth_token.clear_request_context()

    auth_token.set_request_context(arm_token=jwt_b, user_key="oid-b")
    assert auth_token.get_token().token == jwt_b
    auth_token.clear_request_context()

    assert rec.calls == []  # delegated path never touched the credential

    # No delegated token bound and not signed in -> not_signed_in (nothing leaked
    # from the shared cache).
    monkeypatch.setattr(auth_token, "has_cached_account", lambda: False)
    with pytest.raises(auth_token.AuthError) as ex:
        auth_token.get_token(allow_interactive=False)
    assert ex.value.code == "not_signed_in"


def test_delegated_mode_without_token_requires_signin(monkeypatch):
    """DELEGATED_MODE on but no token attached -> the customer must re-auth."""
    monkeypatch.setenv("DELEGATED_MODE", "true")
    monkeypatch.setattr(
        auth_token, "_credentials",
        lambda: (_ for _ in ()).throw(AssertionError("interactive path used")),
    )
    with pytest.raises(auth_token.AuthError) as ex:
        auth_token.get_token()
    assert ex.value.code == "not_signed_in"


def test_delegated_has_cached_account_true_when_token_bound():
    jwt = _fake_jwt({"upn": "c@c.com", "tid": "t", "exp": int(time.time()) + 3600})
    auth_token.set_request_context(arm_token=jwt, user_key="oid")
    try:
        assert auth_token.has_cached_account() is True
    finally:
        auth_token.clear_request_context()


def test_current_user_key_roundtrip():
    assert auth_token.current_user_key() is None
    auth_token.set_request_context(user_key="oid-xyz")
    try:
        assert auth_token.current_user_key() == "oid-xyz"
    finally:
        auth_token.clear_request_context()
    assert auth_token.current_user_key() is None


def test_storage_root_partitioned_per_user(monkeypatch, tmp_path):
    """With MULTIUSER_ISOLATION on, each signed-in customer gets an isolated
    storage root under u/<key>; different customers never collide."""
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("MULTIUSER_ISOLATION", "true")

    auth_token.set_request_context(user_key="oid-a")
    root_a = storage.storage_root()
    auth_token.clear_request_context()

    auth_token.set_request_context(user_key="oid-b")
    root_b = storage.storage_root()
    auth_token.clear_request_context()

    assert root_a != root_b
    assert root_a.endswith("oid-a")
    assert root_b.endswith("oid-b")


def test_storage_root_flat_without_isolation_flag(monkeypatch, tmp_path):
    """Isolation is opt-in: without the flag the layout stays flat even if a
    user key is bound (local single-user mode / tests)."""
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    auth_token.set_request_context(user_key="oid-a")
    try:
        assert storage.storage_root() == str(tmp_path)
    finally:
        auth_token.clear_request_context()


def test_user_key_sanitized(monkeypatch, tmp_path):
    """A hostile user key can't traverse outside the storage root."""
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("MULTIUSER_ISOLATION", "true")
    auth_token.set_request_context(user_key="../../etc/passwd")
    try:
        root = storage.storage_root()
        tail = root.replace(str(tmp_path), "")
        assert ".." not in tail
    finally:
        auth_token.clear_request_context()

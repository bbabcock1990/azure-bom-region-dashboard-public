"""Tests for the local-user stub in ``api/_shared/auth.py``."""
from __future__ import annotations

import importlib
import sys

import pytest


def _reload_auth():
    """Force the module to recompute its cached username."""
    if "_shared.auth" in sys.modules:
        del sys.modules["_shared.auth"]
    return importlib.import_module("_shared.auth")


def test_principal_has_local_oid_and_admin_role(monkeypatch):
    monkeypatch.setenv("USERNAME", "bbabcock")
    monkeypatch.delenv("USER", raising=False)
    auth = _reload_auth()
    p = auth.get_local_user()
    assert p.email == "bbabcock"
    assert p.oid == "local"
    assert p.tenant_id == "local"
    assert p.raw_provider == "local"
    assert "admin" in p.roles


def test_username_is_sanitized_and_lowercased(monkeypatch):
    monkeypatch.setenv("USERNAME", "  Some Weird@Name!! 123  ")
    monkeypatch.delenv("USER", raising=False)
    auth = _reload_auth()
    p = auth.get_local_user()
    # Spaces and "@!!" stripped; segments joined with "-"; lowercased.
    assert p.email == "some-weird-name-123"


def test_fallback_when_no_username_found(monkeypatch):
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    auth = _reload_auth()

    # Replace getpass.getuser so it raises (simulates a stripped env).
    def _boom():
        raise OSError("no username")
    monkeypatch.setattr(auth.getpass, "getuser", _boom)
    # Bust the per-process cache so the patched getpass actually runs.
    auth._CACHED_USERNAME = None
    p = auth.get_local_user()
    assert p.email == "local-user"


def test_username_length_is_capped(monkeypatch):
    monkeypatch.setenv("USERNAME", "a" * 200)
    monkeypatch.delenv("USER", raising=False)
    auth = _reload_auth()
    p = auth.get_local_user()
    assert len(p.email) == 64
    assert p.email == "a" * 64


def test_req_argument_is_accepted_but_ignored(monkeypatch):
    monkeypatch.setenv("USERNAME", "user1")
    auth = _reload_auth()
    sentinel = object()
    # The arg is accepted but the function returns the same Principal.
    p1 = auth.get_local_user(req=sentinel)
    p2 = auth.get_local_user()
    assert p1 == p2

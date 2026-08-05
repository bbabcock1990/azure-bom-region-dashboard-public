"""Tests for the Origin/Referer guard in ``api/_shared/csrf.py``.

The dashboard is anonymous + local-only, so the guard exists only to
defend against drive-by cross-origin POSTs to localhost while the dev
stack is running.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from _shared import csrf


def _req(method: str, headers: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.method = method
    r.headers = headers or {}
    return r


def test_safe_for_get_when_allowed_origin_set(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGIN", "http://localhost:4280")
    # GET requests are always safe — no origin check at all.
    csrf.assert_safe_origin(_req("GET"))
    csrf.assert_safe_origin(_req("HEAD"))
    csrf.assert_safe_origin(_req("OPTIONS"))


def test_safe_when_no_allowed_origin_configured(monkeypatch):
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)
    # No env set → no-op for every method, including POST.
    csrf.assert_safe_origin(_req("POST"))
    csrf.assert_safe_origin(_req("POST", {"Origin": "https://evil.example"}))


def test_post_passes_with_matching_origin(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGIN", "http://localhost:4280")
    csrf.assert_safe_origin(_req("POST", {"Origin": "http://localhost:4280"}))


def test_post_passes_with_matching_referer(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGIN", "http://localhost:4280")
    csrf.assert_safe_origin(
        _req("POST", {"Referer": "http://localhost:4280/index.html"})
    )


def test_post_rejected_when_origin_wrong(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGIN", "http://localhost:4280")
    with pytest.raises(csrf.OriginError):
        csrf.assert_safe_origin(_req("POST", {"Origin": "https://evil.example"}))


def test_post_rejected_when_no_origin_or_referer(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGIN", "http://localhost:4280")
    with pytest.raises(csrf.OriginError):
        csrf.assert_safe_origin(_req("POST", {}))


def test_post_rejected_for_prefix_match_origin(monkeypatch):
    """Regression: a prefix match must NOT pass. http://localhost:42800 (a
    different port) shares the allowed origin as a string prefix but is a
    distinct, untrusted origin."""
    monkeypatch.setenv("ALLOWED_ORIGIN", "http://localhost:4280")
    with pytest.raises(csrf.OriginError):
        csrf.assert_safe_origin(_req("POST", {"Origin": "http://localhost:42800"}))


def test_post_rejected_for_prefix_match_referer(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGIN", "http://localhost:4280")
    with pytest.raises(csrf.OriginError):
        csrf.assert_safe_origin(
            _req("POST", {"Referer": "http://localhost:42800/index.html"})
        )


def test_post_rejected_for_lookalike_host(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGIN", "http://localhost:4280")
    with pytest.raises(csrf.OriginError):
        csrf.assert_safe_origin(
            _req("POST", {"Origin": "http://localhost.evil.com:4280"})
        )


def test_allowed_origin_with_trailing_slash_still_matches(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGIN", "http://localhost:4280/")
    csrf.assert_safe_origin(_req("POST", {"Origin": "http://localhost:4280"}))

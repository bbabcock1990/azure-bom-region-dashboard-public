"""Targeted tests for /api/runs auth fallback behavior."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

from _shared import auth_token, httpfunc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import runs_post


TARGET_SUB = "11111111-1111-1111-1111-111111111111"
OPERATOR_SUB = "22222222-2222-2222-2222-222222222222"


def _token_info(*, token: str, subscription: str = "", tenant: str = "tenant") -> auth_token.TokenInfo:
    return auth_token.TokenInfo(
        token=token,
        expires_at=datetime.now(timezone.utc),
        az_user="user@example.com",
        az_tenant=tenant,
        az_subscription=subscription,
    )


def _req():
    return httpfunc.HttpRequest(
        method="POST",
        url="http://localhost/api/runs",
        form={"subscription_id": TARGET_SUB},
        files={"step2_xlsx": httpfunc.UploadedFile("step2.xlsx", b"x")},
    )


def _wire_common(monkeypatch):
    monkeypatch.setattr(runs_post.csrf, "assert_safe_origin", lambda req: None)
    monkeypatch.setattr(
        runs_post.auth,
        "get_local_user",
        lambda req=None: SimpleNamespace(email="user@example.com", oid="oid-1"),
    )
    monkeypatch.setattr(runs_post.compile_mod, "new_run_id", lambda: "run-1")
    monkeypatch.setattr(runs_post.compile_mod, "insert_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(runs_post.compile_mod, "persist_snapshot", lambda snapshot, **kwargs: "snapshots/run-1.json")
    monkeypatch.setattr(runs_post.activity_log, "record", lambda *args, **kwargs: None)


def test_runs_post_falls_back_to_operator_subscription(monkeypatch):
    _wire_common(monkeypatch)
    monkeypatch.setattr(
        runs_post.auth_token,
        "get_arm_token",
        lambda sub_id, force_refresh=False: (_ for _ in ()).throw(
            runs_post.auth_token.AuthError("cross_tenant_not_guest", "User is not a guest in the target tenant.")
        ),
    )
    monkeypatch.setattr(
        runs_post.auth_token,
        "get_arm_default_token",
        lambda force_refresh=False: _token_info(token="global-token"),
    )
    monkeypatch.setattr(
        runs_post.auth_token,
        "list_subscriptions",
        lambda: [{"id": OPERATOR_SUB}],
    )

    seen = {}

    def fake_compile_snapshot(**kwargs):
        seen["subscriptions"] = kwargs["subscriptions"]
        return {
            "meta": {
                "mode": "global_unscoped",
                "mode_note": "Per-subscription restrictions not evaluated — operator lacks Reader on target subscription",
                "per_sub_status": {},
                "skus_source": "bom_sheet",
                "skus_resolved": [],
                "sku_query_subscription_id": OPERATOR_SUB,
            }
        }

    monkeypatch.setattr(runs_post.compile_mod, "compile_snapshot", fake_compile_snapshot)

    resp = runs_post.main(_req())
    body = json.loads(resp.get_body())

    assert resp.status_code == 200
    assert body["mode"] == "global_unscoped"
    assert seen["subscriptions"][0]["subscription_id"] == TARGET_SUB.lower()
    assert seen["subscriptions"][0]["status"] == "no_access"
    assert seen["subscriptions"][1]["subscription_id"] == OPERATOR_SUB.lower()
    assert seen["subscriptions"][1]["role"] == "operator_fallback"


def test_runs_post_retries_after_arm_401(monkeypatch):
    _wire_common(monkeypatch)
    calls = []

    def fake_get_arm_token(sub_id, force_refresh=False):
        calls.append((sub_id, force_refresh))
        return _token_info(token="fresh-token" if force_refresh else "stale-token", subscription=TARGET_SUB)

    monkeypatch.setattr(runs_post.auth_token, "get_arm_token", fake_get_arm_token)
    monkeypatch.setattr(
        runs_post.auth_token,
        "get_arm_default_token",
        lambda force_refresh=False: _token_info(token="home-token", subscription=TARGET_SUB),
    )
    monkeypatch.setattr(
        runs_post.auth_token,
        "list_subscriptions",
        lambda: [{"id": TARGET_SUB}],
    )

    compile_calls = {"count": 0}

    def fake_compile_snapshot(**kwargs):
        compile_calls["count"] += 1
        if compile_calls["count"] == 1:
            raise runs_post.compile_mod.CompileError(
                "arm_arm_token_expired",
                "ARM rejected the token (401). Re-mint and retry.",
                401,
            )
        return {
            "meta": {
                "mode": "subscription_scoped",
                "mode_note": None,
                "per_sub_status": {},
                "skus_source": "bom_sheet",
                "skus_resolved": [],
                "sku_query_subscription_id": TARGET_SUB.lower(),
            }
        }

    monkeypatch.setattr(runs_post.compile_mod, "compile_snapshot", fake_compile_snapshot)

    resp = runs_post.main(_req())
    body = json.loads(resp.get_body())

    assert resp.status_code == 200
    assert body["mode"] == "subscription_scoped"
    assert compile_calls["count"] == 2
    assert calls == [(TARGET_SUB.lower(), False), (TARGET_SUB.lower(), True)]

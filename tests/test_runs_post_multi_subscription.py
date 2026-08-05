import json
import importlib
import os
import sys
from types import SimpleNamespace

from _shared import httpfunc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
runs_post = importlib.import_module("api.runs_post")


def test_runs_post_accepts_subscription_ids_and_marks_no_access(monkeypatch):
    monkeypatch.setattr(runs_post.csrf, "assert_safe_origin", lambda req: None)
    monkeypatch.setattr(
        runs_post.auth,
        "get_local_user",
        lambda req: SimpleNamespace(email="user@example.com", oid="oid-1"),
    )
    monkeypatch.setattr(runs_post.compile_mod, "new_run_id", lambda: "run-1")
    monkeypatch.setattr(runs_post.compile_mod, "insert_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(runs_post.compile_mod, "persist_snapshot", lambda *args, **kwargs: "snapshots/blob.json")
    monkeypatch.setattr(runs_post.activity_log, "record", lambda *args, **kwargs: None)

    captured = {}

    def fake_compile_snapshot(**kwargs):
        captured.update(kwargs)
        return {
            "regions": [],
            "meta": {
                "subscription_id": kwargs["subscriptions"][0]["subscription_id"],
                "subscription_ids": [s["subscription_id"] for s in kwargs["subscriptions"]],
                "per_sub_status": {
                    s["subscription_id"]: {
                        "status": s.get("status") or ("ok" if s.get("arm_token") else "no_access"),
                        **({"error": s["error"]} if s.get("error") else {}),
                    }
                    for s in kwargs["subscriptions"]
                },
                "skus_source": "bom_data",
                "skus_resolved": [],
            },
        }

    monkeypatch.setattr(runs_post.compile_mod, "compile_snapshot", fake_compile_snapshot)

    def fake_get_arm_token(sub_id):
        if sub_id.endswith("0002"):
            raise runs_post.auth_token.AuthError("not_signed_in", "not signed in")
        return SimpleNamespace(token=f"tok-{sub_id[-4:]}", az_tenant="tenant-a")

    monkeypatch.setattr(runs_post.auth_token, "get_arm_token", fake_get_arm_token)
    monkeypatch.setattr(
        runs_post.auth_token,
        "get_arm_default_token",
        lambda force_refresh=False: (_ for _ in ()).throw(
            runs_post.auth_token.AuthError("not_signed_in", "not signed in")
        ),
    )

    req = httpfunc.HttpRequest(
        method="POST",
        url="http://local/api/runs",
        headers={"origin": "http://localhost"},
        form={
            "subscription_id": "00000000-0000-0000-0000-000000000001",
            "subscription_ids": "00000000-0000-0000-0000-000000000001, 00000000-0000-0000-0000-000000000002",
            "customer_name": "Contoso",
            "customer_segments": "EA,ANY",
        },
        files={
            "step2_xlsx": httpfunc.UploadedFile("region_results_test.xlsx", b"fake-xlsx"),
        },
    )

    resp = runs_post._main(req)
    body = json.loads(resp.get_body().decode("utf-8"))

    assert resp.status_code == 200
    assert [s["subscription_id"] for s in captured["subscriptions"]] == [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    ]
    assert captured["subscriptions"][0]["status"] == "ok"
    assert captured["subscriptions"][1]["status"] == "no_access"
    assert body["subscription_ids"] == [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    ]
    assert body["per_sub_status"]["00000000-0000-0000-0000-000000000002"]["status"] == "no_access"

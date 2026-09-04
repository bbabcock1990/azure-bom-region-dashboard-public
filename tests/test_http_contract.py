"""HTTP contract tests for the local FastAPI host (server/app.py).

These exercise the request/response adapter, route table, route precedence,
multipart upload, JSON body parsing, security headers, and the SQLite/file
storage backend end-to-end — i.e. everything that the Azure Functions + SWA
emulator stack used to provide. Network/az-dependent endpoints are not hit.
"""
import io
import os

import openpyxl
import pytest
from fastapi.testclient import TestClient

SUB = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    # Point storage at a throwaway dir and disable the origin guard so writes
    # don't require an Origin header. Env is read per-request by storage.py.
    os.environ["LOCAL_STORAGE_DIR"] = str(tmp_path_factory.mktemp("store"))
    os.environ.pop("ALLOWED_ORIGIN", None)
    from server.app import app
    return TestClient(app)


def test_static_index_and_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<html" in r.text.lower()
    assert "app.js?v=2026090401" in r.text
    assert "styles.css?v=2026090401" in r.text
    assert "Content-Security-Policy" in r.headers
    assert r.headers.get("X-Content-Type-Options") == "nosniff"


def test_static_assets_explain_ranking_evaluation(client):
    app_js = client.get("/app.js")
    assert app_js.status_code == 200
    assert "How rankings work" in app_js.text
    assert "score = verdictBucket×1000 + confidenceBucket×120 + quotaBucket×80" in app_js.text
    assert "best bucket is 0" in app_js.text
    assert 'role="dialog" aria-modal="true" aria-label="How rankings are evaluated"' in app_js.text
    assert "latency, estimated monthly cost, and region name are tie-breakers" in app_js.text

    styles = client.get("/styles.css")
    assert styles.status_code == 200
    assert ".ranking-legend-list" in styles.text
    assert ".ranking-formula" in styles.text


@pytest.mark.parametrize("path", [
    "/api/snapshots",
    "/api/bom/service_catalog",
    "/api/bom/region_catalog",
    "/api/subscription_metadata",
    "/api/activity_log",
])
def test_simple_get_endpoints_return_json(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert isinstance(r.json(), (dict, list))


def test_run_progress_unknown_token(client):
    r = client.get("/api/run_progress?token=not-a-uuid")
    assert r.status_code == 200
    assert r.json().get("found") is False


def test_snapshots_latest_route_precedence(client):
    # Must hit the snapshots_latest handler, not snapshots/{run_id}.
    r = client.get("/api/snapshots/latest")
    assert r.status_code == 404
    assert r.json().get("error") == "no_snapshots"


def test_subscription_metadata_round_trip(client):
    payload = {
        "subscription_id": SUB,
        "tag": "Contract Test", "customer_name": "Contoso",
        "customer_segments": "EA,ANY",
        "required_skus": [], "services": [], "regions": [],
    }
    # POST creates a new BOM with a server-allocated bom_id.
    created = client.post("/api/subscription_metadata", json=payload)
    assert created.status_code == 201
    body = created.json()
    bom_id = body["bom_id"]
    assert bom_id and body["subscription_id"] == SUB and body["tag"] == "Contract Test"

    got = client.get(f"/api/subscription_metadata/{bom_id}")
    assert got.status_code == 200 and got.json().get("tag") == "Contract Test"

    # PUT updates the existing BOM by id.
    upd = client.put(f"/api/subscription_metadata/{bom_id}",
                     json={**payload, "tag": "Renamed"})
    assert upd.status_code == 200 and upd.json().get("tag") == "Renamed"

    assert client.delete(f"/api/subscription_metadata/{bom_id}").status_code in (200, 204)


def test_two_boms_same_subscription_are_distinct(client):
    """Creating two BOMs for one subscription yields two independent records."""
    base = {"subscription_id": SUB, "customer_segments": "EA,ANY",
            "required_skus": [], "services": [], "regions": []}
    a = client.post("/api/subscription_metadata", json={**base, "tag": "Workload A"})
    b = client.post("/api/subscription_metadata", json={**base, "tag": "Workload B"})
    assert a.status_code == 201 and b.status_code == 201
    id_a, id_b = a.json()["bom_id"], b.json()["bom_id"]
    assert id_a != id_b

    items = client.get("/api/subscription_metadata").json()["items"]
    by_id = {m["bom_id"]: m for m in items}
    assert by_id[id_a]["tag"] == "Workload A"
    assert by_id[id_b]["tag"] == "Workload B"
    assert by_id[id_a]["subscription_id"] == by_id[id_b]["subscription_id"] == SUB

    # Deleting one leaves the other intact.
    client.delete(f"/api/subscription_metadata/{id_a}")
    remaining = {m["bom_id"] for m in client.get("/api/subscription_metadata").json()["items"]}
    assert id_b in remaining and id_a not in remaining
    client.delete(f"/api/subscription_metadata/{id_b}")


def _region_results_xlsx() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Region Results"
    ws.append(["", "", "", "pad"])
    ws.append([])
    ws.append(["Region", "Zone", "Status", "Azure Automation"])
    ws.append(["eastus", "1", "ok", "yes"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_import_xlsx_multipart(client):
    r = client.post(
        "/api/bom/import_xlsx",
        files={"file": ("region_results_contoso.xlsx", _region_results_xlsx(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body.get("source_format") == "region_results"
    assert body.get("customer_name") == "contoso"


def test_import_xlsx_missing_file(client):
    r = client.post("/api/bom/import_xlsx", files={"wrongfield": ("x.xlsx", b"x")})
    assert r.status_code == 400
    assert r.json().get("error") == "missing_file"

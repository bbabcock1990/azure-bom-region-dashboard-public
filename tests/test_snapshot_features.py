import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from _shared import bom_storage, storage

HERE = Path(__file__).resolve().parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)
    from server.app import app

    return TestClient(app)


def _persist_run_and_snapshot(*, bom_id, run_id, snapshot, ended_at=None):
    blob_name = f"{bom_id}/{run_id}.json"
    storage.get_blob_container("snapshots").upload_blob(
        name=blob_name,
        data=json.dumps(snapshot, ensure_ascii=False).encode("utf-8"),
        overwrite=True,
    )
    storage.get_table_client("runs").upsert_entity(
        {
            "PartitionKey": bom_id,
            "RowKey": run_id,
            "status": "succeeded",
            "snapshot_blob": blob_name,
            "started_at": ended_at or "2026-06-29T19:00:00Z",
            "ended_at": ended_at or "2026-06-29T19:05:00Z",
            "subscription_id": "11111111-2222-3333-4444-555555555555",
        }
    )


def test_snapshot_diff_reports_improvement_and_blocker_changes(client):
    bom = bom_storage.create(
        "11111111-2222-3333-4444-555555555555",
        tag="Diff Test",
        customer_name="Contoso",
        customer_segments="EA,ANY",
        required_skus=[
            {
                "primary_family": "standardDav6Family",
                "primary_label": "Dav6",
                "alt_family": "standardDASv5Family",
                "alt_label": "DASv5",
                "required_cores": 32,
            }
        ],
        services=[{"name": "Azure Firewall"}],
        regions=["eastus"],
        updated_by="test@example.com",
    )
    bom_id = bom["bom_id"]

    before = {
        "meta": {
            "compiled_at": "2026-06-29T19:59:45Z",
            "skus_resolved": bom["required_skus"],
        },
        "regions": [
            {
                "name": "East US",
                "short": "eastus",
                "deployment_health": "No",
                "status": "BOM & Compute Issue",
                "fell_back": False,
                "has_zone_restriction": True,
                "missing_services": [{"service": "Azure Firewall", "detail": "not available"}],
                "sku_blockers": ["Dav6 only in Zone 1/2"],
                "sku_fallbacks": [],
                "quota_status": "insufficient",
                "sku_zone_detail": {"Dav6": [True, True, False], "DASv5": [False, False, False]},
            }
        ],
    }
    after = {
        "meta": {
            "compiled_at": "2026-06-29T20:24:03Z",
            "skus_resolved": bom["required_skus"],
        },
        "regions": [
            {
                "name": "East US",
                "short": "eastus",
                "deployment_health": "Yes",
                "status": "OK",
                "fell_back": False,
                "has_zone_restriction": False,
                "missing_services": [],
                "sku_blockers": [],
                "sku_fallbacks": [],
                "quota_status": "sufficient",
                "sku_zone_detail": {"Dav6": [True, True, True], "DASv5": [True, True, True]},
            }
        ],
    }

    _persist_run_and_snapshot(
        bom_id=bom_id,
        run_id="2026-06-29T19-59-45Z-before",
        snapshot=before,
        ended_at="2026-06-29T19:59:45Z",
    )
    _persist_run_and_snapshot(
        bom_id=bom_id,
        run_id="2026-06-29T20-24-03Z-after",
        snapshot=after,
        ended_at="2026-06-29T20:24:03Z",
    )

    res = client.get("/api/snapshots/diff?a=2026-06-29T19-59-45Z-before&b=2026-06-29T20-24-03Z-after")
    assert res.status_code == 200
    body = res.json()
    assert body["summary"]["regions_improved"] == 1
    assert body["summary"]["new_blockers"] == 0
    assert body["summary"]["resolved_blockers"] >= 2
    assert body["changes"][0]["region"] == "eastus"
    assert body["changes"][0]["direction"] == "improved"
    assert body["changes"][0]["verdict_before"] == "not_recommended"
    assert body["changes"][0]["verdict_after"] == "ready"
    assert any("Quota changed" in detail for detail in body["changes"][0]["details"])


def test_bom_sensitivity_ranks_constraints_from_snapshot(client):
    bom = bom_storage.create(
        "11111111-2222-3333-4444-555555555555",
        tag="Sensitivity Test",
        customer_name="Contoso",
        customer_segments="EA,ANY",
        required_skus=[
            {
                "primary_family": "standardDav6Family",
                "primary_label": "Dav6",
                "alt_family": "standardDASv5Family",
                "alt_label": "DASv5",
                "required_cores": 32,
            },
            {
                "primary_family": "standardEav6Family",
                "primary_label": "Eav6",
                "alt_family": "standardEASv5Family",
                "alt_label": "EASv5",
                "required_cores": 32,
            },
        ],
        services=[{"name": "Azure Firewall"}],
        regions=["eastus", "westus", "centralus"],
        updated_by="test@example.com",
    )
    bom_id = bom["bom_id"]

    snapshot = {
        "meta": {
            "compiled_at": "2026-06-29T20:34:44Z",
            "skus_resolved": bom["required_skus"],
        },
        "regions": [
            {
                "name": "East US",
                "short": "eastus",
                "deployment_health": "No",
                "status": "BOM Issue",
                "missing_services": [{"service": "Azure Firewall", "detail": "not available"}],
                "sku_blockers": [],
                "quota_status": "sufficient",
                "sku_zone_detail": {
                    "Dav6": [True, True, True],
                    "DASv5": [True, True, True],
                    "Eav6": [True, True, True],
                    "EASv5": [True, True, True],
                },
            },
            {
                "name": "West US",
                "short": "westus",
                "deployment_health": "No",
                "status": "Compute Issue",
                "missing_services": [],
                "sku_blockers": ["Dav6 only in Zone 1/2"],
                "quota_status": "sufficient",
                "sku_zone_detail": {
                    "Dav6": [True, True, False],
                    "DASv5": [False, False, False],
                    "Eav6": [True, True, True],
                    "EASv5": [True, True, True],
                },
            },
            {
                "name": "Central US",
                "short": "centralus",
                "deployment_health": "No",
                "status": "Compute Issue",
                "missing_services": [],
                "sku_blockers": ["Dav6 only in Zone 1/2", "Eav6 only in Zone 1/2"],
                "quota_status": "sufficient",
                "sku_zone_detail": {
                    "Dav6": [True, True, False],
                    "DASv5": [True, True, True],
                    "Eav6": [True, False, False],
                    "EASv5": [False, False, False],
                },
            },
        ],
    }

    _persist_run_and_snapshot(
        bom_id=bom_id,
        run_id="2026-06-29T20-34-44Z-sensitivity",
        snapshot=snapshot,
        ended_at="2026-06-29T20:34:44Z",
    )

    res = client.get(f"/api/bom/sensitivity?bom_id={bom_id}&run_id=2026-06-29T20-34-44Z-sensitivity")
    assert res.status_code == 200
    body = res.json()
    constraints = body["constraints"]
    by_type = {(item["type"], item["id"]): item for item in constraints}

    service = by_type[("service", "azure-firewall")]
    assert service["regions_excluded"] == 1
    assert service["excluded_regions"] == ["eastus"]

    dav6 = by_type[("sku_family", "standardDav6Family")]
    assert dav6["regions_excluded"] == 1
    assert dav6["excluded_regions"] == ["westus"]

    zone = by_type[("zone_requirement", "3-zone-availability")]
    assert zone["regions_excluded"] == 2
    assert zone["excluded_regions"] == ["centralus", "westus"]


def test_backfill_meta_timestamp_injects_when_missing():
    from _shared import snapshot_store

    snapshot = {"meta": {"subscription_id": "sub"}, "regions": []}
    payload = json.dumps(snapshot).encode("utf-8")
    run_entity = {"RowKey": "r1", "ended_at": "2026-06-29T20:34:44Z"}

    out = snapshot_store.backfill_meta_timestamp(payload, run_entity)
    parsed = json.loads(out)
    assert parsed["meta"]["compiled_at"] == "2026-06-29T20:34:44Z"


def test_backfill_meta_timestamp_preserves_existing():
    from _shared import snapshot_store

    snapshot = {"meta": {"compiled_at": "2026-01-01T00:00:00Z"}, "regions": []}
    payload = json.dumps(snapshot).encode("utf-8")
    run_entity = {"RowKey": "r1", "ended_at": "2026-06-29T20:34:44Z"}

    out = snapshot_store.backfill_meta_timestamp(payload, run_entity)
    assert json.loads(out)["meta"]["compiled_at"] == "2026-01-01T00:00:00Z"


def test_backfill_meta_timestamp_returns_original_on_bad_json():
    from _shared import snapshot_store

    payload = b"{not valid json"
    assert snapshot_store.backfill_meta_timestamp(payload, {"RowKey": "r1"}) == payload


def test_snapshots_export_bundles_snapshots_into_zip(client):
    import io
    import zipfile

    _persist_run_and_snapshot(
        bom_id="demo-bom", run_id="20260629-190500-a",
        snapshot={"meta": {"subscription_id": "sub"}, "regions": []})
    _persist_run_and_snapshot(
        bom_id="demo-bom", run_id="20260630-190500-b",
        snapshot={"meta": {"subscription_id": "sub"}, "regions": []})

    res = client.get("/api/snapshots/export")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    assert "attachment" in res.headers.get("content-disposition", "")

    zf = zipfile.ZipFile(io.BytesIO(res.content))
    names = zf.namelist()
    assert "index.json" in names
    snap_files = [n for n in names if n.startswith("snapshots/") and n.endswith(".json")]
    assert len(snap_files) == 2
    manifest = json.loads(zf.read("index.json"))
    assert manifest["count"] == 2


def test_snapshots_export_empty_is_valid_zip(client):
    import io
    import zipfile

    res = client.get("/api/snapshots/export")
    assert res.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(res.content))
    assert json.loads(zf.read("index.json"))["count"] == 0


def test_open_folder_refused_when_not_local(client, monkeypatch):
    monkeypatch.delenv("LOCAL_MODE", raising=False)
    res = client.post("/api/local-state/open-folder")
    assert res.status_code == 400
    assert res.json()["error"] == "not_local"


def test_open_folder_opens_when_local(client, monkeypatch):
    monkeypatch.setenv("LOCAL_MODE", "true")
    called = {}
    from api import local_state_open_folder as mod
    monkeypatch.setattr(mod, "_open_in_explorer", lambda path: called.setdefault("path", path))
    res = client.post("/api/local-state/open-folder")
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert called.get("path", "").endswith("snapshots")


def test_snapshots_export_import_round_trip(client):
    _persist_run_and_snapshot(
        bom_id="demo-bom", run_id="20260629-190500-a",
        snapshot={"meta": {"subscription_id": "sub-xyz"}, "regions": []})
    _persist_run_and_snapshot(
        bom_id="demo-bom", run_id="20260630-190500-b",
        snapshot={"meta": {"subscription_id": "sub-xyz"}, "regions": []})

    exported = client.get("/api/snapshots/export").content

    # Wipe everything, then import the archive back.
    client.post("/api/local-state/wipe")
    assert client.get("/api/snapshots").json()["snapshots"] == []

    res = client.post(
        "/api/snapshots/import",
        files={"file": ("bom-snapshots.zip", exported, "application/zip")},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["imported"] == 2
    assert body["skipped"] == 0

    listed = client.get("/api/snapshots").json()["snapshots"]
    assert {s["run_id"] for s in listed} == {"20260629-190500-a", "20260630-190500-b"}
    # subscription_id round-trips via the manifest (entity value takes precedence)
    assert all(s["subscription_id"] == "11111111-2222-3333-4444-555555555555" for s in listed)
    # the snapshot blob is fetchable again
    got = client.get("/api/snapshots/20260629-190500-a")
    assert got.status_code == 200


def test_snapshots_import_recovers_subscription_from_payload(client):
    import io
    import zipfile

    # A v16-style archive: manifest lacks subscription_id, so import must recover
    # it from the snapshot payload's meta.
    buf = io.BytesIO()
    snap = json.dumps({"meta": {"subscription_id": "sub-from-meta"}, "regions": []})
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("snapshots/acme/20260701-000000-c.json", snap)
        zf.writestr("index.json", json.dumps({"snapshots": [
            {"run_id": "20260701-000000-c", "bom_id": "acme-bom",
             "file": "snapshots/acme/20260701-000000-c.json"},
        ]}))

    res = client.post(
        "/api/snapshots/import",
        files={"file": ("old.zip", buf.getvalue(), "application/zip")},
    )
    assert res.status_code == 200
    assert res.json()["imported"] == 1
    listed = client.get("/api/snapshots").json()["snapshots"]
    assert listed[0]["subscription_id"] == "sub-from-meta"


def test_snapshots_import_rejects_non_zip(client):
    res = client.post(
        "/api/snapshots/import",
        files={"file": ("not.zip", b"this is not a zip", "application/zip")},
    )
    assert res.status_code == 400
    assert res.json()["error"] == "bad_zip"


def test_snapshots_import_missing_file(client):
    res = client.post("/api/snapshots/import")
    assert res.status_code == 400
    assert res.json()["error"] in ("missing_file", "bad_request")


def test_export_import_restores_bom_definitions(client):
    # A real BOM in the left-panel list plus its analysis snapshot.
    bom = bom_storage.create(
        "11111111-2222-3333-4444-555555555555",
        tag="Restore Me",
        customer_name="Contoso",
        customer_segments="EA,ANY",
        required_skus=[],
        services=[{"name": "Azure Firewall"}],
        updated_by="tester@example.com",
    )
    _persist_run_and_snapshot(
        bom_id=bom["bom_id"], run_id="20260629-190500-a",
        snapshot={"meta": {"subscription_id": "11111111-2222-3333-4444-555555555555"},
                  "regions": []})

    exported = client.get("/api/snapshots/export").content

    # Wipe everything — both the BOM list and the run history are gone.
    client.post("/api/local-state/wipe")
    assert client.get("/api/subscription_metadata").json()["items"] == []

    res = client.post(
        "/api/snapshots/import",
        files={"file": ("bom-snapshots.zip", exported, "application/zip")},
    )
    assert res.status_code == 200
    assert res.json()["boms"] == 1

    boms = client.get("/api/subscription_metadata").json()["items"]
    assert len(boms) == 1
    assert boms[0]["bom_id"] == bom["bom_id"]
    assert boms[0]["tag"] == "Restore Me"
    assert boms[0]["customer_name"] == "Contoso"
    assert [s["name"] for s in boms[0]["services"]] == ["Azure Firewall"]


def test_import_reconstructs_bom_from_snapshot_when_manifest_lacks_boms(client):
    import io
    import zipfile

    # A pre-BOM-export archive: manifest has no "boms" array, so import must
    # rebuild a minimal BOM from the snapshot payload's meta.
    buf = io.BytesIO()
    snap = json.dumps({"meta": {
        "subscription_id": "11111111-2222-3333-4444-555555555555",
        "customer_name": "Acme",
        "customer_segments": "EA,ANY",
        "services": [{"name": "Azure Firewall"}],
        "skus_resolved": [],
    }, "regions": []})
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("snapshots/acme/20260701-000000-c.json", snap)
        zf.writestr("index.json", json.dumps({"snapshots": [
            {"run_id": "20260701-000000-c",
             "bom_id": "abcdef0123456789abcdef0123456789",
             "file": "snapshots/acme/20260701-000000-c.json"},
        ]}))

    res = client.post(
        "/api/snapshots/import",
        files={"file": ("old.zip", buf.getvalue(), "application/zip")},
    )
    assert res.status_code == 200
    assert res.json()["boms"] == 1
    boms = client.get("/api/subscription_metadata").json()["items"]
    assert len(boms) == 1
    assert boms[0]["bom_id"] == "abcdef0123456789abcdef0123456789"
    assert boms[0]["customer_name"] == "Acme"
    assert [s["name"] for s in boms[0]["services"]] == ["Azure Firewall"]

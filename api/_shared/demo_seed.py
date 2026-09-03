"""Demo/sample-data seeding for a customer's first run.

When ``DEMO_MODE=true`` and local storage has no BOMs yet, ``seed_if_empty()``
loads a bundled, scrubbed sample BOM + analysis snapshot (``fixtures/demo/``) so
the dashboard is fully populated *before* the customer signs into Azure. This
turns an empty first-run screen into a working example they can click through
(Overview, Table, Map, Quota, and the support-ticket dry-run flow).

Everything is best-effort and idempotent: if anything is missing or already
present, seeding silently no-ops. The bundled snapshot never contains real
subscription IDs (the generator scrubs them to a demo GUID).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

from . import storage

log = logging.getLogger(__name__)

_FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "demo"


def is_demo_mode() -> bool:
    return os.getenv("DEMO_MODE", "").strip().lower() in ("true", "1", "yes")


def _load(name: str) -> Optional[dict]:
    path = _FIXTURE_DIR / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.debug("demo_seed: could not load %s", path, exc_info=True)
        return None


def _has_any_bom() -> bool:
    try:
        rows = storage.get_table_client("subscriptionmetadata").query_entities(
            query_filter="PartitionKey eq @pk", parameters={"pk": "sub"}
        )
        return any(True for _ in rows)
    except Exception:
        return False


def seed_if_empty() -> bool:
    """Seed demo data when in demo mode and storage is empty. Returns True if a
    seed was performed. Never raises."""
    if not is_demo_mode():
        return False
    return seed(force=False)


def seed(force: bool = False) -> bool:
    """Load the bundled sample BOM + snapshot into local storage.

    Idempotent and best-effort. Unlike ``seed_if_empty`` this does **not**
    require ``DEMO_MODE`` — it powers the in-app "Explore with sample data"
    button so a live instance can populate an example on demand.

    - ``force=False`` (default): no-op if any BOM already exists, so an existing
      workspace is never disturbed.
    - ``force=True``: upsert the sample even when other BOMs exist.

    Returns True if a seed was performed. Never raises.
    """
    try:
        if not force and _has_any_bom():
            return False

        bom = _load("bom.json")
        run = _load("run.json")
        snapshot_path = _FIXTURE_DIR / "snapshot.json"
        if not bom or not run or not snapshot_path.exists():
            log.info("demo_seed: fixtures missing, skipping seed")
            return False

        # 1) BOM row
        bom_entity = {"PartitionKey": bom["pk"], "RowKey": bom["rk"]}
        bom_entity.update(bom["data"])
        storage.get_table_client("subscriptionmetadata").upsert_entity(bom_entity, mode="merge")

        # 2) Snapshot blob
        blob_name = run["data"].get("snapshot_blob")
        if blob_name:
            container = storage.get_blob_container("snapshots")
            container.upload_blob(
                blob_name,
                snapshot_path.read_text(encoding="utf-8"),
                overwrite=True,
                content_type="application/json",
            )

        # 3) Run row (references the blob)
        run_entity = {"PartitionKey": run["pk"], "RowKey": run["rk"]}
        run_entity.update(run["data"])
        storage.get_table_client("runs").upsert_entity(run_entity, mode="merge")

        log.info("demo_seed: seeded sample BOM %s (run %s)", bom["rk"], run["rk"])
        return True
    except Exception:
        log.exception("demo_seed: seeding failed (continuing without demo data)")
        return False


def sample_bom_id() -> Optional[str]:
    """RowKey (bom_id) of the bundled sample BOM, or None if unavailable."""
    bom = _load("bom.json")
    if bom:
        return bom.get("rk")
    return None

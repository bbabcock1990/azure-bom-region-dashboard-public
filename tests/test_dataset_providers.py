"""Tests for the live 'Refresh from Azure' dataset providers."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))

from _shared import dataset_providers as dp  # noqa: E402


def test_service_catalog_refresh_preserves_curated_tiers(monkeypatch):
    """An ARM 'Refresh from Azure' of the service catalog must keep the
    hand-authored tier lists (e.g. PostgreSQL editions). Dropping them would
    leave the override tier-less and the wizard would offer a tier the BOM
    save then rejects with 'has no tier ...'."""
    monkeypatch.setattr(dp, "_operator_context", lambda sub=None: ("tok", "sub-123"))

    # Pretend the subscription exposes the DBforPostgreSQL provider.
    def fake_arm_get_all(url, params, token):
        return [
            {"namespace": "Microsoft.DBforPostgreSQL",
             "resourceTypes": [{"resourceType": "flexibleServers"}]},
            {"namespace": "Microsoft.Sql",
             "resourceTypes": [{"resourceType": "servers"}]},
        ]

    monkeypatch.setattr(dp, "_arm_get_all", fake_arm_get_all)

    raw = dp.service_catalog_bytes("sub-123")
    doc = json.loads(raw.decode("utf-8"))
    by_name = {s["name"]: s for s in doc["services"]}

    pg = by_name.get("Azure Database for PostgreSQL")
    assert pg is not None, "PostgreSQL should survive the provider intersection"
    tier_ids = {t["id"] for t in pg.get("tiers") or []}
    assert "memory_optimized" in tier_ids
    assert "general_purpose" in tier_ids

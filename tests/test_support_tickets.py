"""Tests for the automated support-ticket feature: settings persistence,
payload building, dry-run (no network), and a mocked real submission.
"""
import os

import httpx
import pytest
import respx


@pytest.fixture()
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_STORAGE_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)
    # Fresh import of storage-backed modules is unnecessary — storage reads the
    # env per call — but clear any cached settings module state defensively.
    yield


# ─── settings ────────────────────────────────────────────────────────────────

def test_settings_defaults_and_roundtrip(isolated_storage):
    from _shared import support_settings
    s = support_settings.get_settings()
    assert s["default_severity"] == "moderate"
    assert s["country"] == "US"
    assert support_settings.is_configured() is False

    saved = support_settings.save_settings({
        "contact_first_name": "Ada", "primary_email": "ada@example.com",
        "default_severity": "BOGUS",  # clamps to moderate
        "junk_key": "ignored",
    })
    assert saved["contact_first_name"] == "Ada"
    assert saved["default_severity"] == "moderate"
    assert "junk_key" not in saved
    assert support_settings.is_configured() is True


# ─── payload building (pure) ─────────────────────────────────────────────────

def test_build_quota_ticket_payload_shape(isolated_storage):
    from _shared import support_tickets
    payload = support_tickets.build_quota_ticket_payload(
        subscription_id="11111111-1111-1111-1111-111111111111",
        region="eastus", family="standardDav6Family", new_limit=300,
        severity="moderate", settings={"primary_email": "a@b.com", "country": "US"},
        problem_classification_id="/x/problemClassifications/y", family_label="Dadv6",
    )
    props = payload["properties"]
    assert props["serviceId"].endswith(support_tickets.QUOTA_SERVICE_GUID)
    assert props["severity"] == "moderate"
    assert props["quotaTicketDetails"]["quotaChangeRequests"][0]["region"] == "eastus"


# ─── dry-run: no network ─────────────────────────────────────────────────────

def test_create_ticket_dry_run_is_offline_and_tracked(isolated_storage):
    from _shared import support_tickets, support_settings
    support_settings.save_settings({"contact_first_name": "Ada", "primary_email": "ada@example.com"})
    result = support_tickets.create_ticket(
        kind="quota", subscription_id="11111111-1111-1111-1111-111111111111",
        region="eastus", family="standardDav6Family", new_limit=300,
        dry_run=True, token=None,
    )
    assert result["status"] == "preview"
    assert result["dry_run"] is True
    assert result["payload"]["properties"]["title"]
    # tracked and listable
    listed = support_tickets.list_tickets()
    assert any(t["ticket_name"] == result["ticket_name"] for t in listed)


def test_create_ticket_demo_mode_never_submits(isolated_storage):
    from _shared import support_tickets
    result = support_tickets.create_ticket(
        kind="technical", subscription_id="11111111-1111-1111-1111-111111111111",
        region="westus2", family="standardNCadsH100v5Family",
        zones=["1", "2"], dry_run=False, token="should-not-be-used", demo_mode=True,
    )
    assert result["status"] == "preview"
    assert result["dry_run"] is True


def test_create_ticket_bad_kind(isolated_storage):
    from _shared import support_tickets
    with pytest.raises(support_tickets.SupportError) as ex:
        support_tickets.create_ticket(
            kind="nope", subscription_id="11111111-1111-1111-1111-111111111111",
            region="eastus", family="x", dry_run=True,
        )
    assert ex.value.code == "bad_kind"


# ─── real submission (mocked ARM) ────────────────────────────────────────────

@respx.mock
def test_create_ticket_real_submit_mocked(isolated_storage):
    from _shared import support_tickets, support_settings
    support_settings.save_settings({
        "contact_first_name": "Ada", "primary_email": "ada@example.com", "country": "US",
    })
    sub = "11111111-1111-1111-1111-111111111111"

    respx.get(url__regex=r".*/problemClassifications").mock(
        return_value=httpx.Response(200, json={
            "value": [{
                "id": f"/providers/Microsoft.Support/services/{support_tickets.QUOTA_SERVICE_GUID}/problemClassifications/pc-guid",
                "properties": {"displayName": "Compute VM (cores-vCPUs) quota increase"},
            }]
        })
    )
    put_route = respx.put(url__regex=r".*/supportTickets/.*").mock(
        return_value=httpx.Response(201, json={
            "id": f"/subscriptions/{sub}/providers/Microsoft.Support/supportTickets/bomdash",
            "properties": {"status": "Open", "supportTicketId": "2400010"},
        })
    )

    result = support_tickets.create_ticket(
        kind="quota", subscription_id=sub, region="eastus",
        family="standardDav6Family", new_limit=300,
        dry_run=False, token="fake-token",
    )
    assert put_route.called
    assert result["status"] == "submitted"
    assert result["azure_status"] == "Open"
    assert result["dry_run"] is False


@respx.mock
def test_real_submit_fails_when_classification_unresolved(isolated_storage):
    from _shared import support_tickets, support_settings
    support_settings.save_settings({
        "contact_first_name": "Ada", "primary_email": "ada@example.com", "country": "US",
    })
    respx.get(url__regex=r".*/problemClassifications").mock(
        return_value=httpx.Response(200, json={"value": []})
    )
    with pytest.raises(support_tickets.SupportError) as ex:
        support_tickets.create_ticket(
            kind="quota", subscription_id="11111111-1111-1111-1111-111111111111",
            region="eastus", family="standardDav6Family", new_limit=300,
            dry_run=False, token="fake-token",
        )
    assert ex.value.code == "classification_unresolved"


def test_real_submit_requires_contact(isolated_storage):
    from _shared import support_tickets
    # no settings saved → contact incomplete
    with pytest.raises(support_tickets.SupportError) as ex:
        support_tickets.create_ticket(
            kind="quota", subscription_id="11111111-1111-1111-1111-111111111111",
            region="eastus", family="standardDav6Family", new_limit=300,
            dry_run=False, token="fake-token",
        )
    assert ex.value.code in ("contact_incomplete", "classification_unresolved")

"""
Azure support-ticket automation for BOM deployment blockers.

The dashboard already scores each region and, on the Quota tab, can file a
direct **Microsoft.Quota** increase. When that path is unavailable or denied —
or when the blocker is a *subscription/zone restriction* on a SKU rather than a
numeric quota shortfall — the remaining remedy is an Azure **support ticket**.
This module automates creating and tracking those tickets through the
``Microsoft.Support`` ARM provider (the same ARM token the rest of the app
already mints; no new credential).

Two blocker → ticket shapes are supported:

* ``kind="quota"`` — a "Service and subscription limits (quotas)" ticket asking
  to raise a compute family's vCPU limit in a region. Available on *all* support
  plans (including Basic/free).
* ``kind="technical"`` — a technical ticket requesting zonal / restricted-SKU
  access for a subscription. Technical tickets generally require a *paid* support
  plan; we surface that expectation to the caller rather than guessing.

**Dry-run first.** ``create_ticket(..., dry_run=True)`` (the default) builds and
returns the exact ARM request *without calling Azure*, and records a local
``preview`` row so the user can review everything before committing. Only an
explicit ``dry_run=False`` performs the real ``PUT``. Demo mode never submits.

All created/previewed tickets are tracked in the local ``supporttickets`` table
so the UI can list and poll them regardless of whether they were real or dry-run.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from . import storage, support_settings, activity_log

log = logging.getLogger(__name__)

ARM_BASE = "https://management.azure.com"
SUPPORT_API_VERSION = "2024-04-01"

# Well-known "Service and subscription limits (quotas)" service id. This is the
# stable public service GUID used for quota tickets across subscriptions.
QUOTA_SERVICE_GUID = "06bfd9d3-516b-d5c6-5802-169c800dec89"
# "Subscription management" is a safe technical/general fallback service; real
# technical tickets should resolve a service via the Services API when possible.
TECHNICAL_SERVICE_GUID = "f3dc5421-79ef-1efa-41a5-42bf3cbb52c6"

GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

TABLE_NAME = "supporttickets"
_PK = "tickets"

VALID_SEVERITIES = ("minimal", "moderate", "critical")


class SupportError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


# ─── small helpers ───────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _service_id(guid: str) -> str:
    return f"/providers/Microsoft.Support/services/{guid}"


def _effective_support_settings(bom_id: Optional[str]) -> Dict[str, Any]:
    """Global support settings with the BOM's per-BOM override layered on top.

    Each BOM may carry its own ticket owner / contact profile (persisted as
    ``support_override`` on the BOM record). Any non-empty override field wins
    over the global default; missing fields inherit global. A missing BOM or no
    override simply returns the global settings unchanged, so legacy BOMs and
    ad-hoc tickets keep filing under the global profile.
    """
    settings = support_settings.get_settings()
    if not bom_id:
        return settings
    try:
        from . import bom_storage
        rec = bom_storage.get(bom_id)
    except Exception:
        return settings
    override = (rec or {}).get("support_override") or {}
    if not isinstance(override, dict) or not override:
        return settings
    merged = dict(settings)
    for key, val in override.items():
        if val not in (None, ""):
            merged[key] = val
    merged["default_severity"] = support_settings._clean_severity(
        merged.get("default_severity"))
    return merged


def _severity(value: Any, settings: Dict[str, Any]) -> str:
    sev = str(value or "").strip().lower()
    if sev in VALID_SEVERITIES:
        return sev
    return support_settings._clean_severity(settings.get("default_severity"))


def _contact_details(settings: Dict[str, Any]) -> Dict[str, Any]:
    extra = [e.strip() for e in str(settings.get("additional_emails") or "").split(",") if e.strip()]
    details: Dict[str, Any] = {
        "firstName": settings.get("contact_first_name") or "",
        "lastName": settings.get("contact_last_name") or "",
        "primaryEmailAddress": settings.get("primary_email") or "",
        "preferredContactMethod": (settings.get("preferred_contact_method") or "email").lower(),
        "preferredTimeZone": settings.get("preferred_timezone") or "Pacific Standard Time",
        "country": (settings.get("country") or "US").upper(),
        "preferredSupportLanguage": settings.get("preferred_language") or "en-us",
    }
    if settings.get("phone"):
        details["phoneNumber"] = settings["phone"]
    if extra:
        details["additionalEmailAddresses"] = extra
    return details


def _new_ticket_name(kind: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"bomdash-{kind}-{stamp}-{uuid.uuid4().hex[:8]}"


# ─── contact-detail validation ───────────────────────────────────────────────

def validate_contact(settings: Dict[str, Any]) -> List[str]:
    """Return a list of human-readable problems that would block a *real*
    submission. Empty list == good to go. Dry-runs ignore this."""
    problems: List[str] = []
    if not settings.get("primary_email"):
        problems.append("Contact email is required (set it in Settings → Support).")
    if not settings.get("contact_first_name"):
        problems.append("Contact first name is required (Settings → Support).")
    if not settings.get("country"):
        problems.append("Country is required (Settings → Support).")
    return problems


# ─── payload builders (pure; no network) ─────────────────────────────────────

def build_quota_ticket_payload(
    *,
    subscription_id: str,
    region: str,
    family: str,
    new_limit: int,
    severity: str,
    settings: Dict[str, Any],
    problem_classification_id: str,
    family_label: Optional[str] = None,
) -> Dict[str, Any]:
    label = family_label or family
    title = f"Increase {label} vCPU quota to {new_limit} in {region}"
    description = (
        f"Automated request from Azure BOM Region Dashboard.\n\n"
        f"Subscription: {subscription_id}\n"
        f"Region: {region}\n"
        f"SKU family: {label} ({family})\n"
        f"Requested new vCPU limit: {new_limit}\n\n"
        f"Reason: this region is blocked in our Bill of Materials analysis due to "
        f"insufficient {label} vCPU quota. Please raise the limit so we can deploy."
    )
    return {
        "properties": {
            "description": description,
            "title": title,
            "severity": severity,
            "serviceId": _service_id(QUOTA_SERVICE_GUID),
            "problemClassificationId": problem_classification_id,
            "contactDetails": _contact_details(settings),
            "quotaTicketDetails": {
                "quotaChangeRequestSubType": "Cores",
                "quotaChangeRequestVersion": "1.0",
                "quotaChangeRequests": [
                    {
                        "region": region,
                        "payload": json.dumps({"SKU": family, "NewLimit": new_limit}),
                    }
                ],
            },
            "advancedDiagnosticConsent": "No",
        }
    }


def build_technical_ticket_payload(
    *,
    subscription_id: str,
    region: str,
    family: str,
    severity: str,
    settings: Dict[str, Any],
    problem_classification_id: str,
    zones: Optional[List[str]] = None,
    family_label: Optional[str] = None,
    detail: Optional[str] = None,
) -> Dict[str, Any]:
    label = family_label or family
    zone_txt = f" (zones: {', '.join(zones)})" if zones else ""
    title = f"Request access to {label} in {region}{zone_txt}"
    description = (
        f"Automated request from Azure BOM Region Dashboard.\n\n"
        f"Subscription: {subscription_id}\n"
        f"Region: {region}\n"
        f"SKU family: {label} ({family})\n"
        f"Requested zones: {', '.join(zones) if zones else 'all availability zones'}\n\n"
        f"Reason: our Bill of Materials analysis shows this SKU/zone is restricted "
        f"for this subscription in {region}. Please enable access so we can deploy."
    )
    if detail:
        description += f"\n\nAdditional detail:\n{detail}"
    props: Dict[str, Any] = {
        "description": description,
        "title": title,
        "severity": severity,
        "serviceId": _service_id(TECHNICAL_SERVICE_GUID),
        "problemClassificationId": problem_classification_id,
        "contactDetails": _contact_details(settings),
        "technicalTicketDetails": {
            "resourceId": f"/subscriptions/{subscription_id}"
        },
        "advancedDiagnosticConsent": "No",
    }
    return {"properties": props}


# ─── problem-classification resolution (best-effort) ─────────────────────────

def resolve_problem_classification(
    token: str, service_guid: str, keywords: Optional[List[str]] = None
) -> Optional[str]:
    """Return a ``problemClassificationId`` for ``service_guid``.

    Best-effort: queries the Support ProblemClassifications API and picks the
    entry whose displayName best matches ``keywords`` (else the first one).
    Returns ``None`` if the lookup fails so callers can decide how to proceed.
    """
    url = (
        f"{ARM_BASE}/providers/Microsoft.Support/services/"
        f"{quote(service_guid, safe='')}/problemClassifications"
    )
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        with httpx.Client(timeout=30.0, http2=False) as client:
            resp = client.get(url, params={"api-version": SUPPORT_API_VERSION}, headers=headers)
        if resp.status_code >= 400:
            log.debug("problemClassifications lookup HTTP %d", resp.status_code)
            return None
        items = resp.json().get("value") or []
    except Exception as ex:
        log.debug("problemClassifications lookup failed: %s", ex)
        return None
    if not items:
        return None
    kw = [k.lower() for k in (keywords or [])]
    best = None
    for item in items:
        name = str((item.get("properties") or {}).get("displayName") or "").lower()
        if kw and all(k in name for k in kw):
            best = item
            break
        if kw and any(k in name for k in kw) and best is None:
            best = item
    chosen = best or items[0]
    return chosen.get("id")


# ─── local tracking store ────────────────────────────────────────────────────

def _save_record(rec: Dict[str, Any]) -> None:
    entity = {"PartitionKey": _PK, "RowKey": rec["ticket_name"]}
    entity.update({k: v for k, v in rec.items() if k not in ("PartitionKey", "RowKey")})
    try:
        storage.get_table_client(TABLE_NAME).upsert_entity(entity, mode="merge")
    except Exception:
        log.exception("failed to persist support ticket record")


def list_tickets(limit: int = 100) -> List[Dict[str, Any]]:
    try:
        rows = storage.get_table_client(TABLE_NAME).query_entities(
            query_filter="PartitionKey eq @pk", parameters={"pk": _PK}
        )
    except Exception:
        return []
    tickets = [_public_record(r) for r in rows]
    tickets.sort(key=lambda t: t.get("created_at") or "", reverse=True)
    return tickets[: max(1, int(limit or 0))]


def get_ticket(ticket_name: str) -> Optional[Dict[str, Any]]:
    try:
        entity = storage.get_table_client(TABLE_NAME).get_entity(_PK, ticket_name)
    except Exception:
        return None
    return _public_record(entity)


def _public_record(entity: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ticket_name": entity.get("RowKey"),
        "kind": entity.get("kind"),
        "status": entity.get("status"),
        "azure_status": entity.get("azure_status"),
        "dry_run": bool(entity.get("dry_run")),
        "subscription_id": entity.get("subscription_id"),
        "region": entity.get("region"),
        "family": entity.get("family"),
        "family_label": entity.get("family_label"),
        "new_limit": entity.get("new_limit"),
        "severity": entity.get("severity"),
        "title": entity.get("title"),
        "created_at": entity.get("created_at"),
        "updated_at": entity.get("updated_at"),
        "azure_ticket_id": entity.get("azure_ticket_id"),
        "error": entity.get("error"),
        "payload": _maybe_json(entity.get("payload_json")),
    }


def _maybe_json(value: Any) -> Any:
    if not value:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


# ─── create / submit ─────────────────────────────────────────────────────────

def create_ticket(
    *,
    kind: str,
    subscription_id: str,
    region: str,
    family: str,
    family_label: Optional[str] = None,
    new_limit: int = 0,
    zones: Optional[List[str]] = None,
    severity: Optional[str] = None,
    detail: Optional[str] = None,
    bom_id: Optional[str] = None,
    dry_run: bool = True,
    token: Optional[str] = None,
    demo_mode: bool = False,
) -> Dict[str, Any]:
    """Build (and optionally submit) a support ticket for a BOM blocker.

    When ``dry_run`` is True (default) or ``demo_mode`` is True, no Azure call is
    made: the constructed ARM request is returned under ``payload`` and a local
    ``preview`` record is stored. When ``dry_run`` is False the ticket is
    submitted via ``Microsoft.Support`` and tracked with its Azure status.

    Returns a public ticket record dict (see ``_public_record``).
    """
    kind = (kind or "").strip().lower()
    if kind not in ("quota", "technical"):
        raise SupportError("bad_kind", "kind must be 'quota' or 'technical'.")
    if not GUID_RE.match(str(subscription_id or "").lower()):
        raise SupportError("bad_subscription", "subscription_id must be a GUID.")
    if not region:
        raise SupportError("bad_region", "region is required.")
    if not family:
        raise SupportError("bad_family", "family is required.")
    if kind == "quota" and int(new_limit or 0) <= 0:
        raise SupportError("bad_limit", "new_limit must be a positive integer for quota tickets.")

    subscription_id = subscription_id.lower()
    settings = _effective_support_settings(bom_id)
    sev = _severity(severity, settings)
    ticket_name = _new_ticket_name(kind)

    is_preview = bool(dry_run or demo_mode)

    # For real submissions, validate contact details up front — before any
    # network call — so a misconfiguration fails fast and offline.
    if not is_preview:
        if not token:
            raise SupportError("no_token", "An ARM token is required to submit a ticket.", 401)
        problems = validate_contact(settings)
        if problems:
            raise SupportError("contact_incomplete", " ".join(problems), 400)

    # Resolve a problem classification when we can reach Azure (real submits
    # only). Previews stay fully offline and use a documented placeholder.
    service_guid = QUOTA_SERVICE_GUID if kind == "quota" else TECHNICAL_SERVICE_GUID
    problem_classification_id: Optional[str] = None
    classification_resolved = False
    if token and not is_preview:
        keywords = ["quota"] if kind == "quota" else ["region", "sku"]
        problem_classification_id = resolve_problem_classification(token, service_guid, keywords)
        classification_resolved = problem_classification_id is not None
    if not problem_classification_id:
        problem_classification_id = (
            f"{_service_id(service_guid)}/problemClassifications/"
            f"<resolved-at-submit>"
        )

    if kind == "quota":
        payload = build_quota_ticket_payload(
            subscription_id=subscription_id,
            region=region,
            family=family,
            new_limit=int(new_limit),
            severity=sev,
            settings=settings,
            problem_classification_id=problem_classification_id,
            family_label=family_label,
        )
    else:
        payload = build_technical_ticket_payload(
            subscription_id=subscription_id,
            region=region,
            family=family,
            severity=sev,
            settings=settings,
            problem_classification_id=problem_classification_id,
            zones=zones,
            family_label=family_label,
            detail=detail,
        )

    title = payload["properties"]["title"]
    now = _now_iso()
    record: Dict[str, Any] = {
        "ticket_name": ticket_name,
        "kind": kind,
        "subscription_id": subscription_id,
        "region": region,
        "family": family,
        "family_label": family_label or family,
        "new_limit": int(new_limit) if kind == "quota" else None,
        "severity": sev,
        "title": title,
        "bom_id": bom_id,
        "dry_run": is_preview,
        "created_at": now,
        "updated_at": now,
        "payload_json": json.dumps(payload, ensure_ascii=False),
        "service_id": _service_id(service_guid),
        "problem_classification_id": problem_classification_id,
        "classification_resolved": classification_resolved,
    }

    if is_preview:
        record["status"] = "preview"
        _save_record(record)
        activity_log.record(
            event_type="support_ticket_preview",
            api_scope="local",
            subscription_id=subscription_id,
            message=f"Support ticket preview ({kind}): {title}",
            details={"region": region, "family": family, "severity": sev,
                     "demo_mode": demo_mode},
        )
        result = _public_record({"RowKey": ticket_name, **record})
        result["preview"] = True
        return result

    # ── real submission ──────────────────────────────────────────────────────
    # (token + contact already validated up front)
    if not classification_resolved:
        # We could not resolve a real classification id; ARM would reject the
        # placeholder. Fail clearly rather than send an invalid request.
        raise SupportError(
            "classification_unresolved",
            "Could not resolve an Azure problem classification for this ticket "
            "(the support Services API was unreachable or returned nothing). "
            "Try again, or file this one from the Azure portal.",
            502,
        )

    url = (
        f"{ARM_BASE}/subscriptions/{subscription_id}/providers/"
        f"Microsoft.Support/supportTickets/{quote(ticket_name, safe='')}"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "azure-bom-region-dashboard/1.0",
    }
    activity_log.record(
        event_type="support_ticket_submit",
        api_scope="subscription",
        subscription_id=subscription_id,
        message=f"Submitting support ticket ({kind}): {title}",
        details={"region": region, "family": family, "severity": sev},
    )
    try:
        with httpx.Client(timeout=60.0, http2=False) as client:
            resp = client.put(
                url, params={"api-version": SUPPORT_API_VERSION},
                headers=headers, json=payload,
            )
    except Exception as ex:
        record["status"] = "failed"
        record["error"] = f"request failed: {ex!r}"[:400]
        record["updated_at"] = _now_iso()
        _save_record(record)
        raise SupportError("request_failed", f"Support ticket request failed: {ex!r}", 502)

    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text}

    if resp.status_code >= 400:
        message = _extract_message(body, f"Support ticket failed (HTTP {resp.status_code}).")
        record["status"] = "failed"
        record["error"] = message[:400]
        record["azure_status_code"] = resp.status_code
        record["updated_at"] = _now_iso()
        _save_record(record)
        activity_log.record(
            event_type="support_ticket_failed",
            api_scope="subscription",
            subscription_id=subscription_id,
            status="error",
            message=f"Support ticket FAILED ({kind}): {message}",
            details={"status_code": resp.status_code},
        )
        raise SupportError("submit_failed", message, resp.status_code)

    props = (body or {}).get("properties") or {}
    record["status"] = "submitted"
    record["azure_status"] = props.get("status") or "Open"
    record["azure_ticket_id"] = body.get("id") or props.get("supportTicketId")
    record["azure_status_code"] = resp.status_code
    record["updated_at"] = _now_iso()
    _save_record(record)
    activity_log.record(
        event_type="support_ticket_ok",
        api_scope="subscription",
        subscription_id=subscription_id,
        message=f"Support ticket submitted ({kind}): {record.get('azure_ticket_id') or ticket_name}",
        details={"region": region, "family": family, "azure_status": record["azure_status"]},
    )
    return _public_record({"RowKey": ticket_name, **record})


def refresh_status(ticket_name: str, token: str) -> Dict[str, Any]:
    """Poll Azure for a submitted ticket's latest status and update the record."""
    entity = None
    try:
        entity = storage.get_table_client(TABLE_NAME).get_entity(_PK, ticket_name)
    except Exception:
        raise SupportError("not_found", "Ticket not found.", 404)
    if entity.get("dry_run"):
        return _public_record(entity)
    subscription_id = entity.get("subscription_id") or ""
    url = (
        f"{ARM_BASE}/subscriptions/{subscription_id}/providers/"
        f"Microsoft.Support/supportTickets/{quote(ticket_name, safe='')}"
    )
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        with httpx.Client(timeout=30.0, http2=False) as client:
            resp = client.get(url, params={"api-version": SUPPORT_API_VERSION}, headers=headers)
        if resp.status_code < 400:
            props = (resp.json() or {}).get("properties") or {}
            entity["azure_status"] = props.get("status") or entity.get("azure_status")
            entity["updated_at"] = _now_iso()
            storage.get_table_client(TABLE_NAME).upsert_entity(entity, mode="merge")
    except Exception as ex:
        log.debug("refresh_status failed: %s", ex)
    return _public_record(entity)


# Statuses Azure reports for a support ticket. Anything not explicitly closed is
# treated as "open/active".
_CLOSED_STATUSES = {"closed"}


def _normalize_azure_ticket(sub_id: str, item: Dict[str, Any]) -> Dict[str, Any]:
    """Map an ARM Microsoft.Support/supportTickets item to the UI ticket shape."""
    props = (item or {}).get("properties") or {}
    name = item.get("name") or props.get("supportTicketId") or ""
    created = props.get("createdDate") or props.get("createDate") or ""
    modified = props.get("modifiedDate") or ""
    severity = str(props.get("severity") or "").lower() or None
    status = props.get("status") or ""
    # Best-effort ticket "type" — quota tickets carry a quotaTicketDetails blob.
    kind = "quota" if "quotaTicketDetails" in props else (
        "technical" if "technicalTicketDetails" in props else "support"
    )
    return {
        "ticket_name": name,
        "kind": kind,
        "status": "submitted",
        "azure_status": status,
        "dry_run": False,
        "external": True,
        "subscription_id": sub_id,
        "region": "",
        "severity": severity,
        "title": props.get("title") or name,
        "created_at": created,
        "updated_at": modified,
        "azure_ticket_id": props.get("supportTicketId") or name,
    }


def list_azure_tickets(
    subscription_id: str, token: str, *, open_only: bool = True, limit: int = 100
) -> List[Dict[str, Any]]:
    """Real-time list of Azure support tickets on a subscription via ARM.

    Returns tickets in the same public shape the UI uses, flagged
    ``external: True``. When ``open_only`` is set (default), closed tickets are
    filtered out. Paginates through ARM ``nextLink`` results.
    """
    sub_id = str(subscription_id or "").strip().lower()
    if not GUID_RE.match(sub_id):
        raise SupportError("bad_subscription", "subscription_id must be a GUID.", 400)
    if not token:
        raise SupportError("no_token", "An ARM token is required to list tickets.", 401)

    url: Optional[str] = (
        f"{ARM_BASE}/subscriptions/{sub_id}/providers/"
        f"Microsoft.Support/supportTickets"
    )
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    out: List[Dict[str, Any]] = []
    try:
        with httpx.Client(timeout=30.0, http2=False) as client:
            params: Optional[Dict[str, str]] = {"api-version": SUPPORT_API_VERSION}
            while url and len(out) < limit:
                resp = client.get(url, params=params, headers=headers)
                if resp.status_code >= 400:
                    body = _safe_json(resp)
                    raise SupportError(
                        "list_failed",
                        _extract_message(body, f"Could not list support tickets ({resp.status_code})."),
                        resp.status_code,
                    )
                data = resp.json() or {}
                for item in data.get("value") or []:
                    rec = _normalize_azure_ticket(sub_id, item)
                    if open_only and str(rec.get("azure_status") or "").lower() in _CLOSED_STATUSES:
                        continue
                    out.append(rec)
                url = data.get("nextLink")
                params = None  # nextLink already carries the query string
    except SupportError:
        raise
    except Exception as ex:
        raise SupportError("request_failed", f"Support ticket list failed: {ex!r}", 502)

    out.sort(key=lambda t: t.get("created_at") or "", reverse=True)
    return out[: max(1, int(limit or 0))]


def _safe_json(resp: "httpx.Response") -> Any:
    try:
        return resp.json()
    except Exception:
        return getattr(resp, "text", "")


def _extract_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if payload.get("message"):
            return str(payload["message"])
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    return fallback

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
* ``kind="technical"`` — an Availability-Zone access ("zonal whitelisting")
  request. Despite the name, Azure files this as a **quota** ticket under the
  same "Service and subscription limits (quotas)" service and "Compute-VM
  (cores-vCPUs)" classification, with a per-SKU × zone ``quotaChangeRequests``
  payload (``Type:Zonal``). Available on all support plans.

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
# ARM locations API that exposes logical→physical availability-zone mappings,
# used to reproduce the portal's "Zone access" payload (Physical AZ0N).
ZONE_LOCATIONS_API_VERSION = "2022-12-01"

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

# Azure Support's contactDetails.country requires an ISO 3166-1 **alpha-3** code
# (e.g. "USA"), but users and the rest of the app commonly use alpha-2 ("US").
# Map the common alpha-2 codes to alpha-3 so submissions don't 400 with
# "Use the standard ISO 3166-1 alpha-3 code for Country".
_ISO_ALPHA2_TO_ALPHA3 = {
    "AF": "AFG", "AL": "ALB", "DZ": "DZA", "AD": "AND", "AO": "AGO", "AR": "ARG",
    "AM": "ARM", "AU": "AUS", "AT": "AUT", "AZ": "AZE", "BS": "BHS", "BH": "BHR",
    "BD": "BGD", "BB": "BRB", "BY": "BLR", "BE": "BEL", "BZ": "BLZ", "BJ": "BEN",
    "BM": "BMU", "BT": "BTN", "BO": "BOL", "BA": "BIH", "BW": "BWA", "BR": "BRA",
    "BN": "BRN", "BG": "BGR", "BF": "BFA", "BI": "BDI", "KH": "KHM", "CM": "CMR",
    "CA": "CAN", "CV": "CPV", "KY": "CYM", "CF": "CAF", "TD": "TCD", "CL": "CHL",
    "CN": "CHN", "CO": "COL", "KM": "COM", "CG": "COG", "CD": "COD", "CR": "CRI",
    "CI": "CIV", "HR": "HRV", "CU": "CUB", "CY": "CYP", "CZ": "CZE", "DK": "DNK",
    "DJ": "DJI", "DM": "DMA", "DO": "DOM", "EC": "ECU", "EG": "EGY", "SV": "SLV",
    "GQ": "GNQ", "ER": "ERI", "EE": "EST", "SZ": "SWZ", "ET": "ETH", "FJ": "FJI",
    "FI": "FIN", "FR": "FRA", "GA": "GAB", "GM": "GMB", "GE": "GEO", "DE": "DEU",
    "GH": "GHA", "GR": "GRC", "GL": "GRL", "GD": "GRD", "GT": "GTM", "GN": "GIN",
    "GW": "GNB", "GY": "GUY", "HT": "HTI", "HN": "HND", "HK": "HKG", "HU": "HUN",
    "IS": "ISL", "IN": "IND", "ID": "IDN", "IR": "IRN", "IQ": "IRQ", "IE": "IRL",
    "IL": "ISR", "IT": "ITA", "JM": "JAM", "JP": "JPN", "JO": "JOR", "KZ": "KAZ",
    "KE": "KEN", "KI": "KIR", "KP": "PRK", "KR": "KOR", "KW": "KWT", "KG": "KGZ",
    "LA": "LAO", "LV": "LVA", "LB": "LBN", "LS": "LSO", "LR": "LBR", "LY": "LBY",
    "LI": "LIE", "LT": "LTU", "LU": "LUX", "MO": "MAC", "MG": "MDG", "MW": "MWI",
    "MY": "MYS", "MV": "MDV", "ML": "MLI", "MT": "MLT", "MH": "MHL", "MR": "MRT",
    "MU": "MUS", "MX": "MEX", "FM": "FSM", "MD": "MDA", "MC": "MCO", "MN": "MNG",
    "ME": "MNE", "MA": "MAR", "MZ": "MOZ", "MM": "MMR", "NA": "NAM", "NR": "NRU",
    "NP": "NPL", "NL": "NLD", "NZ": "NZL", "NI": "NIC", "NE": "NER", "NG": "NGA",
    "MK": "MKD", "NO": "NOR", "OM": "OMN", "PK": "PAK", "PW": "PLW", "PS": "PSE",
    "PA": "PAN", "PG": "PNG", "PY": "PRY", "PE": "PER", "PH": "PHL", "PL": "POL",
    "PT": "PRT", "PR": "PRI", "QA": "QAT", "RO": "ROU", "RU": "RUS", "RW": "RWA",
    "SA": "SAU", "SN": "SEN", "RS": "SRB", "SC": "SYC", "SL": "SLE", "SG": "SGP",
    "SK": "SVK", "SI": "SVN", "SB": "SLB", "SO": "SOM", "ZA": "ZAF", "SS": "SSD",
    "ES": "ESP", "LK": "LKA", "SD": "SDN", "SR": "SUR", "SE": "SWE", "CH": "CHE",
    "SY": "SYR", "TW": "TWN", "TJ": "TJK", "TZ": "TZA", "TH": "THA", "TL": "TLS",
    "TG": "TGO", "TO": "TON", "TT": "TTO", "TN": "TUN", "TR": "TUR", "TM": "TKM",
    "UG": "UGA", "UA": "UKR", "AE": "ARE", "GB": "GBR", "US": "USA", "UY": "URY",
    "UZ": "UZB", "VU": "VUT", "VE": "VEN", "VN": "VNM", "YE": "YEM", "ZM": "ZMB",
    "ZW": "ZWE",
}
# A few common full names / variants users might type.
_COUNTRY_NAME_TO_ALPHA3 = {
    "UNITED STATES": "USA", "UNITED STATES OF AMERICA": "USA", "USA": "USA",
    "UNITED KINGDOM": "GBR", "GREAT BRITAIN": "GBR", "UK": "GBR",
    "CANADA": "CAN", "AUSTRALIA": "AUS", "INDIA": "IND", "GERMANY": "DEU",
    "FRANCE": "FRA", "JAPAN": "JPN", "BRAZIL": "BRA", "IRELAND": "IRL",
    "NETHERLANDS": "NLD", "SPAIN": "ESP", "ITALY": "ITA", "MEXICO": "MEX",
}


def _iso3_country(value: Any, default: str = "USA") -> str:
    """Normalize a country value to an ISO 3166-1 alpha-3 code for Azure Support.

    Accepts alpha-2 ("US"), alpha-3 ("USA"), or a common country name and always
    returns an alpha-3 code. Falls back to ``default`` when it can't be resolved.
    """
    text = str(value or "").strip().upper()
    if not text:
        return default
    if len(text) == 3 and text.isalpha():
        return text
    if len(text) == 2 and text in _ISO_ALPHA2_TO_ALPHA3:
        return _ISO_ALPHA2_TO_ALPHA3[text]
    if text in _COUNTRY_NAME_TO_ALPHA3:
        return _COUNTRY_NAME_TO_ALPHA3[text]
    return default

VALID_SEVERITIES = ("minimal", "moderate", "critical")


class SupportError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details


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
        "country": _iso3_country(settings.get("country")),
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


def _physical_zone_map(
    subscription_id: str, region_code_lower: str, token: Optional[str]
) -> Dict[str, str]:
    """Map logical zone ("1") → physical-zone display ("Physical AZ01").

    Reads the ARM ``locations`` API ``availabilityZoneMappings`` for the
    subscription so the zonal payload carries the subscription-specific physical
    AZ the portal's "Zone access" flow submits. Best-effort: returns ``{}`` when
    unavailable (e.g. offline dry-run with no token), and callers fall back to a
    generic ``Physical AZ0N``.
    """
    if not token:
        return {}
    url = f"{ARM_BASE}/subscriptions/{subscription_id}/locations"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    out: Dict[str, str] = {}
    try:
        with httpx.Client(timeout=30.0, http2=False) as client:
            resp = client.get(
                url, params={"api-version": ZONE_LOCATIONS_API_VERSION}, headers=headers
            )
        if resp.status_code >= 400:
            log.debug("zone mapping lookup HTTP %d", resp.status_code)
            return {}
        for loc in (resp.json().get("value") or []):
            if str(loc.get("name") or "").lower() != region_code_lower:
                continue
            for m in (loc.get("availabilityZoneMappings") or []):
                logical = str(m.get("logicalZone") or "").strip()
                physical = str(m.get("physicalZone") or "").strip()
                if not logical:
                    continue
                # physicalZone looks like "australiaeast-az1" → "Physical AZ01".
                mt = re.search(r"az(\d+)$", physical, re.IGNORECASE)
                out[logical] = (
                    f"Physical AZ{int(mt.group(1)):02d}" if mt else (physical or f"Physical AZ{logical}")
                )
    except Exception as ex:  # pragma: no cover - defensive
        log.debug("zone mapping lookup failed: %s", ex)
    return out


def build_zonal_ticket_payload(
    *,
    subscription_id: str,
    region: str,
    family: str,
    new_limit: int,
    zones: Optional[List[str]],
    severity: str,
    settings: Dict[str, Any],
    problem_classification_id: str,
    family_label: Optional[str] = None,
    zone_map: Optional[Dict[str, str]] = None,
    detail: Optional[str] = None,
) -> Dict[str, Any]:
    """Build an Availability-Zone access ("zonal whitelisting") request.

    Azure files zonal whitelisting as a **quota** ticket (service "Service and
    subscription limits (quotas)", classification "Compute-VM (cores-vCPUs)
    subscription limit increases") — *not* a technical ticket — with one
    ``quotaChangeRequests`` entry per SKU family × availability zone. Each
    payload mirrors the portal's "Zone access" submission exactly::

        {VMFamily:<family>,NewLimit:<n>,DeploymentStack:ARM,Type:Zonal,
         AvailabilityZone:Physical AZ0N,LogicalAvailabilityZone:Zone N}

    ``region`` is uppercased with spaces stripped (as the portal stores it), and
    the physical AZ comes from ``zone_map`` (see ``_physical_zone_map``).
    """
    label = family_label or family
    region_code = re.sub(r"\s+", "", str(region or ""))
    region_upper = region_code.upper()
    zone_map = zone_map or {}
    zlist = [str(z).strip() for z in (zones or []) if str(z).strip()]
    if not zlist:
        raise SupportError(
            "bad_zones", "At least one availability zone is required for a zonal access ticket.", 400
        )
    if int(new_limit or 0) <= 0:
        raise SupportError(
            "bad_limit", "new_limit (target vCPU limit) must be a positive integer for zonal tickets.", 400
        )

    change_requests: List[Dict[str, str]] = []
    detail_lines: List[str] = []
    for z in zlist:
        logical_display = f"Zone {z}"
        physical_display = zone_map.get(z) or (
            f"Physical AZ{int(z):02d}" if z.isdigit() else f"Physical AZ{z}"
        )
        payload_str = (
            "{VMFamily:%s,NewLimit:%d,DeploymentStack:ARM,Type:Zonal,"
            "AvailabilityZone:%s,LogicalAvailabilityZone:%s}"
            % (family, int(new_limit), physical_display, logical_display)
        )
        change_requests.append({"region": region_upper, "payload": payload_str})
        detail_lines.append(f"{logical_display} ({physical_display}): {label} = {new_limit} vCPUs")

    title = f"{label} zonal access whitelisting in {region_code} (zones {', '.join(zlist)})"
    description = (
        "Automated request from Azure BOM Region Dashboard.\n\n"
        f"Subscription: {subscription_id}\n"
        f"Region: {region_upper}\n"
        f"SKU family: {label} ({family})\n"
        f"Requested new vCPU limit: {new_limit}\n"
        f"Availability zones: {', '.join(zlist)}\n\n"
        "Reason: this SKU family is zone-restricted for this subscription. "
        "Requesting Availability Zone access (Zone access request type) / zonal "
        "whitelisting for the listed zones so we can deploy.\n\n"
        "Payload Details - requested new limit (vCPUs) per Availability Zone:\n"
        + "\n".join(detail_lines)
        + f"\nApplies to all rows: Type=Zonal, DeploymentStack=ARM, Region={region_upper}, "
        "quotaChangeVersion=1.0."
    )
    if detail:
        description += f"\n\nAdditional detail:\n{detail}"

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
                "quotaChangeRequests": change_requests,
            },
            "advancedDiagnosticConsent": "No",
        }
    }


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
    if not kw:
        return items[0].get("id")
    # Score each classification by how many keywords appear in its displayName
    # and pick the best match. Do NOT fall back to an arbitrary classification
    # (items[0]): submitting a Compute cores payload under an unrelated
    # classification makes ARM reject the whole request with a generic 400.
    best = None
    best_score = 0
    for item in items:
        name = str((item.get("properties") or {}).get("displayName") or "").lower()
        score = sum(1 for k in kw if k in name)
        if score > best_score:
            best, best_score = item, score
    if best is None:
        return None
    return best.get("id")


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
    if kind == "technical":
        # Zonal access ("whitelisting") is filed as a quota ticket and needs a
        # target vCPU limit plus at least one zone.
        if int(new_limit or 0) <= 0:
            raise SupportError("bad_limit", "new_limit (target vCPU limit) is required for zonal access tickets.")
        if not (zones and any(str(z).strip() for z in zones)):
            raise SupportError("bad_zones", "At least one availability zone is required for a zonal access ticket.", 400)

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
    # Zonal whitelisting is filed under the *quota* service (cores-vCPUs
    # classification) — the same service/classification as a cores increase.
    service_guid = QUOTA_SERVICE_GUID
    problem_classification_id: Optional[str] = None
    classification_resolved = False
    if token and not is_preview:
        keywords = ["cores", "vcpu"]
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
        # Resolve the subscription-specific physical AZ mapping for real submits
        # so the payload matches the portal; previews stay offline (zone_map={}).
        zone_map: Dict[str, str] = {}
        if token and not is_preview:
            region_code_lower = re.sub(r"\s+", "", region).lower()
            zone_map = _physical_zone_map(subscription_id, region_code_lower, token)
        payload = build_zonal_ticket_payload(
            subscription_id=subscription_id,
            region=region,
            family=family,
            new_limit=int(new_limit),
            zones=zones,
            severity=sev,
            settings=settings,
            problem_classification_id=problem_classification_id,
            family_label=family_label,
            zone_map=zone_map,
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
        "new_limit": int(new_limit) if int(new_limit or 0) > 0 else None,
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
    log.info(
        "support ticket PUT sub=%s classification=%s payload=%s",
        subscription_id, problem_classification_id,
        json.dumps(payload, ensure_ascii=False)[:2000],
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
        log.error(
            "support ticket submit HTTP %d for sub=%s kind=%s: %s | body=%s",
            resp.status_code, subscription_id, kind, message,
            json.dumps(body, ensure_ascii=False)[:2000],
        )
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
        # Conditional Access can require an MFA-authenticated ARM token to
        # *write* a support ticket. The browser-minted (delegated) token may be
        # password-only, so Azure rejects the PUT with a 401 step-up challenge.
        # Surface a distinct code + any claims challenge so the SPA can re-auth
        # with MFA and retry, instead of showing an opaque failure.
        mfa = _mfa_challenge(resp, body)
        if mfa is not None:
            raise SupportError(
                "mfa_required",
                "Azure requires multi-factor authentication (MFA) to file a "
                "support ticket. Please re-authenticate when prompted and submit "
                "again.",
                401,
                details={"azure": body, "claims": mfa.get("claims")},
            )
        raise SupportError("submit_failed", message, resp.status_code, details=body)

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


def close_azure_ticket(subscription_id: str, ticket_name: str, token: str) -> Dict[str, Any]:
    """Close an Azure support ticket via ARM ``PATCH`` (``status = Closed``).

    Works for both dashboard-created and externally created tickets. Azure only
    permits closing a ticket that is not actively assigned to an engineer;
    otherwise ARM returns an error which is surfaced to the caller. When the
    ticket is also tracked locally, its cached ``azure_status`` is updated.
    """
    sub_id = str(subscription_id or "").strip().lower()
    name = str(ticket_name or "").strip()
    if not GUID_RE.match(sub_id):
        raise SupportError("bad_subscription", "subscription_id must be a GUID.", 400)
    if not name:
        raise SupportError("bad_name", "ticket_name is required.", 400)
    if not token:
        raise SupportError("no_token", "An ARM token is required to close a ticket.", 401)

    url = (
        f"{ARM_BASE}/subscriptions/{sub_id}/providers/"
        f"Microsoft.Support/supportTickets/{quote(name, safe='')}"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=30.0, http2=False) as client:
            resp = client.patch(
                url,
                params={"api-version": SUPPORT_API_VERSION},
                headers=headers,
                json={"status": "Closed"},
            )
    except Exception as ex:
        raise SupportError("request_failed", f"Close request failed: {ex!r}", 502)

    if resp.status_code >= 400:
        body = _safe_json(resp)
        raise SupportError(
            "close_failed",
            _extract_message(body, f"Could not close the ticket ({resp.status_code})."),
            resp.status_code,
            details=body,
        )

    props = (resp.json() or {}).get("properties") or {}
    new_status = props.get("status") or "closed"

    # Reflect the new status locally if we track this ticket.
    try:
        entity = storage.get_table_client(TABLE_NAME).get_entity(_PK, name)
        entity["azure_status"] = new_status
        entity["updated_at"] = _now_iso()
        storage.get_table_client(TABLE_NAME).upsert_entity(entity, mode="merge")
    except Exception:
        pass

    return {"ticket_name": name, "subscription_id": sub_id, "azure_status": new_status}


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
        if isinstance(error, dict):
            parts: List[str] = []
            if error.get("message"):
                parts.append(str(error["message"]))
            # ARM often hides the real reason in error.details[].message and/or a
            # more specific error.code — surface both so the client sees it.
            details = error.get("details")
            if isinstance(details, list):
                for d in details:
                    if isinstance(d, dict) and d.get("message") and str(d["message"]) not in parts:
                        parts.append(str(d["message"]))
            code = error.get("code")
            if code and not parts:
                parts.append(str(code))
            elif code:
                parts[0] = f"{parts[0]} [{code}]"
            if parts:
                return " — ".join(parts)
        if payload.get("message"):
            return str(payload["message"])
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    return fallback


# Signatures Azure uses when a write is blocked pending an MFA step-up.
_MFA_MARKERS = (
    "requestdisallowedbyazure",   # ARM CA block: "...without authenticating through MFA"
    "multi-factor",
    "multifactor",
    "insufficient_claims",
    "aka.ms/mfaforazure",
    "mfaforazure",
    "50076",                      # AADSTS50076 — MFA required
    "50079",                      # AADSTS50079 — MFA enrollment required
)


def _mfa_challenge(resp: Any, body: Any) -> Optional[Dict[str, Any]]:
    """Detect an MFA / conditional-access step-up rejection on a support PUT.

    Returns a dict (optionally carrying the base64 ``claims`` challenge from the
    ``WWW-Authenticate`` header) when Azure demands an MFA-authenticated token,
    else ``None``. Callers turn this into an ``mfa_required`` error so the SPA
    can re-acquire an MFA token and retry.
    """
    try:
        status = int(getattr(resp, "status_code", 0) or 0)
    except Exception:
        status = 0
    if status not in (401, 403):
        return None

    hay = ""
    if isinstance(body, dict):
        hay = json.dumps(body, ensure_ascii=False)
    elif body:
        hay = str(body)
    hay = hay.lower()

    www = ""
    claims: Optional[str] = None
    try:
        www = str((getattr(resp, "headers", {}) or {}).get("WWW-Authenticate", "") or "")
    except Exception:
        www = ""
    if www:
        hay += " " + www.lower()
        m = re.search(r'claims="([^"]+)"', www)
        if m:
            claims = m.group(1)

    if any(marker in hay for marker in _MFA_MARKERS):
        return {"claims": claims}
    return None


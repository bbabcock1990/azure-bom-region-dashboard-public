"""Live "Refresh from Azure" providers for managed datasets.

Some reference datasets can be regenerated directly from ARM instead of
being uploaded by hand as Azure ships new regions and SKUs:

  * ``region_catalog``     — the master region list with availability-zone
    support, from the ARM *Locations* API
    (``/subscriptions/{sub}/locations``).
  * ``sku_families_seed``  — the canonical VM family ids, from
    ``Microsoft.Compute/skus`` (reuses :mod:`sku_families`).

Each provider returns **bytes in the packaged seed's exact format**, so the
existing :mod:`dataset_store` validators accept the result unchanged and it
drops straight into the override layer. A provider never mutates a seed and
never writes anything itself — :mod:`dataset_store` owns persistence.

Datasets with no ARM equivalent (the latency matrix, the curated service
catalog) are intentionally absent from :data:`PROVIDERS`; those are kept
current with the "Link a data URL" mechanism instead.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

log = logging.getLogger(__name__)

ARM_BASE = "https://management.azure.com"
# Locations API version that returns `availabilityZoneMappings`, which is our
# availability-zone signal for a region.
LOCATIONS_API_VERSION = "2022-12-01"
DEFAULT_TIMEOUT_S = 45.0


class ProviderError(Exception):
    """Stable error code the HTTP/store layer maps to a friendly message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _operator_context() -> tuple:
    """Return ``(bearer_token, subscription_id)`` for the signed-in operator.

    Reuses the same token/subscription resolution the live SKU pull uses, so
    it works even when the customer subscription lives in a foreign tenant."""
    from . import auth_token, sku_families
    try:
        info = auth_token.get_arm_default_token()
    except auth_token.AuthError:
        raise ProviderError(
            "not_signed_in",
            "Sign in to Azure first — no ARM token is available to refresh "
            "this dataset.",
        )
    sub = sku_families._resolve_operator_subscription()
    if not sub:
        raise ProviderError(
            "no_subscription",
            "No readable Azure subscription was found for the signed-in "
            "account.",
        )
    return info.token, sub


def _arm_get_all(url: str, params: Optional[dict], token: str) -> List[dict]:
    """GET an ARM collection, following ``nextLink`` paging, and return the
    concatenated ``value`` items."""
    try:
        import httpx
    except Exception:  # pragma: no cover
        raise ProviderError("http_unavailable", "HTTP client is unavailable.")
    headers = {
        "authorization": f"Bearer {token}",
        "accept": "application/json",
        "user-agent": "azure-bom-region-dashboard/1.0",
    }
    items: List[dict] = []
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT_S, http2=False) as client:
            while True:
                resp = client.get(url, params=params, headers=headers)
                if resp.status_code >= 400:
                    raise ProviderError(
                        "arm_error",
                        f"Azure returned HTTP {resp.status_code} "
                        f"({resp.text[:160]}).",
                    )
                body = resp.json()
                items.extend(body.get("value") or [])
                nxt = body.get("nextLink")
                if not nxt:
                    break
                url, params = nxt, None  # nextLink carries the full query
    except ProviderError:
        raise
    except Exception as ex:
        raise ProviderError("arm_error", f"ARM call failed: {ex!r}")
    return items


def region_catalog_bytes() -> bytes:
    """Build the region catalog JSON live from the ARM Locations API."""
    token, sub = _operator_context()
    url = f"{ARM_BASE}/subscriptions/{sub}/locations"
    items = _arm_get_all(url, {"api-version": LOCATIONS_API_VERSION}, token)
    regions: List[Dict] = []
    for it in items:
        name = (it.get("name") or "").strip()
        if not name:
            continue
        meta = it.get("metadata") or {}
        # Skip logical/paired pseudo-locations; keep real physical regions.
        if (meta.get("regionType") or "") and meta.get("regionType") != "Physical":
            continue
        display = (it.get("displayName") or name).strip()
        has_az = bool(it.get("availabilityZoneMappings"))
        regions.append(
            {"name": name, "display_name": display, "has_az": has_az}
        )
    if not regions:
        raise ProviderError(
            "empty_result",
            "The ARM Locations API returned no physical regions.",
        )
    regions.sort(key=lambda r: r["name"])
    doc = {
        "_comment": (
            f"Auto-generated from the ARM Locations API (api-version "
            f"{LOCATIONS_API_VERSION}) on "
            f"{datetime.now(timezone.utc).date().isoformat()} using "
            f"subscription {sub}. 'has_az' reflects whether this subscription "
            "has availability-zone mappings for the region. Re-run 'Refresh "
            "from Azure' after new regions launch, or revert to the built-in "
            "seed at any time."
        ),
        "regions": regions,
    }
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def sku_families_seed_bytes() -> bytes:
    """Return the canonical VM family ids live from ``Microsoft.Compute/skus``."""
    from . import sku_families
    fams = sku_families._families_from_arm()
    if not fams:
        raise ProviderError(
            "empty_result",
            "ARM returned no VM SKU families — check that you're signed in and "
            "have a readable subscription.",
        )
    return ("\n".join(fams) + "\n").encode("utf-8")


# Map of dataset id → provider callable returning seed-format bytes.
PROVIDERS: Dict[str, Callable[[], bytes]] = {
    "region_catalog": region_catalog_bytes,
    "sku_families_seed": sku_families_seed_bytes,
}


def can_refresh(ds_id: str) -> bool:
    return ds_id in PROVIDERS


def refresh_bytes(ds_id: str) -> bytes:
    fn = PROVIDERS.get(ds_id)
    if fn is None:
        raise ProviderError(
            "no_provider",
            f"Dataset '{ds_id}' can't be refreshed from Azure. Use 'Link a "
            "data URL' or upload a file instead.",
        )
    return fn()

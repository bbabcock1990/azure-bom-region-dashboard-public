"""POST /api/pricing/estimate

Estimate the monthly cost of the BOM across regions.

Request body::

    {
      "regions":  ["eastus", "westus2", ...],       # region shorts to price
      "families": [{"family": "<id>", "label": "Dsv5", "required_cores": 100}],
      "services": ["Azure Bastion", "Premium SSD v2", ...],   # optional
      # optional overrides (else saved pricing settings are used):
      "os": "linux" | "windows",
      "currency": "USD",
      "hours_per_month": 730,
      "acd_discount_pct": 0,
      "noncompute_uplift_pct": 35,
      "service_estimates": {"Azure Bastion": 140.0}
    }

Compute cost is authoritative (public Azure Retail Prices API, no auth).
Non-compute cost is an estimate: per-service flat figures the operator itemized
plus an uplift percentage of compute as a catch-all. Everything is flagged
``estimate_only``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .._shared import auth, csrf, pricing, pricing_settings
from .._shared import httpfunc as func

log = logging.getLogger(__name__)


def _ok(payload: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload), status_code=status, mimetype="application/json"
    )


def _err(code: str, message: str, status: int = 400) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps({"error": code, "message": message}),
        status_code=status,
        mimetype="application/json",
    )


def _pick(body: dict, key: str, default: Any) -> Any:
    val = body.get(key)
    return default if val is None else val


async def main(req: func.HttpRequest) -> func.HttpResponse:
    auth.get_local_user(req)

    try:
        csrf.assert_safe_origin(req)
    except csrf.OriginError as ex:
        return _err("origin_rejected", str(ex), 403)

    try:
        body = req.get_json()
    except ValueError:
        return _err("bad_json", "Body must be a JSON object.", 400)
    if not isinstance(body, dict):
        return _err("bad_json", "Body must be a JSON object.", 400)

    regions = body.get("regions")
    if not isinstance(regions, list) or not regions:
        return _err("no_regions", "Body.regions must be a non-empty array.", 400)
    families = body.get("families")
    if not isinstance(families, list):
        families = []
    services = body.get("services")
    if not isinstance(services, list):
        services = []

    # Saved settings supply defaults; the request may override any of them so
    # the UI can live-preview a changed ACD/OS/uplift before saving.
    saved = pricing_settings.get_settings()
    os_name = _pick(body, "os", saved.get("pricing_os", "linux"))
    currency = _pick(body, "currency", saved.get("currency", "USD"))
    hours = _pick(body, "hours_per_month", saved.get("hours_per_month", 730))
    acd = _pick(body, "acd_discount_pct", saved.get("acd_discount_pct", 0.0))
    uplift = _pick(body, "noncompute_uplift_pct", saved.get("noncompute_uplift_pct", 35.0))
    service_estimates = _pick(body, "service_estimates", saved.get("service_estimates", {}))
    suggest_alts = _pick(body, "suggest_alternatives", saved.get("suggest_alternatives", True))
    alt_min_savings = _pick(body, "alt_min_savings_pct", saved.get("alt_min_savings_pct", 5.0))
    allow_older_gen = _pick(body, "allow_older_generation", saved.get("allow_older_generation", False))

    try:
        result = await asyncio.to_thread(
            pricing.estimate,
            regions,
            families,
            os_name=os_name,
            currency=currency,
            hours_per_month=hours,
            acd_discount_pct=acd,
            services=services,
            noncompute_uplift_pct=uplift,
            service_estimates=service_estimates,
            suggest_alternatives=suggest_alts,
            alt_min_savings_pct=alt_min_savings,
            allow_older_generation=allow_older_gen,
        )
    except Exception as ex:  # pragma: no cover - defensive
        log.exception("pricing estimate failed")
        return _err("estimate_failed", f"Cost estimate failed: {ex!r}", 500)

    return _ok(result, status=200)

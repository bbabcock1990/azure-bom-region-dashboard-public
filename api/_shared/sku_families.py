"""Canonical Azure VM SKU family IDs for the BOM editor's family pickers.

Family IDs (e.g. ``standardDav6Family``) are **case-sensitive** — they are the
exact strings Azure's quota / ARM APIs return. Making users type them by hand is
error-prone, so the BOM editor offers a dropdown populated from here.

Source of truth: ARM ``Microsoft.Compute/skus`` — every VM SKU carries a
``family`` property; the distinct set of those (for ``resourceType ==
"virtualMachines"``) is the canonical family list. We query a single
representative region (families are effectively global) using the operator's
own subscription, so it works even when the customer sub lives in a foreign
tenant.

Resilience:
- The bundled seed (``data/sku_families_seed.txt``, with ``data/skus.txt`` as a
  fallback) is always available, so the picker has the well-known families even
  before the user signs in.
- Opening the editor (``refresh=False``) never touches the network or auth — it
  serves the cache or the seed, so it can't trigger a sign-in prompt or add
  latency. Only the explicit "Refresh families" gesture (``refresh=True``) does
  the live ARM pull, and a failed pull leaves any existing richer cache intact.
"""
from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional

from . import auth_token

log = logging.getLogger(__name__)

ARM_BASE = "https://management.azure.com"
ARM_API_VERSION = "2024-07-01"
# Families are effectively global; one well-populated region gives the canonical
# set without paging through every location.
PROBE_REGION = "eastus"
DEFAULT_TIMEOUT_S = 45.0

_DATA_DIR = Path(__file__).resolve().parent / "data"
# Picker seed: a broad snapshot of canonical family IDs so the dropdown is
# useful offline / pre-sign-in. Falls back to the smaller pipeline-default seed
# (skus.txt) if the snapshot is absent. The live ARM pull (Refresh button)
# supersedes both.
_FAMILIES_SEED = _DATA_DIR / "sku_families_seed.txt"
_SKUS_SEED = _DATA_DIR / "skus.txt"

# Defensive shape check so a malformed ARM/seed value can't poison the picker.
_FAMILY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,80}$")

_LOCK = threading.Lock()
# Cached merged result: {"families": [...], "source": "..."}. None until first load.
_CACHE: Optional[Dict] = None


def _load_seed_families() -> List[str]:
    """Bundled canonical families for the picker. Prefer the broad snapshot,
    fall back to the pipeline-default seed (skus.txt)."""
    seed_path = _FAMILIES_SEED if _FAMILIES_SEED.exists() else _SKUS_SEED
    try:
        text = seed_path.read_text(encoding="utf-8")
    except Exception:
        log.warning("sku_families: could not read seed %s", seed_path)
        return []
    out: List[str] = []
    for line in text.splitlines():
        fam = line.strip()
        if fam and _FAMILY_RE.match(fam):
            out.append(fam)
    return out


def _resolve_operator_subscription() -> Optional[str]:
    """A subscription the signed-in operator can read SKUs from. Families are
    global, so any readable sub works — prefer one from the token, else the
    first visible subscription."""
    try:
        info = auth_token.get_arm_default_token()
    except auth_token.AuthError:
        return None
    sub = (info.az_subscription or "").strip()
    if re.match(r"^[0-9a-fA-F-]{36}$", sub):
        return sub
    try:
        subs = auth_token.list_subscriptions()
    except auth_token.AuthError:
        return None
    for s in subs:
        if s.get("id"):
            return s["id"]
    return None


def _families_from_arm(*, region: str = PROBE_REGION) -> Optional[List[str]]:
    """Return distinct canonical VM family IDs from ARM, or None on any failure."""
    try:
        import httpx
    except Exception:  # pragma: no cover
        return None
    try:
        token_info = auth_token.get_arm_default_token()
    except auth_token.AuthError as ex:
        log.info("sku_families: ARM token unavailable (%s) — seed only", ex.code)
        return None
    sub = _resolve_operator_subscription()
    if not sub:
        log.info("sku_families: no readable subscription — seed only")
        return None

    headers = {
        "authorization": f"Bearer {token_info.token}",
        "accept": "application/json",
        "user-agent": "azure-bom-region-dashboard/1.0",
    }
    url = f"{ARM_BASE}/subscriptions/{sub}/providers/Microsoft.Compute/skus"
    params: Optional[dict] = {
        "api-version": ARM_API_VERSION,
        "$filter": f"location eq '{region}'",
    }
    families: set = set()
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT_S, http2=False) as client:
            while True:
                resp = client.get(url, params=params, headers=headers)
                if resp.status_code >= 400:
                    log.info("sku_families: ARM skus returned %s — seed only",
                             resp.status_code)
                    return None
                body = resp.json()
                for sku in body.get("value") or []:
                    if sku.get("resourceType") != "virtualMachines":
                        continue
                    fam = (sku.get("family") or "").strip()
                    if fam and _FAMILY_RE.match(fam):
                        families.add(fam)
                next_link = body.get("nextLink")
                if not next_link:
                    break
                url = next_link
                params = None  # nextLink carries the full query
    except Exception as ex:
        log.info("sku_families: ARM call failed (%r) — seed only", ex)
        return None
    return sorted(families)


def _friendly_label(family_id: str) -> str:
    """standardDav6Family -> 'Dav6 Series'. Best-effort human-readable label."""
    m = re.match(r"^standard(.+)Family$", family_id, re.IGNORECASE)
    return f"{m.group(1)} Series" if m else family_id


def _merge(seed: List[str], arm: Optional[List[str]]) -> Dict:
    """Merge seed + ARM families, de-duped case-insensitively (ARM canonical
    casing wins), sorted case-insensitively for a tidy dropdown."""
    by_key: Dict[str, str] = {}
    for fam in seed:
        by_key.setdefault(fam.lower(), fam)
    if arm:
        for fam in arm:  # ARM wins on casing
            by_key[fam.lower()] = fam
    families = sorted(by_key.values(), key=lambda s: s.lower())
    families_rich = [{"id": f, "label": _friendly_label(f)} for f in families]
    source = "arm+builtin" if arm else "builtin"
    return {"families": families, "families_rich": families_rich, "source": source}


def load_families(*, refresh: bool = False) -> Dict:
    """Return ``{"families": [...canonical...], "source": "arm+builtin"|"builtin"}``.

    Two modes, by design:
    - ``refresh=False`` (default, used on every editor open): **never** touches
      the network or auth. Returns the cached result if present, else the bundled
      seed. This keeps opening the BOM editor instant and guarantees it can never
      trigger a sign-in prompt.
    - ``refresh=True`` (the explicit "Refresh families" user gesture): performs a
      live ARM pull and updates the cache. A failed live pull (not signed in,
      transient ARM error) leaves any existing richer cache intact rather than
      downgrading it to seed-only.
    """
    global _CACHE

    def _result_from(r: Dict) -> Dict:
        return {
            "families": list(r["families"]),
            "families_rich": list(r["families_rich"]),
            "source": r["source"],
        }

    if not refresh:
        with _LOCK:
            if _CACHE is not None:
                return _result_from(_CACHE)
        # No cache yet and no live pull allowed — serve the bundled seed.
        result = _merge(_load_seed_families(), None)
        with _LOCK:
            if _CACHE is None:
                _CACHE = result
            else:
                result = _CACHE
        return _result_from(result)

    # Explicit refresh: attempt the live ARM pull.
    seed = _load_seed_families()
    arm = _families_from_arm()
    result = _merge(seed, arm)
    with _LOCK:
        # Only replace the cache when we actually got live data (or there was
        # nothing cached). A failed refresh must not clobber a good list.
        if result["source"] == "arm+builtin" or _CACHE is None:
            _CACHE = result
        else:
            result = _CACHE
    return _result_from(result)

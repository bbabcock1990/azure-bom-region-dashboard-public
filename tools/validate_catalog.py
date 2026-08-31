r"""Deep-validate the packaged service catalog against live Azure.

For every product in ``api/_shared/data/bom_service_catalog.json`` this:

  * confirms the ARM **provider namespace** is one Azure recognizes for the
    signed-in subscription (catches typos / retired / wrong-cloud namespaces
    that would otherwise show a service as "not available in any region"),
  * confirms the specific **resource type** exists under that provider (a
    ``*`` resource type is accepted as "the whole provider"),
  * reports how many **regions** each resource type is offered in, flagging
    global (all-region) services separately from ones with zero regions.

It hits the authoritative, subscription-scoped provider registry in a single
call: ``GET /subscriptions/{sub}/providers`` (which returns every provider —
registered or not — with per-resource-type locations), so it never
misrepresents a provider that's merely un-registered.

Run it from the repo root with the app's venv while signed in:

    .\.venv\Scripts\python.exe tools\validate_catalog.py
    .\.venv\Scripts\python.exe tools\validate_catalog.py --json report.json

Exit code is non-zero if any BAD_NAMESPACE / BAD_RESOURCE_TYPE is found, so it
can gate CI or a pre-ship check.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Make ``api/_shared`` importable regardless of where we're launched from.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "api"))

import httpx  # noqa: E402
from _shared import auth_token, sku_families  # noqa: E402

ARM_BASE = "https://management.azure.com"
PROVIDERS_API = "2024-07-01"
CATALOG = os.path.join(
    _REPO, "api", "_shared", "data", "bom_service_catalog.json")


def _token() -> str:
    info = auth_token.get_arm_default_token()
    return getattr(info, "token", None) or str(info)


def _fetch_subscription_providers(token: str, sub: str) -> dict:
    """Return ``{namespace_lower: provider_item}`` for every provider ARM
    exposes to the subscription (registered or not), paging ``nextLink``."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    url = f"{ARM_BASE}/subscriptions/{sub}/providers"
    params = {"api-version": PROVIDERS_API, "$top": "1000"}
    items: list = []
    with httpx.Client(timeout=60.0) as client:
        while url:
            r = client.get(url, params=params, headers=headers)
            r.raise_for_status()
            body = r.json()
            items += body.get("value") or []
            url = body.get("nextLink")
            params = None
    return {(it.get("namespace") or "").lower(): it for it in items}


def _regions_for(item: dict, resource_type: str) -> tuple:
    """Return ``(status, regions)`` for a catalog entry against a provider item.

    status is one of: OK, GLOBAL, ZERO_REGION, BAD_RESOURCE_TYPE.
    """
    rts = {(x.get("resourceType") or "").lower(): x
           for x in (item.get("resourceTypes") or [])}
    if resource_type == "*":
        regional, saw_global = set(), False
        for x in item.get("resourceTypes") or []:
            for loc in x.get("locations") or []:
                if not loc:
                    continue
                if str(loc).lower() == "global":
                    saw_global = True
                else:
                    regional.add(str(loc))
        if regional:
            return "OK", sorted(regional)
        return ("GLOBAL" if (saw_global or rts) else "ZERO_REGION"), []
    x = rts.get(resource_type.lower())
    if x is None:
        return "BAD_RESOURCE_TYPE", []
    regional = [str(l) for l in (x.get("locations") or [])
                if l and str(l).lower() != "global"]
    saw_global = any(str(l).lower() == "global"
                     for l in (x.get("locations") or []))
    if regional:
        return "OK", sorted(regional)
    return ("GLOBAL" if saw_global else "ZERO_REGION"), []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", metavar="PATH",
                    help="write the full per-service report as JSON")
    args = ap.parse_args()

    with open(CATALOG, encoding="utf-8") as f:
        catalog = (json.load(f) or {}).get("services") or []

    sub = sku_families._resolve_operator_subscription()
    if not sub:
        print("ERROR: no readable Azure subscription for the signed-in "
              "account — sign in first.", file=sys.stderr)
        return 2
    print(f"Validating {len(catalog)} products against subscription {sub}\n")
    providers = _fetch_subscription_providers(_token(), sub)

    buckets: dict = {"OK": [], "GLOBAL": [], "ZERO_REGION": [],
                     "BAD_NAMESPACE": [], "BAD_RESOURCE_TYPE": []}
    report = []
    for s in catalog:
        name, ns, rt = s["name"], s["provider"], s["resource_type"]
        item = providers.get(ns.lower())
        if item is None:
            status, regions = "BAD_NAMESPACE", []
        else:
            status, regions = _regions_for(item, rt)
        buckets[status].append((name, ns, rt, len(regions)))
        report.append({"name": name, "provider": ns, "resource_type": rt,
                       "status": status, "region_count": len(regions),
                       "regions": regions})

    order = ["BAD_NAMESPACE", "BAD_RESOURCE_TYPE", "ZERO_REGION",
             "GLOBAL", "OK"]
    for status in order:
        rows = buckets[status]
        print(f"{status}: {len(rows)}")
        if status in ("OK",):
            continue
        for name, ns, rt, n in rows:
            suffix = f" ({n} regions)" if n else ""
            print(f"    - {name}: {ns}/{rt}{suffix}")
    print()

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"subscription": sub, "services": report}, f, indent=2)
        print(f"Wrote {args.json}")

    problems = len(buckets["BAD_NAMESPACE"]) + len(buckets["BAD_RESOURCE_TYPE"])
    if problems:
        print(f"FAIL: {problems} product(s) reference a provider/resource "
              "type Azure doesn't recognize for this subscription.")
        return 1
    print("PASS: every product maps to a real ARM provider + resource type.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

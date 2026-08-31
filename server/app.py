"""
Local single-process web host (replaces the Azure Functions + SWA CLI stack).

Serves the static frontend in ``app/`` and the JSON API under ``/api/*`` from
one FastAPI/uvicorn process. Each ``/api`` route maps to the existing
``api/<name>/__init__.py`` handler's ``main(req)`` function; an adapter builds
the lightweight ``httpfunc.HttpRequest`` and converts the returned
``httpfunc.HttpResponse`` back into a Starlette response.

Run with:  python -m server
"""
from __future__ import annotations

import importlib
import inspect
import json
import sys
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

# Make the repo root importable so ``import api.<endpoint>`` resolves the
# handlers' ``from .._shared import ...`` relative imports.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from api._shared import httpfunc  # noqa: E402

from fastapi import FastAPI  # noqa: E402

APP_DIR = _REPO_ROOT / "app"

# Route table transcribed from the (now removed) api/*/function.json files.
# Order matters: more specific paths must precede parameterized ones so that
# e.g. /api/snapshots/latest is not captured by /api/snapshots/{run_id}.
# Optional Azure route params ({name?}) are registered as two explicit paths.
ROUTES = [
    ("snapshots/diff", ["GET"], "snapshot_diff"),
    ("snapshots/latest", ["GET"], "snapshots_latest"),
    ("snapshots/{run_id}", ["GET"], "snapshots_get"),
    ("snapshots", ["GET"], "snapshots_list"),
    ("runs/{run_id}", ["GET"], "runs_get"),
    ("runs", ["POST"], "runs_post"),
    ("run_progress", ["GET"], "run_progress_get"),
    ("quota/request-increase", ["POST"], "quota_increase"),
    ("quota/request-status", ["GET"], "quota_request_status"),
    ("quota/history", ["GET", "POST"], "quota_history"),
    ("providers/register", ["POST"], "register_provider"),
    ("providers/status", ["GET"], "register_provider"),
    ("subscription_metadata/{bom_id}", ["GET", "PUT", "DELETE"], "subscription_metadata"),
    ("subscription_metadata", ["GET", "POST", "PUT", "DELETE"], "subscription_metadata"),
    ("subscriptions", ["GET"], "subscriptions_list"),
    ("az/subscriptions", ["GET"], "az_subscriptions"),
    ("bom/import_xlsx", ["POST"], "bom_import_xlsx"),
    ("bom/sensitivity", ["GET"], "bom_sensitivity"),
    ("bom/sku_families", ["GET"], "bom_sku_families"),
    ("bom/service_catalog/{name}", ["GET", "POST", "DELETE"], "bom_service_catalog"),
    ("bom/service_catalog", ["GET", "POST", "DELETE"], "bom_service_catalog"),
    ("bom/region_catalog/{name}", ["GET", "POST", "DELETE"], "bom_region_catalog"),
    ("bom/region_catalog", ["GET", "POST", "DELETE"], "bom_region_catalog"),
    ("auth/signin", ["GET", "POST"], "auth_signin"),
    ("auth/signout", ["POST"], "auth_signout"),
    ("activity_log/clear", ["POST"], "activity_log_clear"),
    ("activity_log", ["GET"], "activity_log_list"),
    ("donor-quota-scan", ["POST"], "donor_quota_scan"),
    ("app-config", ["GET"], "app_config"),
    ("support/settings", ["GET", "POST"], "support_settings"),
    ("support/azure-tickets/close", ["POST"], "support_azure_ticket_close"),
    ("support/azure-tickets", ["GET"], "support_azure_tickets"),
    ("support/tickets/{ticket_name}", ["GET", "POST"], "support_ticket_get"),
    ("support/tickets", ["GET"], "support_tickets_list"),
    ("support/tickets", ["POST"], "support_ticket_create"),
    ("pricing/settings", ["GET", "POST"], "pricing_settings"),
    ("pricing/estimate", ["POST"], "pricing_estimate"),
    ("pricing/validate-alternatives", ["POST"], "pricing_validate_alt"),
    ("datasets/{dataset_id}/refresh", ["POST"], "datasets"),
    ("datasets/{dataset_id}/source", ["POST", "DELETE"], "datasets"),
    ("datasets/{dataset_id}", ["GET", "POST", "DELETE"], "datasets"),
    ("datasets", ["GET"], "datasets"),
    ("local-state/wipe", ["POST"], "local_state_wipe"),
    ("demo/seed", ["POST"], "demo_seed"),
]


def _load_global_headers() -> dict:
    """Read security/CSP headers from app/staticwebapp.config.json so the same
    policy the SWA emulator applied is preserved by this host."""
    cfg = APP_DIR / "staticwebapp.config.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
        return dict(data.get("globalHeaders") or {})
    except Exception:
        return {}


_GLOBAL_HEADERS = _load_global_headers()


async def _adapt_request(request: Request) -> httpfunc.HttpRequest:
    body = await request.body()
    form_data: dict = {}
    files: dict = {}
    content_type = request.headers.get("content-type", "")
    if request.method in ("POST", "PUT", "PATCH") and (
        "multipart/form-data" in content_type
        or "application/x-www-form-urlencoded" in content_type
    ):
        form = await request.form()
        for key, value in form.multi_items():
            filename = getattr(value, "filename", None)
            if filename is not None:
                files[key] = httpfunc.UploadedFile(filename, await value.read())
            else:
                form_data[key] = value
    return httpfunc.HttpRequest(
        method=request.method,
        url=str(request.url),
        headers=dict(request.headers),
        params=dict(request.query_params),
        route_params=dict(request.path_params),
        body=body,
        form=form_data,
        files=files,
    )


def _make_endpoint(handler):
    async def endpoint(request: Request) -> Response:
        req = await _adapt_request(request)
        if inspect.iscoroutinefunction(handler):
            resp = await handler(req)
        else:
            resp = await run_in_threadpool(handler, req)
        return Response(
            content=resp.get_body(),
            status_code=resp.status_code,
            media_type=resp.mimetype,
            headers=resp.headers or None,
        )
    return endpoint


def create_app() -> FastAPI:
    app = FastAPI(
        title="Azure BOM Region Dashboard (local)", docs_url=None, redoc_url=None
    )

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        for key, value in _GLOBAL_HEADERS.items():
            if key not in response.headers:
                response.headers[key] = value
        # Local dev tool: force the browser to revalidate static assets so an
        # edited app.js/styles.css is picked up on the next load (ETag/
        # Last-Modified still yield cheap 304s when unchanged). Avoids stale
        # cached assets when the ?v= querystring isn't bumped.
        if "cache-control" not in (k.lower() for k in response.headers.keys()):
            response.headers["Cache-Control"] = "no-cache"
        return response

    for route, methods, module_name in ROUTES:
        mod = importlib.import_module(f"api.{module_name}")
        app.add_route(f"/api/{route}", _make_endpoint(mod.main), methods=methods)

    # Static frontend last, so /api/* routes take precedence.
    app.mount("/", StaticFiles(directory=str(APP_DIR), html=True), name="static")

    # In demo mode, seed a sample BOM + snapshot so the dashboard is populated
    # on first launch (before any Azure sign-in). Best-effort; never fatal.
    try:
        from api._shared import demo_seed
        if demo_seed.seed_if_empty():
            print("==> Demo mode: seeded sample BOM + analysis snapshot")
    except Exception:  # pragma: no cover - defensive
        pass

    return app


app = create_app()

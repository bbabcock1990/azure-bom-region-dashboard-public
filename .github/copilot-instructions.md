# Copilot instructions — Azure BOM Region Dashboard

Local-only, single-user web app that scores Azure region/zone availability
against a customer Bill of Materials (BOM). Runs as one Python FastAPI/uvicorn
process. No Azure hosting, no Azure CLI, no Functions/SWA runtime.

## Build, run, test

Setup (Windows/PowerShell; swap activation on macOS/Linux):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r api\requirements.txt -r requirements-dev.txt
```

- **Run the app:** `.\start-local.ps1` (sets `LOCAL_MODE=true`, serves UI + `/api/*` on http://localhost:4280). Under the hood it runs `python -m server`.
- **Full test suite:** `pytest -q` (this is exactly what CI runs — `.github/workflows/ci.yml`, Python 3.11).
- **Single test file / case:** `pytest tests/test_pipeline.py -q` or `pytest tests/test_http_contract.py::test_static_index_and_security_headers -q`.
- There is no linter or build step. Pytest is the only gate.

## Architecture (the parts you must read multiple files to grasp)

This codebase was migrated **from** Azure Functions + Static Web Apps **to** a
plain local FastAPI host. That history explains most of the structure:

- **Handlers** live in `api/<name>/__init__.py` and each export `def main(req)`.
  They still receive an `httpfunc.HttpRequest` and return an
  `httpfunc.HttpResponse` — `api/_shared/httpfunc.py` is a drop-in shim for the
  slice of `azure.functions` the handlers used, so handler bodies never changed.
  **Do not import `azure.functions`.**
- **`server/app.py`** is the real host. Its `ROUTES` list maps `/api/*` paths to
  handler modules and replaces the deleted `function.json` files. Adding/changing
  an endpoint means editing `ROUTES` — and **order matters**: specific paths must
  precede parameterized ones (e.g. `snapshots/latest` before `snapshots/{run_id}`).
- **`api/_shared/`** is the engine shared by all handlers:
  - `pipeline/` — pure-Python snapshot compiler (`model.py`, `sources.py`, `geo.py`). Has no I/O; tested in isolation.
  - `arm_sku_availability.py` — projects `Microsoft.Compute/skus` into the global availability row shape the pipeline expects.
  - `arm_skus.py` / `bom_services.py` — live ARM provider + `Microsoft.Compute/skus` calls.
  - `quota_groups.py` — best-effort Azure Quota Groups + subscription vCPU usage lookups.
  - `activity_log.py` — best-effort audit trail for all API operations (never raises).
  - `auth_token.py` — in-process token acquisition via `azure-identity` `InteractiveBrowserCredential` (browser sign-in; no `az login`).
  - `storage.py` — local persistence (see below).
  - `data/` — static catalogs: `bom_service_catalog.json`, `bom_region_catalog.json`, `regions.txt`, `skus.txt`, latency CSV.
- A run (`POST /api/runs`, handler `api/runs_post/__init__.py`) fans out three
  independent live signals per region (ARM SKU availability, ARM service
  availability, ARM SSD v2 zones), then compiles one snapshot. The per-customer-sub
  ARM overlay is **opt-in and gracefully skipped on 401** (logs `arm_call_skipped`).

## Key conventions

- **Storage mirrors the Azure SDK surface, but is local.** `storage.py` exposes
  `get_table_client()` / `get_blob_container()` etc. that look like
  `azure-data-tables` / `azure-storage-blob`, but back onto a single SQLite DB
  (`local-storage/app.db`) plus snapshot JSON files. Entities are plain dicts that
  **must** carry `PartitionKey` and `RowKey`; all other keys are stored as a JSON
  document. Storage root is `LOCAL_STORAGE_DIR` (else `<repo>/local-storage`, gitignored).
- **Snapshots are files,** not table rows: `local-storage/blobs/snapshots/{sub}/{run-id}.json`.
- **Env flags:** `LOCAL_MODE=true` enables local-only endpoints (`auth_signin`,
  `az_subscriptions`) and defaults `arm_overlay` to true. `ALLOWED_ORIGIN` drives
  the Origin/Referer CSRF guard in `api/_shared/csrf.py` (tests unset it).
- **Security headers** come from `app/staticwebapp.config.json` `globalHeaders`,
  re-applied by middleware in `server/app.py` — keep CSP edits there.
- **BOM SKU-family resolution priority:** saved BOM `required_families` →
  imported xlsx `Required SKUs` sheet → built-in `pipeline_model.DEFAULT_REQUIRED_FAMILIES`.
- **Frontend** is plain static `app/` (`index.html`, `app.js`, `styles.css`) — no
  build, no framework. Bust caches via the `styles.css?v=` querystring.
- **Tests** add `api/` to `sys.path` (`tests/conftest.py`), use FastAPI `TestClient`
  for HTTP-contract tests, and stub ARM with `respx` fixtures — no network.
- `tools/bom-checker/` is the legacy standalone CLI, kept to generate
  `region_results_*.xlsx` import files; it is not part of the app runtime.

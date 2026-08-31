"""POST /api/runs

multipart/form-data:
  subscription_id   : str (required GUID)
  bom_id            : str (optional). Identifies the specific saved BOM to run
                      (multiple BOMs may share a subscription). When omitted,
                      the BOM is keyed by subscription_id (legacy behavior).
  step2_xlsx        : file (legacy path — the region_results_*.xlsx
                      produced by check_azure_regions.py). The BOM's
                      "Required SKUs" sheet is the authoritative source
                      of required SKU families and core counts in this
                      mode.
  use_saved_bom     : "true"|"false" (default false). When true, loads
                      the in-app BOM previously saved via PUT
                      /api/subscription_metadata/{sub_id}, calls ARM
                      provider show + Microsoft.Compute/skus live to
                      compute service availability + zones, and runs
                      analysis without an xlsx upload. Mutually
                      exclusive with step2_xlsx.
  customer_name     : str (optional, e.g. "Avaya", "Contoso"). When
                      omitted under use_saved_bom=true, falls back to
                      the customer_name stored in the BOM record.
  customer_segments : str (optional CSV, retained for metadata/back-compat).
                      Same fallback to the
                      saved BOM applies under use_saved_bom=true.

Synchronous run today. Returns:
  { run_id, status, status_url }   on success
  { error, message, ... }          on failure (with stable error code)
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Dict, List, Optional  # noqa: F401

from .._shared import httpfunc as func

from .._shared import auth, csrf
from .._shared import activity_log
from .._shared import auth_token
from .._shared import bom_services
from .._shared import bom_regions
from .._shared import bom_storage
from .._shared import compile as compile_mod
from .._shared import arm_sku_availability
from .._shared import run_progress

log = logging.getLogger(__name__)

GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_CUSTOMER_NAME = 80


def _err(code: str, message: str, status: int = 400, **extra) -> func.HttpResponse:
    payload = {"error": code, "message": message}
    payload.update(extra)
    return func.HttpResponse(
        json.dumps(payload), status_code=status, mimetype="application/json",
    )


def _truthy(s: str) -> bool:
    return (s or "").strip().lower() in ("true", "1", "yes", "on")


def _parse_subscription_ids_csv(raw: str) -> List[str]:
    out: List[str] = []
    for chunk in re.split(r"[\s,;]+", str(raw or "").strip()):
        value = chunk.strip()
        if not value:
            continue
        if not GUID_RE.match(value):
            raise ValueError(f"{value} is not a GUID")
        lowered = value.lower()
        if lowered not in out:
            out.append(lowered)
    return out


def _resolve_operator_subscription_id(default_info: Optional[auth_token.TokenInfo]) -> Optional[str]:
    sub_id = ((default_info.az_subscription if default_info else None) or "").strip().lower()
    if GUID_RE.match(sub_id):
        return sub_id
    try:
        subs = auth_token.list_subscriptions()
    except auth_token.AuthError:
        return None
    for sub in subs:
        candidate = str(sub.get("id") or "").strip().lower()
        if GUID_RE.match(candidate):
            return candidate
    return None


def _auth_error_message(ex: auth_token.AuthError) -> str:
    if ex.code == "cross_tenant_not_guest":
        return (
            "Your account is not a guest in the customer's tenant. "
            "Ask the customer to invite you to the target tenant, then retry."
        )
    if ex.code == "cross_tenant_no_consent":
        return (
            "The customer tenant has not granted access for this sign-in flow. "
            "Ask the customer to grant consent (or register the app path) in the target tenant, then retry."
        )
    if ex.code == "subscription_no_reader":
        return (
            "You need at least Reader on the target subscription to run subscription-scoped ARM checks."
        )
    if ex.code == "not_signed_in":
        return "ARM sign-in is required before running analysis. Open Sign in and retry."
    return (
        "ARM sign-in is required before running analysis. "
        f"Open Sign in and retry ({ex.code})."
    )


def main(req: func.HttpRequest) -> func.HttpResponse:
    log.warning("runs_post: ENTER method=%s url=%s len=%s",
                req.method, req.url, req.headers.get("Content-Length"))
    try:
        return _main(req)
    except Exception as ex:
        log.exception("runs_post top-level crash")
        return _err("internal_error",
                    f"Unhandled exception in /api/runs: {ex!r}", 500)


def _main(req: func.HttpRequest) -> func.HttpResponse:
    principal = auth.get_local_user(req)

    try:
        csrf.assert_safe_origin(req)
    except csrf.OriginError as ex:
        log.warning("origin check rejected /api/runs: %s", ex)
        return _err("origin_rejected", str(ex), 403)

    try:
        form = req.form
        files = req.files
    except Exception as ex:
        return _err("bad_request", f"Could not parse multipart body: {ex}", 400)

    subscription_ids_raw = (form.get("subscription_ids") or "").strip()
    try:
        subscription_ids = _parse_subscription_ids_csv(subscription_ids_raw)
    except ValueError as ex:
        return _err("bad_subscription", f"subscription_ids: {ex}", 400)
    subscription_id = (form.get("subscription_id") or "").strip().lower()
    if subscription_ids:
        subscription_id = subscription_ids[0]
    elif GUID_RE.match(subscription_id):
        subscription_ids = [subscription_id]
    else:
        return _err("bad_subscription", "subscription_id must be a GUID.", 400)

    # A run belongs to a specific BOM. The frontend sends bom_id; legacy
    # callers (xlsx upload, older clients) may omit it, in which case the BOM
    # is keyed by subscription (pre-decoupling behavior).
    bom_id = (form.get("bom_id") or "").strip() or None

    customer_name = (form.get("customer_name") or "").strip()[:MAX_CUSTOMER_NAME] or None

    segs_raw = (form.get("customer_segments") or "").strip()
    customer_segments = None
    if segs_raw:
        customer_segments = [s.strip().upper() for s in segs_raw.split(",") if s.strip()]

    # Optional client-generated UUID used to look up live progress while
    # this synchronous POST is still in flight. If absent or malformed,
    # progress simply isn't tracked (the call still works).
    progress_token = (form.get("progress_token") or "").strip()
    if progress_token and not run_progress.is_valid_token(progress_token):
        log.warning("runs_post: ignoring malformed progress_token=%r", progress_token[:40])
        progress_token = ""

    step2_file = files.get("step2_xlsx")
    use_saved_bom = _truthy(form.get("use_saved_bom"))

    if use_saved_bom and step2_file is not None:
        return _err(
            "bom_source_conflict",
            ("Send either use_saved_bom=true OR a step2_xlsx upload, "
             "not both."),
            400,
        )

    step2_bytes: Optional[bytes] = None
    bom_data: Optional[dict] = None
    compile_regions: Optional[list] = None
    bom_arm_token_info: Optional[auth_token.TokenInfo] = None

    if use_saved_bom:
        # Load the specific BOM by its id (multiple BOMs may share a sub).
        # Fall back to keying by subscription for legacy callers that didn't
        # send a bom_id.
        lookup_id = bom_id or subscription_id
        saved = bom_storage.get(lookup_id)
        if not saved:
            return _err(
                "no_saved_bom",
                (f"No saved BOM {lookup_id}. "
                 "Open the BOM editor and save one, or upload an xlsx."),
                404,
            )
        # The BOM record is authoritative for which subscription it targets.
        saved_sub_ids = [
            str(s).strip().lower()
            for s in (saved.get("subscription_ids") or [])
            if str(s or "").strip()
        ]
        if saved_sub_ids:
            subscription_ids = saved_sub_ids
            subscription_id = saved_sub_ids[0]
        elif saved.get("subscription_id"):
            subscription_id = saved["subscription_id"]
            subscription_ids = [subscription_id]
        bom_id = saved.get("bom_id") or lookup_id
        if not saved.get("required_skus"):
            return _err(
                "empty_saved_bom",
                ("Saved BOM has no required SKU families. Add at least "
                 "one before running analysis."),
                400,
            )
        # Saved metadata can override the optional form fields (less
        # retyping). Form values still win when explicitly set.
        if not customer_name and saved.get("customer_name"):
            customer_name = saved["customer_name"]
        if not customer_segments and saved.get("customer_segments"):
            customer_segments = [
                s.strip().upper()
                for s in str(saved["customer_segments"]).split(",")
                if s.strip()
            ]

        # Resolve regions + services, run live ARM availability check, and
        # synthesize bom_records compatible with pipeline_model.
        # Use the BOM's explicitly-saved regions when present; otherwise fall
        # back to the full BOM region catalog (the same 56-region set the
        # editor shows) so "all regions selected" analyzes all of them rather
        # than collapsing to the smaller legacy regions.txt default.
        saved_regions = saved.get("regions") or []
        if saved_regions:
            regions = [str(r).strip().lower() for r in saved_regions if r]
        else:
            try:
                regions = [r["name"] for r in bom_regions.load_merged_catalog()
                           if r.get("name")]
            except Exception:
                log.warning("region catalog load failed — falling back to regions.txt")
                regions = []
            if not regions:
                regions = arm_sku_availability.load_default_regions(compile_mod.DATA_DIR)
        # Tell compile_snapshot to scope its SKU availability queries to
        # exactly the regions we ran the BOM availability check against —
        # otherwise pipeline_model joins on a different region set and
        # drops rows. ``None`` means "use defaults" (regions.txt).
        compile_regions = list(regions)
        region_specs = bom_services.build_region_specs(
            regions, data_dir=compile_mod.DATA_DIR,
        )
        try:
            resolved_services = bom_services.resolve_services(
                [s.get("name") for s in (saved.get("services") or [])
                 if s.get("name")]
            )
        except bom_services.BomServicesError as ex:
            return _err(ex.code, ex.message, ex.status)

        # BOM service availability always runs under the operator's own ARM
        # context — /providers/{ns}?api-version=... returns tenant-agnostic
        # global metadata, so the customer's tenant is irrelevant. The
        # sub-scoped arm_token (used for the existing ARM SKU overlay in
        # compile.py) often isn't mintable when the customer sub lives in
        # a different tenant, so we don't depend on it here.
        bom_arm_token = None
        if resolved_services:
            try:
                bom_arm_token_info = auth_token.get_arm_default_token()
                bom_arm_token = bom_arm_token_info.token
            except auth_token.AuthError as ex:
                return _err(
                    "arm_token_required",
                    ("In-app BOM service availability needs an ARM token from "
                     "your sign-in (tenant-agnostic). Sign in from the dashboard "
                     f"and retry ({ex.code})."),
                    401,
                )

        try:
            if resolved_services:
                # Premium SSD v2 zone availability is a *global* property of
                # the SKU per region. The Microsoft.Compute/skus call still
                # needs *some* subscription in its URL, but the answer is
                # identical for every sub. Use the operator's own sub (where
                # they always have rights) so we get correct zone data even
                # when the customer's sub lives in a foreign tenant. Without
                # this, every region would fall through to the 401/403 path
                # and get marked "Premium SSD v2: not available".
                ssdv2_sub_id = (
                    _resolve_operator_subscription_id(bom_arm_token_info)
                    or subscription_id
                )
                region_results = bom_services.check_services_availability(
                    resolved_services, region_specs,
                    arm_token=bom_arm_token,
                    subscription_id=subscription_id,
                    ssdv2_arm_token=bom_arm_token,
                    ssdv2_subscription_id=ssdv2_sub_id,
                )
                bom_header, bom_records = bom_services.synthesize_bom_records(
                    resolved_services, region_results,
                )
            else:
                # SKU-only BOM (no service entries) — mark every region
                # SUPPORTED so analysis runs against ARM SKU data
                # only.
                bom_header, bom_records = bom_services.synthesize_empty_bom(
                    region_specs,
                )
        except bom_services.BomServicesError as ex:
            return _err(ex.code, ex.message, ex.status)

        bom_data = {
            "bom_header": bom_header,
            "bom_records": bom_records,
            "required_families": saved["required_skus"],
        }
    else:
        if step2_file is None:
            return _err(
                "missing_step2",
                ("Step 2 BOM file (region_results_*.xlsx) is required, "
                 "or set use_saved_bom=true after creating an in-app BOM."),
                400,
            )
        step2_bytes = step2_file.read()
        if not step2_bytes:
            return _err("missing_step2", "Step 2 file is empty.", 400)
        if len(step2_bytes) > MAX_FILE_BYTES:
            return _err(
                "file_too_large",
                f"Step 2 file exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB.",
                413,
            )

    token_source = "web_auth"
    subscriptions: List[Dict] = []
    default_arm_info = bom_arm_token_info
    if default_arm_info is None:
        try:
            default_arm_info = auth_token.get_arm_default_token()
        except auth_token.AuthError as ex:
            log.warning("default ARM token unavailable: %s", ex.code)
            default_arm_info = None
    operator_subscription_id = _resolve_operator_subscription_id(default_arm_info)
    for sub_id in subscription_ids:
        try:
            arm_info = auth_token.get_arm_token(sub_id)
            log.info("auto-fetched ARM token for sub=%s tenant=%s",
                     sub_id, arm_info.az_tenant)
            subscriptions.append({
                "subscription_id": sub_id,
                "arm_token": arm_info.token,
                "status": "ok",
                "role": "target",
            })
        except auth_token.AuthError as ex:
            log.warning("subscription-scoped ARM token unavailable for sub=%s: %s", sub_id, ex.code)
            subscriptions.append({
                "subscription_id": sub_id,
                "arm_token": None,
                "status": "no_access",
                "error": _auth_error_message(ex),
                "role": "target",
            })

    if (
        default_arm_info is not None
        and operator_subscription_id
        and operator_subscription_id not in [s["subscription_id"] for s in subscriptions]
    ):
        subscriptions.append({
            "subscription_id": operator_subscription_id,
            "arm_token": default_arm_info.token,
            "status": "ok",
            "role": "operator_fallback",
        })

    if not any((s.get("status") == "ok") for s in subscriptions):
        status = 401
        for sub in subscriptions:
            if sub.get("error"):
                return _err("arm_token_required", str(sub["error"]), status)
        return _err(
            "arm_token_required",
            "ARM sign-in is required before running analysis. Open Sign in and retry.",
            status,
        )

    run_id = compile_mod.new_run_id()
    web_auth = token_source in ("web_auth", "web_auth_refreshed")
    if use_saved_bom:
        source_label = ("local-web+saved-bom" if web_auth
                        else "saved-bom+live-arm")
    else:
        source_label = ("local-web+upload" if web_auth
                        else "upload+live-arm")
    log.info("run %s start sub=%s by=%s src=%s customer=%s segs=%s",
             run_id, subscription_id, principal.email, source_label,
             customer_name, customer_segments)

    compile_mod.insert_run(
        run_id, status="running", subscription_id=subscription_id,
        bom_id=bom_id,
        triggered_by_email=principal.email, triggered_by_oid=principal.oid,
        source=source_label,
        customer_name=customer_name, customer_segments=customer_segments,
    )

    activity_log.record(
        "analysis_start",
        actor_email=principal.email, actor_oid=principal.oid,
        subscription_id=subscription_id, run_id=run_id,
        api_scope="local",
        message=(f"Analysis requested for {customer_name or subscription_id} "
                 f"(source: {source_label})"),
        details={
            "subscription_id": subscription_id,
            "subscription_ids": subscription_ids,
            "sku_query_subscription_ids": [s["subscription_id"] for s in subscriptions],
            "customer_name": customer_name,
            "customer_segments": customer_segments,
            "token_source": token_source,
            "use_saved_bom": use_saved_bom,
            "files": {
                "step2_xlsx_bytes": len(step2_bytes) if step2_bytes else 0,
            },
        },
    )

    # Register progress as soon as we have a run_id, even if compile fails
    # immediately — the frontend may already be polling.
    if progress_token:
        run_progress.start(
            progress_token,
            actor_oid=principal.oid,
            phases=[
                "Loading BOM",
                "ARM SKU availability",
                "Building model",
            ],
        )

    t0 = time.time()
    try:
        compile_kwargs = {
            "subscription_id": subscription_id,
            "subscriptions": subscriptions,
            "step2_bytes": step2_bytes,
            "bom_data": bom_data,
            "regions": compile_regions,
            "customer_segments": customer_segments,
            "customer_name": customer_name,
            "triggered_by_email": principal.email,
            "triggered_by_oid": principal.oid,
            "source_label": source_label,
            "run_id": run_id,
            "progress_token": progress_token or None,
        }
        try:
            snapshot = compile_mod.compile_snapshot(**compile_kwargs)
        except compile_mod.CompileError as ex:
            if ex.code != "arm_arm_token_expired":
                raise
            log.warning("run %s got 401 from ARM; refreshing token cache and retrying once", run_id)
            try:
                for idx, target_sub_id in enumerate(subscription_ids):
                    refreshed_primary = auth_token.get_arm_token(target_sub_id, force_refresh=True)
                    subscriptions[idx] = {
                        "subscription_id": target_sub_id,
                        "arm_token": refreshed_primary.token,
                        "status": "ok",
                        "role": "target",
                    }
                if len(subscriptions) > len(subscription_ids):
                    refreshed_default = auth_token.get_arm_default_token(force_refresh=True)
                    refreshed_operator_sub = _resolve_operator_subscription_id(refreshed_default)
                    if refreshed_operator_sub:
                        subscriptions[-1] = {
                            "subscription_id": refreshed_operator_sub,
                            "arm_token": refreshed_default.token,
                            "status": "ok",
                            "role": "operator_fallback",
                        }
            except auth_token.AuthError as refresh_ex:
                raise compile_mod.CompileError(
                    refresh_ex.code,
                    _auth_error_message(refresh_ex),
                    401 if refresh_ex.code == "not_signed_in" else 502,
                ) from refresh_ex
            snapshot = compile_mod.compile_snapshot(**compile_kwargs)
    except compile_mod.CompileError as ex:
        log.warning("run %s failed: %s %s", run_id, ex.code, ex.message)
        if progress_token:
            run_progress.complete(
                progress_token, status="failed",
                run_id=run_id, error_code=ex.code, error_message=ex.message,
            )
        compile_mod.insert_run(
            run_id, status="failed", subscription_id=subscription_id,
            bom_id=bom_id,
            triggered_by_email=principal.email, triggered_by_oid=principal.oid,
            source=source_label,
            error=f"{ex.code}: {ex.message}",
            customer_name=customer_name, customer_segments=customer_segments,
        )
        activity_log.record(
            "analysis_failed",
            actor_email=principal.email, actor_oid=principal.oid,
            subscription_id=subscription_id, run_id=run_id,
            api_scope="local", status="error",
            message=f"{ex.code}: {ex.message}",
            duration_ms=int((time.time() - t0) * 1000),
        )
        return _err(ex.code, ex.message, ex.status, run_id=run_id)
    except Exception as ex:
        log.exception("run %s unexpected failure", run_id)
        if progress_token:
            run_progress.complete(
                progress_token, status="failed",
                run_id=run_id, error_code="internal_error",
                error_message=repr(ex)[:200],
            )
        compile_mod.insert_run(
            run_id, status="failed", subscription_id=subscription_id,
            bom_id=bom_id,
            triggered_by_email=principal.email, triggered_by_oid=principal.oid,
            source=source_label,
            error=f"internal_error: {ex!r}"[:1024],
            customer_name=customer_name, customer_segments=customer_segments,
        )
        activity_log.record(
            "analysis_failed",
            actor_email=principal.email, actor_oid=principal.oid,
            subscription_id=subscription_id, run_id=run_id,
            api_scope="local", status="error",
            message=f"internal_error: {ex!r}"[:200],
            duration_ms=int((time.time() - t0) * 1000),
        )
        return _err("internal_error", "Something went wrong. Check logs.", 500, run_id=run_id)

    try:
        blob_name = compile_mod.persist_snapshot(snapshot, run_id=run_id, bom_id=bom_id)
    except Exception as ex:
        log.exception("run %s persist_snapshot failed", run_id)
        if progress_token:
            run_progress.complete(
                progress_token, status="failed",
                run_id=run_id, error_code="persist_failed",
                error_message=repr(ex)[:200],
            )
        compile_mod.insert_run(
            run_id, status="failed", subscription_id=subscription_id,
            bom_id=bom_id,
            triggered_by_email=principal.email, triggered_by_oid=principal.oid,
            source=source_label,
            error=f"persist_failed: {ex!r}"[:1024],
            customer_name=customer_name, customer_segments=customer_segments,
        )
        activity_log.record(
            "analysis_failed",
            actor_email=principal.email, actor_oid=principal.oid,
            subscription_id=subscription_id, run_id=run_id,
            api_scope="local", status="error",
            message=f"persist_failed: {ex!r}"[:200],
            duration_ms=int((time.time() - t0) * 1000),
        )
        return _err("persist_failed",
                    f"Compile succeeded but writing the snapshot blob failed: {ex!r}",
                    500, run_id=run_id)

    skus_meta = (snapshot.get("meta") or {})
    compile_mod.insert_run(
        run_id, status="succeeded", subscription_id=subscription_id,
        bom_id=bom_id,
        triggered_by_email=principal.email, triggered_by_oid=principal.oid,
        source=source_label,
        snapshot_blob=blob_name,
        customer_name=customer_name, customer_segments=customer_segments,
        arm_overlay=True,
    )
    if progress_token:
        run_progress.complete(progress_token, status="succeeded", run_id=run_id)
    try:
        from .._shared import snapshot_store
        snapshot_store.prune_snapshots(bom_id)
    except Exception:
        log.debug("snapshot pruning skipped", exc_info=True)
    log.info("run %s done in %.1fs (arm_availability=%s, "
             "skus_source=%s, families=%d)",
             run_id, time.time() - t0, True,
             skus_meta.get("skus_source"), skus_meta.get("families_requested"))
    activity_log.record(
        "analysis_complete",
        actor_email=principal.email, actor_oid=principal.oid,
        subscription_id=subscription_id, run_id=run_id,
        api_scope="local", status="ok",
        message=(f"Snapshot {run_id} persisted "
                 f"(sku_availability=arm, skus_source={skus_meta.get('skus_source')})"),
        duration_ms=int((time.time() - t0) * 1000),
        details={
            "snapshot_blob": blob_name,
            "arm_overlay_applied": True,
            "skus_source": skus_meta.get("skus_source"),
            "families_requested": skus_meta.get("families_requested"),
            "regions_requested": skus_meta.get("regions_requested"),
            "mode": skus_meta.get("mode"),
            "sku_query_subscription_id": skus_meta.get("sku_query_subscription_id"),
        },
    )

    return func.HttpResponse(
        json.dumps({
            "run_id": run_id,
            "status": "succeeded",
            "status_url": f"/api/runs/{run_id}",
            "snapshot_url": f"/api/snapshots/{run_id}",
            "bom_id": bom_id or subscription_id,
            "subscription_id": subscription_id,
            "subscription_ids": subscription_ids,
            "customer_name": customer_name,
            "arm_overlay_applied": True,
            "per_sub_status": skus_meta.get("per_sub_status"),
            "mode": skus_meta.get("mode"),
            "mode_note": skus_meta.get("mode_note"),
            "skus_source": skus_meta.get("skus_source"),
            "skus_resolved": skus_meta.get("skus_resolved"),
            "sku_query_subscription_id": skus_meta.get("sku_query_subscription_id"),
        }),
        status_code=200, mimetype="application/json",
    )

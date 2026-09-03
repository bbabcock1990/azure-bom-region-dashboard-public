"""Unit tests for the non-destructive ARM ``validate`` deep-check.

Covers the error classifier, the nested-error flattener, and ``evaluate_deep``
folding, with httpx mocked so no ARM call is ever made.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
_API = os.path.join(_ROOT, "api")
if _API not in sys.path:
    sys.path.insert(0, _API)

import httpx  # noqa: E402
import pytest  # noqa: E402

from _shared import deploy_validation as dv  # noqa: E402


# ------------------------------------------------------------------ classifier

def test_classify_quota():
    hit = dv._classify("InternalSubscriptionIsOverQuotaForSku",
                       "Current Limit (Total AZ VMs): 0")
    assert hit is not None
    assert hit[0] == "quota"
    assert hit[1] == "quota"


def test_classify_sku_restriction():
    hit = dv._classify("SkuNotAvailable",
                       "The requested size is currently not available for subscription in zone.")
    assert hit is not None
    assert hit[0] == "sku_restriction"


def test_classify_region_restriction_wins_over_generic():
    hit = dv._classify("RegionDoesNotAllowProvisioning",
                       "Location 'East US' is not accepting creation of new servers.")
    assert hit is not None
    assert hit[0] == "region_restriction"


def test_classify_unrelated_returns_none():
    assert dv._classify("InvalidTemplate", "The template resource name is invalid.") is None


# ---------------------------------------------------------------- flatten error

def test_flatten_error_walks_details():
    err = {
        "code": "InvalidTemplateDeployment",
        "message": "Deployment failed.",
        "details": [
            {"code": "SkuNotAvailable", "message": "not available for subscription"},
        ],
    }
    pairs = dv._flatten_error(err)
    codes = [c for c, _ in pairs]
    assert "InvalidTemplateDeployment" in codes
    assert "SkuNotAvailable" in codes


# --------------------------------------------------------------- validate_resource

def _mock_post(monkeypatch, *, status, payload=None, text=""):
    class _Resp:
        status_code = status

        def json(self):
            if payload is None:
                raise ValueError("no json")
            return payload

        @property
        def text(self):
            return text

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(dv.httpx, "Client", _Client)


def _res():
    return {"type": "Microsoft.Cache/Redis", "name": "x", "location": "eastus"}


def test_validate_available_on_clean_200(monkeypatch):
    _mock_post(monkeypatch, status=200, payload={"properties": {"provisioningState": "Succeeded"}})
    out = dv.validate_resource(resource=_res(), region="eastus", resource_group="rg",
                               subscription_id="sub", arm_token="t")
    assert out["verdict"] == "available"


def test_validate_blocked_on_quota_error(monkeypatch):
    payload = {"error": {"code": "InternalSubscriptionIsOverQuotaForSku",
                         "message": "Current Limit (Total AZ VMs): 0"}}
    _mock_post(monkeypatch, status=400, payload=payload)
    out = dv.validate_resource(resource=_res(), region="eastus", resource_group="rg",
                               subscription_id="sub", arm_token="t")
    assert out["verdict"] == "blocked"
    assert out["block_type"] == "quota"
    assert out["ticket"] == "quota"


def test_validate_blocked_on_nested_sku_error(monkeypatch):
    payload = {"error": {"code": "InvalidTemplateDeployment", "message": "failed",
                         "details": [{"code": "SkuNotAvailable",
                                      "message": "NotAvailableForSubscription in this region"}]}}
    _mock_post(monkeypatch, status=400, payload=payload)
    out = dv.validate_resource(resource=_res(), region="eastus", resource_group="rg",
                               subscription_id="sub", arm_token="t")
    assert out["verdict"] == "blocked"
    assert out["block_type"] == "sku_restriction"


def test_validate_no_rg_on_404(monkeypatch):
    _mock_post(monkeypatch, status=404, payload={"error": {"code": "ResourceGroupNotFound"}})
    out = dv.validate_resource(resource=_res(), region="eastus", resource_group="rg",
                               subscription_id="sub", arm_token="t")
    assert out["verdict"] == "no_resource_group"


def test_validate_unverifiable_on_403(monkeypatch):
    _mock_post(monkeypatch, status=403, payload={"error": {"code": "AuthorizationFailed"}})
    out = dv.validate_resource(resource=_res(), region="eastus", resource_group="rg",
                               subscription_id="sub", arm_token="t")
    assert out["verdict"] == "unverifiable"


def test_validate_unverifiable_on_unrelated_error(monkeypatch):
    payload = {"error": {"code": "InvalidTemplate", "message": "bad probe template"}}
    _mock_post(monkeypatch, status=400, payload=payload)
    out = dv.validate_resource(resource=_res(), region="eastus", resource_group="rg",
                               subscription_id="sub", arm_token="t")
    assert out["verdict"] == "unverifiable"


# ------------------------------------------------------------------ evaluate_deep

def test_evaluate_deep_advisory_for_cosmos(monkeypatch):
    out = dv.evaluate_deep(services=[{"name": "Azure Cosmos DB", "tier": "standard"}],
                           region="eastus", resource_group="rg", subscription_id="sub",
                           arm_token="t")
    assert out[0]["verdict"] == "advisory"
    assert out[0]["ticket"] == "region_access"


def test_evaluate_deep_no_rg_prompts(monkeypatch):
    out = dv.evaluate_deep(services=[{"name": "Azure App Service", "tier": "premium_v3"}],
                           region="eastus", resource_group="", subscription_id="sub",
                           arm_token="t")
    assert out[0]["verdict"] == "no_resource_group"


def test_evaluate_deep_runs_validate_for_appservice(monkeypatch):
    payload = {"error": {"code": "InternalSubscriptionIsOverQuotaForSku",
                         "message": "Current Limit (Total AZ VMs): 0"}}
    _mock_post(monkeypatch, status=400, payload=payload)
    out = dv.evaluate_deep(services=[{"name": "Azure App Service", "tier": "premium_v3"}],
                           region="eastus", resource_group="rg", subscription_id="sub",
                           arm_token="t")
    assert out[0]["verdict"] == "blocked"
    assert out[0]["block_type"] == "quota"


def test_evaluate_deep_unknown_service_not_verifiable(monkeypatch):
    out = dv.evaluate_deep(services=[{"name": "Totally Unknown", "tier": "premium"}],
                           region="eastus", resource_group="rg", subscription_id="sub",
                           arm_token="t")
    assert out[0]["verdict"] == "not_verifiable"


# ------------------------------------------------- extended service coverage

# Every service/tier the frontend offers a deep check for must have a template
# here (or be advisory) — otherwise the UI would show "Run deep check" but the
# backend would answer not_verifiable. Keep this list in sync with
# _ZRS_DEEP_CHECKABLE in app/app.js.
_EXPECTED_VALIDATE = {
    "Azure Container Registry": ["premium"],
    "Azure SignalR Service": ["premium"],
    "Public IP Addresses": ["standard"],
    "Azure Load Balancer (Standard)": ["standard"],
    "Application Gateway (WAF v2)": ["standard_v2", "waf_v2"],
    "Azure VPN Gateway": ["vpngw1az", "vpngw2az", "vpngw3az"],
    "Azure ExpressRoute": ["ergw1az", "ergw2az", "ergw3az"],
    "Azure AI Search": ["standard_s1", "standard_s2", "standard_s3", "storage_l1", "storage_l2"],
    "Azure API Management": ["premium", "premium_v2"],
    "Azure Spring Apps": ["standard", "enterprise"],
    "App Service Environment": ["ase_v3"],
    "Azure Logic Apps": ["standard"],
}


def test_all_expected_services_have_validate_templates():
    for svc, tiers in _EXPECTED_VALIDATE.items():
        assert dv.service_validate_kind(svc) == "validate", svc
        for tier in tiers:
            assert tier in dv._VALIDATE_SERVICES[svc], f"{svc}/{tier}"


def test_builders_return_well_formed_resources():
    for svc, tiers in _EXPECTED_VALIDATE.items():
        for tier in tiers:
            built = dv._VALIDATE_SERVICES[svc][tier]("westus3", "probe")
            resources = built if isinstance(built, list) else [built]
            assert resources, f"{svc}/{tier} produced no resources"
            for res in resources:
                assert res.get("type"), f"{svc}/{tier} resource missing type"
                assert res.get("apiVersion"), f"{svc}/{tier} resource missing apiVersion"
                assert res.get("location") == "westus3", f"{svc}/{tier} wrong location"
                assert res.get("name"), f"{svc}/{tier} resource missing name"


def test_gateway_and_ase_are_multi_resource():
    # These need dependent infrastructure, so their builders must return a list
    # (VNet + subnet [+ Public IP] + the zonal resource).
    for svc, tier in (("Azure VPN Gateway", "vpngw1az"),
                      ("Azure ExpressRoute", "ergw1az"),
                      ("Application Gateway (WAF v2)", "waf_v2"),
                      ("App Service Environment", "ase_v3")):
        built = dv._VALIDATE_SERVICES[svc][tier]("eastus", "probe")
        assert isinstance(built, list) and len(built) >= 2, f"{svc}/{tier}"


def test_validate_accepts_multi_resource_list(monkeypatch):
    _mock_post(monkeypatch, status=200, payload={"properties": {"provisioningState": "Succeeded"}})
    resources = dv._VALIDATE_SERVICES["Azure VPN Gateway"]["vpngw1az"]("eastus", "probe")
    out = dv.validate_resource(resource=resources, region="eastus", resource_group="rg",
                               subscription_id="sub", arm_token="t")
    assert out["verdict"] == "available"


def test_evaluate_deep_runs_validate_for_new_service(monkeypatch):
    payload = {"error": {"code": "SubscriptionIsOverQuotaForSku",
                         "message": "Operation would exceed 'standard2' tier service quota."}}
    _mock_post(monkeypatch, status=400, payload=payload)
    out = dv.evaluate_deep(services=[{"name": "Azure AI Search", "tier": "standard_s2"}],
                           region="eastus", resource_group="rg", subscription_id="sub",
                           arm_token="t")
    assert out[0]["verdict"] == "blocked"
    assert out[0]["block_type"] == "quota"


def test_frontend_deep_checkable_set_matches_backend():
    """Every service the UI marks deep-checkable must be backed by a validate
    template or an advisory entry — otherwise the button lies."""
    app_js = os.path.join(_ROOT, "app", "app.js")
    with open(app_js, encoding="utf-8") as fh:
        src = fh.read()
    marker = "const _ZRS_DEEP_CHECKABLE = new Set(["
    start = src.index(marker) + len(marker)
    block = src[start:src.index("]);", start)]
    import re as _re
    names = _re.findall(r'"([^"]+)"', block)
    assert names, "could not parse _ZRS_DEEP_CHECKABLE"
    for name in names:
        kind = dv.service_validate_kind(name)
        assert kind in ("validate", "advisory"), f"UI offers deep check for {name!r} but backend has no template"

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

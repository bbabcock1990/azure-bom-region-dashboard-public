"""Route-table smoke tests for the local FastAPI host."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import ROUTES


def test_auth_signin_route_maps_to_signin_handler():
    assert ("auth/signin", ["GET", "POST"], "auth_signin") in ROUTES


def test_quota_request_status_route_maps_to_handler():
    assert ("quota/request-status", ["GET"], "quota_request_status") in ROUTES


def test_azure_tickets_route_registered_before_ticket_name_route():
    assert ("support/azure-tickets", ["GET"], "support_azure_tickets") in ROUTES
    routes = [r[0] for r in ROUTES]
    # The literal azure-tickets path must be matched before the {ticket_name}
    # catch-all so it isn't shadowed.
    assert routes.index("support/azure-tickets") < routes.index("support/tickets/{ticket_name}")


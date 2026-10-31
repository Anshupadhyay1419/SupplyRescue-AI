"""
Shared test fixtures.

Every test gets a brand-new, freshly seeded SQLite file in a temporary folder,
so tests never touch the real demo database and never affect each other.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import repository  # noqa: E402
from database.connection import set_database_path  # noqa: E402
from database.seed import reset_database  # noqa: E402
from services import procurement_service, quotation_service  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_database(tmp_path):
    set_database_path(tmp_path / "test.db")
    reset_database()
    yield
    set_database_path(None)


def get_scenario_request(prefix: str) -> dict:
    """Return the seeded request whose scenario starts with e.g. 'S8'."""
    return next(r for r in repository.list_requests("NEW") if (r["scenario"] or "").startswith(prefix))


def quote_all_approved(request: dict) -> None:
    """Send RFQs to every approved supplier and let the simulation answer (test helper)."""
    search = procurement_service.search_suppliers(request["part_number"])
    ids = [s["supplier_id"] for s in search["approved_suppliers"]]
    quotation_service.create_rfqs(request["id"], ids, request["quantity"])
    quotation_service.simulate_supplier_responses(request["id"])


def approve_main_scenario() -> int:
    """Plan + approve the main MCU-X100 request. Returns the purchase order id."""
    request = get_scenario_request("S8")
    quote_all_approved(request)
    plan = procurement_service.calculate_plan_for_request(request["id"])
    plan_id = procurement_service.save_plan(plan, "PROCUREMENT", request["id"])
    return procurement_service.approve_procurement_plan(plan_id)


def commitment_of(supplier_name: str) -> dict:
    return next(c for c in repository.list_active_commitments() if c["supplier_name"] == supplier_name)

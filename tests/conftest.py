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
from database.connection import fetch_all, fetch_one, set_database_path  # noqa: E402
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


def approve_new_mcu_request(budget: float, quantity: int = 1000, days: int = 7) -> int:
    """Create an MCU-X100 request with a custom budget, plan it and approve it. Returns the order id."""
    from datetime import date, timedelta

    request_id = procurement_service.create_procurement_request({
        "component_name": "Microchip", "part_number": "MCU-X100", "quantity": quantity,
        "required_by": date.today() + timedelta(days=days), "max_budget": budget,
    })
    quote_all_approved(repository.get_request(request_id))
    plan = procurement_service.calculate_plan_for_request(request_id)
    plan_id = procurement_service.save_plan(plan, "PROCUREMENT", request_id)
    return procurement_service.approve_procurement_plan(plan_id)


def commitment_of(supplier_name: str) -> dict:
    return next(c for c in repository.list_active_commitments() if c["supplier_name"] == supplier_name)


def check_inventory_and_order_consistency() -> list[str]:
    """
    Recompute, from the raw records, what every reservation and order total SHOULD be,
    and return a list of mismatches (empty list = consistent).

    Rules checked:
      1. Supplier reserved_qty == sum of that supplier's ACTIVE commitments for the part.
      2. Warehouse quantity_reserved == sum of warehouse lines on open orders.
      3. Open order total == sum(committed_qty x unit_price) of its active commitments.
      4. An open order without open disruptions covers exactly the requested quantity.
    """
    problems = []
    active = "c.status NOT IN ('CANCELLED', 'REPLACED', 'DELIVERED')"

    for row in fetch_all(f"""
        SELECT si.supplier_id, si.part_number, si.reserved_qty,
               COALESCE((SELECT SUM(c.committed_qty) FROM supplier_commitments c
                         JOIN purchase_orders o ON o.id = c.order_id
                         WHERE c.supplier_id = si.supplier_id AND c.part_number = si.part_number AND {active}
                           AND o.status IN ('CONFIRMED', 'AT_RISK')), 0) AS expected
        FROM supplier_inventory si"""):
        if row["reserved_qty"] != row["expected"]:
            problems.append(f"supplier {row['supplier_id']} {row['part_number']}: reserved {row['reserved_qty']} "
                            f"but active commitments {row['expected']}")

    for row in fetch_all("""
        SELECT w.part_number, w.quantity_reserved,
               COALESCE((SELECT SUM(oi.quantity) FROM order_items oi JOIN purchase_orders o ON o.id = oi.order_id
                         WHERE oi.source_type = 'WAREHOUSE' AND oi.part_number = w.part_number
                           AND o.status IN ('CONFIRMED', 'AT_RISK')), 0) AS expected
        FROM warehouse_inventory w"""):
        if row["quantity_reserved"] != row["expected"]:
            problems.append(f"warehouse {row['part_number']}: reserved {row['quantity_reserved']} "
                            f"but open warehouse lines {row['expected']}")

    for order in fetch_all("SELECT * FROM purchase_orders WHERE status IN ('CONFIRMED', 'AT_RISK')"):
        expected_total = fetch_one(
            f"SELECT COALESCE(SUM(c.committed_qty * c.unit_price), 0) AS t FROM supplier_commitments c "
            f"WHERE c.order_id = ? AND {active}", (order["id"],))["t"]
        if abs(order["total_cost"] - expected_total) > 0.01:
            problems.append(f"order {order['id']}: total {order['total_cost']} but commitments sum to {expected_total}")

        open_disruptions = fetch_one(
            "SELECT COUNT(*) AS n FROM disruptions WHERE order_id = ? "
            "AND status IN ('OPEN', 'RECOVERY_PROPOSED', 'UNRESOLVED')", (order["id"],))["n"]
        if not open_disruptions:
            covered = fetch_one(
                f"""SELECT COALESCE(SUM(CASE WHEN oi.source_type = 'WAREHOUSE' THEN oi.quantity
                                             WHEN {active} THEN c.committed_qty ELSE 0 END), 0) AS q
                    FROM order_items oi LEFT JOIN supplier_commitments c ON c.order_item_id = oi.id
                    WHERE oi.order_id = ?""", (order["id"],))["q"]
            required = repository.get_request(order["request_id"])["quantity"]
            if covered != required:
                problems.append(f"order {order['id']}: covers {covered} of {required} units with no open disruption")
    return problems

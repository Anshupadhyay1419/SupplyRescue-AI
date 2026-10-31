"""Recovery planning and recovery approval."""

import pytest

from database import repository
from database.connection import transaction
from services import recovery_service, simulation_service
from services.procurement_service import PlanDecisionError, reject_plan
from tests.conftest import approve_main_scenario, commitment_of


def disrupt_apex_to(quantity: int) -> int:
    approve_main_scenario()
    return simulation_service.simulate_partial_supply(commitment_of("Apex Microsystems")["id"], quantity)[0]


def test_recovery_plan_replaces_shortage_with_alternative_supplier():
    disruption_id = disrupt_apex_to(350)
    plan = recovery_service.generate_recovery_plan(disruption_id)
    assert plan["feasible"] and plan["quantity_needed"] == 250
    assert all(item["supplier_name"] != "Apex Microsystems" for item in plan["items"])
    assert all(item["supplier_name"] != "EastWind Electronics Trading" for item in plan["items"])
    assert plan["cost_difference"] == plan["total_cost"] - 250 * 100


def test_recovery_approval_updates_order_and_resolves_disruption():
    disruption_id = disrupt_apex_to(350)
    plan_id = recovery_service.save_recovery_plan(disruption_id, recovery_service.generate_recovery_plan(disruption_id))
    assert repository.get_disruption(disruption_id)["status"] == "RECOVERY_PROPOSED"
    recovery_service.approve_recovery_plan(plan_id)

    disruption = repository.get_disruption(disruption_id)
    order = repository.get_order(disruption["order_id"])
    assert disruption["status"] == "RESOLVED" and order["status"] == "CONFIRMED"
    items = repository.get_order_items(order["id"])
    covered = sum(i["quantity"] if i["source_type"] == "WAREHOUSE" else i["committed_qty"] for i in items
                  if i["commitment_status"] not in ("CANCELLED", "REPLACED"))
    assert covered == 1000
    with pytest.raises(PlanDecisionError):  # no duplicate side effects
        recovery_service.approve_recovery_plan(plan_id)


def test_supplier_cancellation_recovery():
    approve_main_scenario()
    [disruption_id] = simulation_service.simulate_supplier_cancellation(commitment_of("Delta Circuitworks")["id"])
    plan = recovery_service.generate_recovery_plan(disruption_id)
    assert plan["feasible"] and plan["quantity_covered"] == 300
    assert all(item["supplier_name"] != "Delta Circuitworks" for item in plan["items"])


def test_no_feasible_recovery_is_reported_not_fabricated():
    disruption_id = disrupt_apex_to(350)
    with transaction() as conn:  # every alternative supplier sells out
        conn.execute("UPDATE supplier_inventory SET stock_qty = reserved_qty WHERE part_number = 'MCU-X100'")
    plan = recovery_service.generate_recovery_plan(disruption_id)
    assert not plan["feasible"] and plan["quantity_covered"] == 0 and plan["reasons"]
    plan_id = recovery_service.save_recovery_plan(disruption_id, plan)
    assert repository.get_plan(plan_id)["status"] == "INFEASIBLE"
    assert repository.get_disruption(disruption_id)["status"] == "UNRESOLVED"


def test_extra_budget_allowance_is_respected():
    disruption_id = disrupt_apex_to(350)
    context = recovery_service.get_recovery_context(disruption_id, extra_budget=10000)
    base = recovery_service.get_recovery_context(disruption_id)
    assert context["remaining_budget"] == base["remaining_budget"] + 10000


def test_price_increase_can_keep_original_supplier_partially():
    approve_main_scenario()
    [disruption_id] = simulation_service.simulate_price_increase(commitment_of("Apex Microsystems")["id"], 20)
    plan = recovery_service.generate_recovery_plan(disruption_id)
    assert plan["feasible"] and plan["quantity_needed"] == 600
    plan_id = recovery_service.save_recovery_plan(disruption_id, plan)
    recovery_service.approve_recovery_plan(plan_id)
    order = repository.get_order(repository.get_disruption(disruption_id)["order_id"])
    # BlueRiver (₹110) is cheaper than Apex at its new price (₹120), so it replaces as much as it can.
    assert order["total_cost"] == 400 * 110 + 200 * 120 + 300 * 105


def test_rejected_recovery_reopens_disruption():
    disruption_id = disrupt_apex_to(350)
    plan_id = recovery_service.save_recovery_plan(disruption_id, recovery_service.generate_recovery_plan(disruption_id))
    reject_plan(plan_id)
    assert repository.get_disruption(disruption_id)["status"] == "OPEN"

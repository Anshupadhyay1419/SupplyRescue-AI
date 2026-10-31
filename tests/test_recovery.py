"""Recovery planning and recovery approval."""

import pytest

from database import repository
from database.connection import transaction
from services import recovery_service, simulation_service
from services.procurement_service import PlanDecisionError, reject_plan
from tests.conftest import (
    approve_main_scenario,
    approve_new_mcu_request,
    check_inventory_and_order_consistency,
    commitment_of,
)


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


# ---------------------------------------------------------------- consistency (no double counting)

def test_seed_data_is_consistent():
    assert check_inventory_and_order_consistency() == []


def test_reduction_recovery_keeps_inventory_and_totals_consistent():
    disruption_id = disrupt_apex_to(350)
    assert check_inventory_and_order_consistency() == []  # while AT_RISK
    plan_id = recovery_service.save_recovery_plan(disruption_id, recovery_service.generate_recovery_plan(disruption_id))
    recovery_service.approve_recovery_plan(plan_id)
    assert check_inventory_and_order_consistency() == []
    order = repository.get_order(repository.get_disruption(disruption_id)["order_id"])
    assert order["total_cost"] == 350 * 100 + 300 * 105 + 250 * 110


def test_delay_past_deadline_recovery_releases_old_reservation():
    approve_main_scenario()
    apex = commitment_of("Apex Microsystems")
    [disruption_id] = simulation_service.simulate_delivery_delay(apex["id"], 10)
    plan = recovery_service.generate_recovery_plan(disruption_id)
    assert plan["feasible"] and plan["quantity_needed"] == 600
    assert all(item["supplier_name"] != "Apex Microsystems" for item in plan["items"])
    recovery_service.approve_recovery_plan(recovery_service.save_recovery_plan(disruption_id, plan))

    assert repository.get_commitment(apex["id"])["status"] == "REPLACED"
    assert check_inventory_and_order_consistency() == []
    order = repository.get_order(repository.get_disruption(disruption_id)["order_id"])
    assert order["status"] == "CONFIRMED"
    assert order["total_cost"] == 300 * 105 + plan["total_cost"]  # Delta + replacement lines


def test_price_increase_recovery_is_consistent():
    approve_main_scenario()
    apex = commitment_of("Apex Microsystems")
    [disruption_id] = simulation_service.simulate_price_increase(apex["id"], 20)
    recovery_service.approve_recovery_plan(
        recovery_service.save_recovery_plan(disruption_id, recovery_service.generate_recovery_plan(disruption_id)))
    commitment = repository.get_commitment(apex["id"])
    assert (commitment["committed_qty"], commitment["unit_price"]) == (200, 120)  # kept 200 at the new price
    assert check_inventory_and_order_consistency() == []


def test_deadline_extension_makes_recovery_feasible_and_updates_deadline():
    approve_main_scenario()
    # 1) Delta reduces 300 -> 100; recover with BlueRiver (uses 200 of its 400 free units).
    [first] = simulation_service.simulate_partial_supply(commitment_of("Delta Circuitworks")["id"], 100)
    recovery_service.approve_recovery_plan(
        recovery_service.save_recovery_plan(first, recovery_service.generate_recovery_plan(first)))
    # 2) Apex (600) is delayed past the deadline. Fast alternatives now have only 400 units.
    [second] = simulation_service.simulate_delivery_delay(commitment_of("Apex Microsystems")["id"], 10)
    without_extension = recovery_service.generate_recovery_plan(second)
    assert not without_extension["feasible"]

    # 3) A human allows 6 extra days -> Cobalt (12-day lead time) becomes eligible.
    with_extension = recovery_service.generate_recovery_plan(second, deadline_extension_days=6)
    assert with_extension["feasible"]
    assert [(i["supplier_name"], i["quantity"]) for i in with_extension["items"]] == [
        ("Cobalt Semicon", 500), ("BlueRiver Components", 100)]
    assert with_extension["cost_difference"] == 500 * 95 + 100 * 110 - 600 * 100

    original_deadline = repository.get_disruption(second)["required_by"]
    recovery_service.approve_recovery_plan(recovery_service.save_recovery_plan(second, with_extension))
    request = repository.get_request(repository.get_disruption(second)["request_id"])
    assert request["required_by"] == with_extension["required_by"] > original_deadline
    assert check_inventory_and_order_consistency() == []


def test_extra_budget_makes_recovery_feasible_and_updates_budget():
    order_id = approve_new_mcu_request(budget=92000)  # plan costs 91,500
    apex = next(c for c in repository.list_active_commitments()
                if c["order_id"] == order_id and c["supplier_name"] == "Apex Microsystems")
    [disruption_id] = simulation_service.simulate_partial_supply(apex["id"], 350)

    without_extra = recovery_service.generate_recovery_plan(disruption_id)
    assert not without_extra["feasible"]  # BlueRiver 250 x 110 = 27,500 > 25,500 left
    with_extra = recovery_service.generate_recovery_plan(disruption_id, extra_budget=5000)
    assert with_extra["feasible"] and with_extra["total_cost"] == 27500

    recovery_service.approve_recovery_plan(recovery_service.save_recovery_plan(disruption_id, with_extra))
    order = repository.get_order(order_id)
    assert order["total_cost"] == 94000
    assert repository.get_request(order["request_id"])["max_budget"] == 94000  # human-approved new budget
    assert check_inventory_and_order_consistency() == []


def test_recovery_cannot_double_reserve_after_stock_changes():
    """If an alternative supplier's stock is taken between planning and approval, approval rolls back."""
    disruption_id = disrupt_apex_to(350)
    plan_id = recovery_service.save_recovery_plan(disruption_id, recovery_service.generate_recovery_plan(disruption_id))
    with transaction() as conn:
        conn.execute("UPDATE supplier_inventory SET reserved_qty = stock_qty WHERE part_number = 'MCU-X100' "
                     "AND supplier_id = (SELECT id FROM suppliers WHERE name = 'BlueRiver Components')")
    with pytest.raises(PlanDecisionError):
        recovery_service.approve_recovery_plan(plan_id)
    assert repository.get_plan(plan_id)["status"] == "PROPOSED"
    assert repository.get_disruption(disruption_id)["status"] == "RECOVERY_PROPOSED"

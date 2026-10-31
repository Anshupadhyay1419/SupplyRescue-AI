"""Allocation, validation, quotation simulation, scenarios and approval."""

from datetime import date, timedelta

import pytest

from database import repository
from database.connection import fetch_one
from services import planning_service, procurement_service, quotation_service
from services.procurement_service import PlanDecisionError
from tests.conftest import approve_main_scenario, get_scenario_request, quote_all_approved

TODAY = date.today()


def make_source(name, qty, price, lead, reliability=0.95, approved=True, part="P-1"):
    return {"source_type": "SUPPLIER", "supplier_id": hash(name) % 1000, "supplier_name": name, "part_number": part,
            "available_qty": qty, "unit_price": price, "lead_time_days": lead, "reliability": reliability,
            "approved": approved}


# ---------- pure allocation logic ----------

def test_cheapest_eligible_suppliers_are_used_first():
    sources = [make_source("A", 600, 100, 3), make_source("B", 400, 110, 5), make_source("D", 300, 105, 4)]
    plan = planning_service.allocate_supply(sources, "P-1", 900, TODAY + timedelta(days=7), 150000, TODAY)
    assert plan["feasible"]
    assert [(i["supplier_name"], i["quantity"]) for i in plan["items"]] == [("A", 600), ("D", 300)]
    assert plan["total_cost"] == 600 * 100 + 300 * 105


def test_unapproved_supplier_is_never_selected_even_if_cheapest():
    sources = [make_source("E", 1000, 90, 6, reliability=0.75, approved=False), make_source("A", 1000, 100, 3)]
    plan = planning_service.allocate_supply(sources, "P-1", 500, TODAY + timedelta(days=7), 1e9, TODAY)
    assert [i["supplier_name"] for i in plan["items"]] == ["A"]
    assert any(e["name"] == "E" for e in plan["excluded_sources"])


def test_low_reliability_supplier_is_excluded():
    sources = [make_source("Cheap", 1000, 50, 2, reliability=0.70), make_source("Good", 1000, 80, 2)]
    plan = planning_service.allocate_supply(sources, "P-1", 100, TODAY + timedelta(days=5), 1e9, TODAY)
    assert [i["supplier_name"] for i in plan["items"]] == ["Good"]


def test_budget_violation_makes_plan_infeasible():
    plan = planning_service.allocate_supply([make_source("A", 100, 100, 1)], "P-1", 100,
                                            TODAY + timedelta(days=5), 5000, TODAY)
    assert not plan["feasible"]
    assert any("exceeds the budget" in reason for reason in plan["reasons"])


def test_deadline_excludes_slow_supplier():
    sources = [make_source("Slow", 1000, 10, 14), make_source("Fast", 1000, 20, 2)]
    plan = planning_service.allocate_supply(sources, "P-1", 100, TODAY + timedelta(days=5), 1e9, TODAY)
    assert [i["supplier_name"] for i in plan["items"]] == ["Fast"]


def test_warehouse_stock_is_used_before_suppliers():
    warehouse = {"source_type": "WAREHOUSE", "supplier_id": None, "supplier_name": "TechNova Warehouse",
                 "part_number": "P-1", "available_qty": 100, "unit_price": 0.0, "lead_time_days": 0,
                 "reliability": 1.0, "approved": True}
    plan = planning_service.allocate_supply([make_source("A", 1000, 100, 3), warehouse], "P-1", 1000,
                                            TODAY + timedelta(days=7), 1e9, TODAY)
    assert plan["items"][0]["source_type"] == "WAREHOUSE" and plan["items"][0]["quantity"] == 100
    assert plan["total_cost"] == 900 * 100


def test_validate_plan_detects_tampered_plan():
    plan = planning_service.allocate_supply([make_source("A", 1000, 100, 3)], "P-1", 100,
                                            TODAY + timedelta(days=7), 1e9, TODAY)
    plan["items"][0]["approved"] = False
    plan["items"][0]["lead_time_days"] = 30
    result = planning_service.validate_plan(plan, "P-1", 100, TODAY + timedelta(days=7), 1e9, TODAY)
    failed = {c["name"] for c in result["checks"] if not c["passed"]}
    assert not result["valid"] and {"Supplier approval", "Deadline"} <= failed


# ---------- quotation simulation ----------

def test_quotation_simulation_lifecycle():
    request = get_scenario_request("S5")  # MEM-DDR4-8G: Juniper quotes, Tidal never answers
    quote_all_approved(request)
    status = quotation_service.summarize_rfq_status(request["id"])
    assert status == {"PENDING": 1, "RECEIVED": 1, "DECLINED": 0}
    quote = repository.list_received_quotations(request["id"])[0]
    assert quote["supplier_name"] == "Juniper Memory Systems" and quote["unit_price"] == 1450


def test_declining_supplier_and_no_duplicate_rfqs():
    request = get_scenario_request("S1")  # CAP-220UF: Umbra always declines
    quote_all_approved(request)
    quote_all_approved(request)  # second time must not duplicate RFQs
    rfqs = repository.list_rfqs(request["id"])
    assert len(rfqs) == len({r["supplier_id"] for r in rfqs}) == 3
    assert next(r for r in rfqs if r["supplier_name"] == "Umbra Components")["status"] == "DECLINED"


# ---------- scenarios from seeded data ----------

def test_scenario_multiple_supplier_allocation():
    request = get_scenario_request("S8")
    quote_all_approved(request)
    plan = procurement_service.calculate_plan_for_request(request["id"])
    allocation = [(i["supplier_name"], i["quantity"]) for i in plan["items"]]
    assert plan["feasible"] and allocation == [("TechNova Warehouse", 100), ("Apex Microsystems", 600),
                                               ("Delta Circuitworks", 300)]
    assert plan["total_cost"] == 91500


def test_scenario_partial_availability_combines_suppliers():
    request = get_scenario_request("S2")
    quote_all_approved(request)
    plan = procurement_service.calculate_plan_for_request(request["id"])
    assert plan["feasible"] and len(plan["items"]) >= 2
    assert all(i["supplier_name"] != "Saffron Electronics" for i in plan["items"])  # reliability 78%


def test_scenario_budget_constraint_is_infeasible():
    request = get_scenario_request("S5")
    quote_all_approved(request)
    plan = procurement_service.calculate_plan_for_request(request["id"])
    assert not plan["feasible"] and plan["total_cost"] > request["max_budget"]


def test_scenario_deadline_prefers_faster_supplier():
    request = get_scenario_request("S6")
    quote_all_approved(request)
    plan = procurement_service.calculate_plan_for_request(request["id"])
    assert plan["feasible"]
    assert [i["supplier_name"] for i in plan["items"]] == ["Quartz Line Components"]


def test_scenario_no_feasible_solution_is_explained():
    request = get_scenario_request("S7")
    quote_all_approved(request)
    plan = procurement_service.calculate_plan_for_request(request["id"])
    assert not plan["feasible"] and plan["items"] == []
    assert any("deadline" in reason for reason in plan["reasons"])
    assert any("unapproved" in reason for reason in plan["reasons"])


# ---------- approval ----------

def test_approval_persists_order_commitments_and_reservations():
    order_id = approve_main_scenario()
    order = repository.get_order(order_id)
    assert order["status"] == "CONFIRMED" and order["total_cost"] == 91500
    assert repository.get_request(order["request_id"])["status"] == "ORDERED"
    assert fetch_one("SELECT decision FROM approval_events WHERE plan_id = ?", (order["plan_id"],))["decision"] == "APPROVED"
    # Inventory reservation correctness
    assert repository.get_warehouse_row("MCU-X100")["quantity_reserved"] == 250
    apex = fetch_one("SELECT * FROM supplier_inventory si JOIN suppliers s ON s.id = si.supplier_id "
                     "WHERE s.name = 'Apex Microsystems' AND si.part_number = 'MCU-X100'")
    assert apex["reserved_qty"] == 600
    assert len([i for i in repository.get_order_items(order_id) if i["commitment_id"]]) == 2


def test_duplicate_approval_is_prevented():
    order_id = approve_main_scenario()
    plan_id = repository.get_order(order_id)["plan_id"]
    with pytest.raises(PlanDecisionError):
        procurement_service.approve_procurement_plan(plan_id)
    assert fetch_one("SELECT COUNT(*) AS n FROM purchase_orders WHERE plan_id = ?", (plan_id,))["n"] == 1
    assert repository.get_warehouse_row("MCU-X100")["quantity_reserved"] == 250  # not reserved twice


def test_approval_fails_safely_if_stock_changed():
    request = get_scenario_request("S8")
    quote_all_approved(request)
    plan_id = procurement_service.save_plan(procurement_service.calculate_plan_for_request(request["id"]),
                                            "PROCUREMENT", request["id"])
    from database.connection import transaction
    with transaction() as conn:  # Delta sells its stock elsewhere meanwhile
        conn.execute("UPDATE supplier_inventory SET stock_qty = 10 WHERE part_number = 'MCU-X100' AND supplier_id = "
                     "(SELECT id FROM suppliers WHERE name = 'Delta Circuitworks')")
    with pytest.raises(PlanDecisionError):
        procurement_service.approve_procurement_plan(plan_id)
    assert repository.list_orders()[0]["request_id"] != request["id"]  # no order was created
    assert repository.get_plan(plan_id)["status"] == "PROPOSED"  # rolled back, can be re-planned


def test_infeasible_plan_cannot_be_approved():
    request = get_scenario_request("S7")
    plan_id = procurement_service.save_plan(procurement_service.calculate_plan_for_request(request["id"]),
                                            "PROCUREMENT", request["id"])
    with pytest.raises(PlanDecisionError):
        procurement_service.approve_procurement_plan(plan_id)


def test_rejection_is_recorded_and_revision_created():
    request = get_scenario_request("S8")
    quote_all_approved(request)
    plan_id = procurement_service.save_plan(procurement_service.calculate_plan_for_request(request["id"]),
                                            "PROCUREMENT", request["id"])
    procurement_service.reject_plan(plan_id, comment="Too risky")
    assert repository.get_plan(plan_id)["status"] == "REJECTED"
    assert repository.get_request(request["id"])["status"] == "REJECTED"
    new_id = procurement_service.create_revised_request(request["id"], {"max_budget": 120000})
    revised = repository.get_request(new_id)
    assert revised["parent_request_id"] == request["id"] and revised["max_budget"] == 120000


def test_invalid_requirement_is_rejected():
    with pytest.raises(ValueError):
        procurement_service.create_procurement_request({
            "component_name": "", "part_number": "bad part!", "quantity": -1,
            "required_by": TODAY - timedelta(days=1), "max_budget": 0})


def test_double_click_cannot_start_two_agent_runs():
    request = get_scenario_request("S1")
    assert procurement_service.claim_request_for_planning(request["id"]) is True
    assert procurement_service.claim_request_for_planning(request["id"]) is False

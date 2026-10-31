"""Tools: compute and validate a procurement plan (deterministic Python, not the LLM)."""

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from database import repository
from services import procurement_service, quotation_service
from tools.context import compact_plan, get_run_context


@tool
def calculate_procurement_plan(config: RunnableConfig) -> dict:
    """Calculate the lowest-cost feasible allocation from free warehouse stock and RECEIVED quotations. Returns feasibility, allocation, total cost and reasons if infeasible."""
    request_id = get_run_context(config).get("request_id")
    if not request_id:
        return {"ok": False, "error": "No procurement request in this run.", "summary": "No request in context."}

    plan = procurement_service.calculate_plan_for_request(request_id)
    result = {"ok": True, **compact_plan(plan), "quotations_used": plan["quotations_used"]}

    if not plan["feasible"]:
        # Tell the agent which approved suppliers have NOT been asked yet, so it can decide to ask them.
        request = repository.get_request(request_id)
        asked = {rfq["supplier_name"] for rfq in repository.list_rfqs(request_id)}
        search = procurement_service.search_suppliers(request["part_number"])
        not_asked = [s["supplier_name"] for s in search["approved_suppliers"] if s["supplier_name"] not in asked]
        result["approved_suppliers_not_yet_asked_for_quotes"] = not_asked
        result["rfq_status_totals"] = quotation_service.summarize_rfq_status(request_id)

    allocation = ", ".join(f"{item['supplier_name']} {item['quantity']}" for item in plan["items"]) or "nothing"
    if plan["feasible"]:
        result["summary"] = (f"Feasible plan: {allocation}. Total ₹{plan['total_cost']:,.0f} "
                             f"(budget ₹{plan['budget']:,.0f}), delivery by {plan['expected_delivery']}.")
    else:
        result["summary"] = f"Infeasible: {' '.join(plan['reasons'])} Current allocation: {allocation}."
    return result


@tool
def validate_procurement_plan(config: RunnableConfig) -> dict:
    """Independently validate the current best plan: quantity, exact part number, supplier approval, deadline and budget checks."""
    request_id = get_run_context(config).get("request_id")
    if not request_id:
        return {"ok": False, "error": "No procurement request in this run.", "summary": "No request in context."}

    plan = procurement_service.calculate_plan_for_request(request_id)
    validation = procurement_service.validate_plan_for_request(request_id, plan)
    failed = [check["name"] for check in validation["checks"] if not check["passed"]]
    return {
        "ok": True,
        "valid": validation["valid"],
        "checks": validation["checks"],
        "summary": "All checks passed: " + ", ".join(c["name"] for c in validation["checks"]) + "."
        if validation["valid"] else f"Validation FAILED on: {', '.join(failed)}.",
    }

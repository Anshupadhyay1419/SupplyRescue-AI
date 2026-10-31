"""Tools used by the agent during disruption recovery."""

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from database import repository
from services import planning_service, recovery_service
from tools.context import compact_plan, get_run_context


def _context(config: RunnableConfig) -> dict:
    run = get_run_context(config)
    return recovery_service.get_recovery_context(
        run["disruption_id"], run.get("extra_budget", 0), run.get("deadline_extension_days", 0)
    )


@tool
def get_disruption_details(config: RunnableConfig) -> dict:
    """Get the detected disruption: affected supplier, original vs updated commitment, shortage, units to re-source, remaining budget and days left."""
    if not get_run_context(config).get("disruption_id"):
        return {"ok": False, "error": "No disruption in this run.", "summary": "No disruption in context."}
    ctx = _context(config)
    d = ctx["disruption"]
    return {
        "ok": True,
        "disruption_type": d["disruption_type"],
        "supplier": d["supplier_name"],
        "part_number": ctx["part_number"],
        "original_commitment_qty": d["original_qty"],
        "updated_commitment_qty": d["updated_qty"],
        "shortage_qty": d["shortage_qty"],
        "units_to_resource": ctx["units_to_resource"],
        "remaining_budget_inr": ctx["remaining_budget"],
        "required_by": ctx["required_by"].isoformat(),
        "days_available": ctx["days_available"],
        "details": d["details"],
        "summary": (f"{d['disruption_type']} at {d['supplier_name']}: {d['details']} "
                    f"Units to re-source: {ctx['units_to_resource']}, remaining budget ₹{ctx['remaining_budget']:,.0f}, "
                    f"{ctx['days_available']} day(s) left."),
    }


@tool
def check_order_status(config: RunnableConfig) -> dict:
    """Show the affected purchase order: status, and every line with its current supplier commitment."""
    disruption_id = get_run_context(config).get("disruption_id")
    if not disruption_id:
        return {"ok": False, "error": "No disruption in this run.", "summary": "No disruption in context."}
    disruption = repository.get_disruption(disruption_id)
    order = repository.get_order(disruption["order_id"])
    lines = []
    covered = 0
    for item in repository.get_order_items(order["id"]):
        if item["source_type"] == "WAREHOUSE":
            qty, status = item["quantity"], "RESERVED"
        else:
            qty = item["committed_qty"] if item["commitment_status"] not in ("CANCELLED", "REPLACED") else 0
            status = item["commitment_status"]
        covered += qty
        lines.append({"source": item["supplier_name"], "ordered_qty": item["quantity"],
                      "currently_committed_qty": qty, "status": status,
                      "expected_delivery": item["expected_delivery_date"]})
    return {
        "ok": True, "order_id": order["id"], "order_status": order["status"],
        "required_quantity": order["quantity"], "currently_covered_qty": covered, "lines": lines,
        "summary": (f"Order #{order['id']} is {order['status']}: {covered} of {order['quantity']} units "
                    f"currently covered across {len(lines)} line(s)."),
    }


@tool
def search_recovery_options(config: RunnableConfig) -> dict:
    """Search warehouse stock and alternative suppliers for the missing units and report which are eligible (approved, reliable, fast enough)."""
    if not get_run_context(config).get("disruption_id"):
        return {"ok": False, "error": "No disruption in this run.", "summary": "No disruption in context."}
    ctx = _context(config)
    options = []
    for source in recovery_service.search_recovery_options(ctx):
        problems = planning_service.find_eligibility_problems(
            source, ctx["part_number"], ctx["days_available"], planning_service.DEFAULT_MIN_RELIABILITY
        )
        options.append({"source": source["supplier_name"], "free_qty": source["available_qty"],
                        "unit_price_inr": source["unit_price"], "lead_time_days": source["lead_time_days"],
                        "eligible": not problems, "problems": problems})
    eligible = [o for o in options if o["eligible"]]
    return {
        "ok": True, "units_to_resource": ctx["units_to_resource"], "options": options,
        "summary": (f"{len(eligible)} eligible option(s) out of {len(options)} for {ctx['units_to_resource']} units: "
                    + (", ".join(f"{o['source']} ({o['free_qty']} @ ₹{o['unit_price_inr']:,.0f})" for o in eligible)
                       or "none")),
    }


@tool
def generate_recovery_plan(config: RunnableConfig) -> dict:
    """Generate the lowest-cost feasible recovery plan for the missing units, including cost difference versus the original commitment."""
    run = get_run_context(config)
    if not run.get("disruption_id"):
        return {"ok": False, "error": "No disruption in this run.", "summary": "No disruption in context."}
    plan = recovery_service.generate_recovery_plan(
        run["disruption_id"], run.get("extra_budget", 0), run.get("deadline_extension_days", 0)
    )
    allocation = ", ".join(f"{i['supplier_name']} {i['quantity']}" for i in plan["items"]) or "nothing"
    summary = (f"Feasible recovery: {allocation}. Cost ₹{plan['total_cost']:,.0f} "
               f"(₹{plan['cost_difference']:+,.0f} vs original).") if plan["feasible"] else (
        f"No feasible recovery plan found under current constraints. {' '.join(plan['reasons'])}")
    return {"ok": True, **compact_plan(plan), "cost_difference_inr": plan["cost_difference"], "summary": summary}


@tool
def validate_recovery_plan(config: RunnableConfig) -> dict:
    """Independently validate the recovery plan: quantity, exact part number, supplier approval, deadline and remaining budget."""
    run = get_run_context(config)
    if not run.get("disruption_id"):
        return {"ok": False, "error": "No disruption in this run.", "summary": "No disruption in context."}
    ctx = _context(config)
    plan = recovery_service.generate_recovery_plan(
        run["disruption_id"], run.get("extra_budget", 0), run.get("deadline_extension_days", 0)
    )
    validation = planning_service.validate_plan(
        plan, ctx["part_number"], ctx["units_to_resource"], ctx["required_by"], ctx["remaining_budget"]
    )
    failed = [c["name"] for c in validation["checks"] if not c["passed"]]
    return {
        "ok": True, "valid": validation["valid"], "checks": validation["checks"],
        "summary": "All recovery checks passed." if validation["valid"] else f"Validation FAILED on: {', '.join(failed)}.",
    }

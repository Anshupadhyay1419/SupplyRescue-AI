"""
Recovery planning after a supply disruption.

Steps (each one is also exposed to the agent as a tool):
  1. get_recovery_context   - which order, how many units must be re-sourced,
                              how much budget and time are left
  2. search_recovery_options - warehouse stock + alternative suppliers
  3. generate_recovery_plan  - same cheapest-first allocation as procurement
  4. approve_recovery_plan   - human approval, then new commitments + reservations

The supplier that caused the disruption is NOT used again for extra units,
except after a PRICE INCREASE, where keeping it at the new price is a valid
option that is compared against the alternatives.
"""

from datetime import date, datetime, timedelta

from database import repository
from database.connection import transaction
from services import planning_service
from services.procurement_service import (
    PlanDecisionError,
    check_supplier_still_eligible,
    claim_plan_decision,
    recalculate_order_total,
    release_supplier_stock,
    reserve_supplier_stock,
    reserve_warehouse_stock,
    save_plan,
)
from services.disruption_service import OPEN_DISRUPTION_STATUSES
from utils.logger import get_logger
from utils.validators import parse_date

logger = get_logger(__name__)

INACTIVE = repository.INACTIVE_COMMITMENT_STATUSES


def _committed_cost_excluding(order_id: int, excluded_commitment_id: int | None) -> float:
    """Current cost of all ACTIVE supplier commitments of an order, optionally skipping one."""
    total = 0.0
    for item in repository.get_order_items(order_id):
        if item["commitment_id"] is None:
            continue  # warehouse lines have no procurement cost
        if item["commitment_status"] in INACTIVE or item["commitment_id"] == excluded_commitment_id:
            continue
        total += item["committed_qty"] * item["committed_unit_price"]
    return round(total, 2)


def get_recovery_context(disruption_id: int, extra_budget: float = 0, deadline_extension_days: int = 0) -> dict:
    """
    Gather the facts needed to re-plan.

    units_to_resource:
      - quantity reduced / cancelled: the shortage
      - delayed past the deadline:    the whole delayed delivery
      - price increase:               the whole commitment (keep vs. switch is decided by cost)
    """
    disruption = repository.get_disruption(disruption_id)
    if disruption is None:
        raise ValueError(f"Disruption {disruption_id} not found.")
    commitment = repository.get_commitment(disruption["commitment_id"])
    request = repository.get_request(disruption["request_id"])

    kind = disruption["disruption_type"]
    if kind == "PRICE_INCREASE":
        units_to_resource = commitment["committed_qty"]
        excluded_commitment = commitment["id"]
    elif kind == "DELAYED":
        units_to_resource = disruption["shortage_qty"]
        excluded_commitment = commitment["id"]
    else:
        units_to_resource = disruption["shortage_qty"]
        excluded_commitment = None  # the reduced commitment still supplies its remaining units

    committed_cost = _committed_cost_excluding(disruption["order_id"], excluded_commitment)
    required_by = parse_date(request["required_by"]) + timedelta(days=max(deadline_extension_days, 0))
    remaining_budget = round(request["max_budget"] - committed_cost + max(extra_budget, 0), 2)

    return {
        "disruption": disruption,
        "commitment": commitment,
        "request": request,
        "part_number": commitment["part_number"],
        "units_to_resource": units_to_resource,
        "committed_cost_of_other_lines": committed_cost,
        "remaining_budget": remaining_budget,
        "required_by": required_by,
        "days_available": planning_service.days_until(required_by),
        "original_cost_of_units": round(units_to_resource * (disruption["original_price"] or 0), 2),
        "extra_budget": extra_budget,
        "deadline_extension_days": deadline_extension_days,
    }


def search_recovery_options(context: dict) -> list[dict]:
    """Candidate sources for the missing units (eligibility is decided by the planner)."""
    part_number = context["part_number"]
    disruption = context["disruption"]
    commitment = context["commitment"]
    sources = []

    warehouse = repository.get_warehouse_row(part_number)
    if warehouse and warehouse["quantity_on_hand"] - warehouse["quantity_reserved"] > 0:
        sources.append({
            "source_type": "WAREHOUSE", "supplier_id": None, "supplier_name": "TechNova Warehouse",
            "part_number": part_number, "available_qty": warehouse["quantity_on_hand"] - warehouse["quantity_reserved"],
            "unit_price": 0.0, "lead_time_days": 0, "reliability": 1.0, "approved": True,
        })

    for offer in repository.list_supplier_offers_for_part(part_number):
        if offer["supplier_id"] == commitment["supplier_id"]:
            continue  # handled below (only for price increases)
        sources.append({
            "source_type": "SUPPLIER", "supplier_id": offer["supplier_id"], "supplier_name": offer["supplier_name"],
            "part_number": offer["part_number"], "available_qty": max(offer["free_qty"], 0),
            "unit_price": offer["unit_price"], "lead_time_days": offer["lead_time_days"],
            "reliability": offer["reliability"], "approved": bool(offer["approved"]),
        })

    if disruption["disruption_type"] == "PRICE_INCREASE":
        supplier = repository.get_supplier(commitment["supplier_id"])
        days_to_delivery = max(planning_service.days_until(parse_date(commitment["expected_delivery_date"])), 0)
        sources.append({
            "source_type": "SUPPLIER", "supplier_id": supplier["id"],
            "supplier_name": supplier["name"] + " (keep, new price)", "part_number": part_number,
            "available_qty": commitment["committed_qty"], "unit_price": disruption["updated_price"],
            "lead_time_days": days_to_delivery, "reliability": supplier["reliability"],
            "approved": bool(supplier["approved"]),
        })
    return sources


def generate_recovery_plan(disruption_id: int, extra_budget: float = 0, deadline_extension_days: int = 0) -> dict:
    """Compute (but do not save) the best recovery plan for a disruption."""
    context = get_recovery_context(disruption_id, extra_budget, deadline_extension_days)
    sources = search_recovery_options(context)
    plan = planning_service.allocate_supply(
        sources=sources,
        part_number=context["part_number"],
        quantity_needed=context["units_to_resource"],
        required_by=context["required_by"],
        budget=context["remaining_budget"],
    )
    plan["cost_difference"] = round(plan["total_cost"] - context["original_cost_of_units"], 2)
    plan["original_cost_of_units"] = context["original_cost_of_units"]
    plan["disruption_id"] = disruption_id
    return plan


def save_recovery_plan(disruption_id: int, plan: dict, agent_run_id: int | None = None,
                       ai_explanation: str | None = None) -> int:
    """Store the plan and move the disruption to RECOVERY_PROPOSED (or UNRESOLVED)."""
    disruption = repository.get_disruption(disruption_id)
    plan_id = save_plan(plan, "RECOVERY", disruption["request_id"], agent_run_id, ai_explanation,
                        disruption_id=disruption_id, cost_difference=plan.get("cost_difference"))
    new_status = "RECOVERY_PROPOSED" if plan["feasible"] else "UNRESOLVED"
    with transaction() as conn:
        conn.execute(
            "UPDATE disruptions SET status = ? WHERE id = ? AND status IN ('OPEN', 'RECOVERY_PROPOSED', 'UNRESOLVED')",
            (new_status, disruption_id),
        )
    return plan_id


def approve_recovery_plan(plan_id: int, decided_by: str = "Procurement Officer",
                          comment: str | None = None, today: date | None = None) -> None:
    """
    Human approval of a RECOVERY plan, in one transaction:
      - claim the plan (no duplicate approvals)
      - re-check deadline, budget, supplier eligibility and stock
      - create new order lines + commitments and reserve their stock
      - retire/adjust the disrupted commitment where needed
      - resolve the disruption and recompute the order total and status
    """
    today = today or date.today()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with transaction() as conn:
        plan = claim_plan_decision(conn, plan_id, "APPROVED", decided_by, comment)
        if plan["plan_type"] != "RECOVERY":
            raise PlanDecisionError("This is not a recovery plan.")

        disruption = conn.execute("SELECT * FROM disruptions WHERE id = ?", (plan["disruption_id"],)).fetchone()
        if disruption["status"] not in OPEN_DISRUPTION_STATUSES:
            raise PlanDecisionError(f"Disruption is already {disruption['status']}.")
        commitment = conn.execute(
            "SELECT * FROM supplier_commitments WHERE id = ?", (disruption["commitment_id"],)
        ).fetchone()
        order_id = disruption["order_id"]
        items = conn.execute("SELECT * FROM plan_items WHERE plan_id = ?", (plan_id,)).fetchall()

        # Re-validate deadline and budget at approval time.
        deadline = parse_date(plan["required_by"])
        days_available = planning_service.days_until(deadline, today)
        if any(item["lead_time_days"] > days_available for item in items):
            raise PlanDecisionError("A delivery in this plan would now miss the deadline. Re-run recovery.")
        if sum(item["quantity"] * item["unit_price"] for item in items) > plan["budget_available"] + 0.01:
            raise PlanDecisionError("Plan cost exceeds the budget it was validated against.")

        kept_qty = 0
        for item in items:
            keeps_original = (disruption["disruption_type"] == "PRICE_INCREASE"
                              and item["supplier_id"] == commitment["supplier_id"])
            if keeps_original:
                kept_qty += item["quantity"]  # already reserved by the existing commitment
                continue

            if item["source_type"] == "WAREHOUSE":
                reserve_warehouse_stock(conn, item["part_number"], item["quantity"])
            else:
                check_supplier_still_eligible(conn, item["supplier_id"])
                reserve_supplier_stock(conn, item["supplier_id"], item["part_number"], item["quantity"])

            order_item_id = conn.execute(
                "INSERT INTO order_items (order_id, plan_item_id, source_type, supplier_id, part_number, quantity, "
                "unit_price, added_by) VALUES (?, ?, ?, ?, ?, ?, ?, 'RECOVERY')",
                (order_id, item["id"], item["source_type"], item["supplier_id"], item["part_number"],
                 item["quantity"], item["unit_price"]),
            ).lastrowid
            if item["source_type"] == "SUPPLIER":
                conn.execute(
                    "INSERT INTO supplier_commitments (order_item_id, order_id, supplier_id, part_number, "
                    "original_qty, committed_qty, unit_price, expected_delivery_date, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CONFIRMED')",
                    (order_item_id, order_id, item["supplier_id"], item["part_number"], item["quantity"],
                     item["quantity"], item["unit_price"], (today + timedelta(days=item["lead_time_days"])).isoformat()),
                )

        _retire_disrupted_commitment(conn, disruption, commitment, kept_qty, now)

        total = recalculate_order_total(conn, order_id)
        conn.execute("UPDATE disruptions SET status = 'RESOLVED', resolved_at = ? WHERE id = ?", (now, disruption["id"]))
        still_open = conn.execute(
            "SELECT COUNT(*) FROM disruptions WHERE order_id = ? AND status IN ('OPEN', 'RECOVERY_PROPOSED', 'UNRESOLVED')",
            (order_id,),
        ).fetchone()[0]
        conn.execute("UPDATE purchase_orders SET status = ? WHERE id = ?",
                     ("AT_RISK" if still_open else "CONFIRMED", order_id))

        # If the human accepted a plan with a larger budget or later deadline, record the new constraints.
        request = conn.execute(
            "SELECT r.* FROM procurement_requests r JOIN purchase_orders o ON o.request_id = r.id WHERE o.id = ?",
            (order_id,),
        ).fetchone()
        if total > request["max_budget"]:
            conn.execute("UPDATE procurement_requests SET max_budget = ? WHERE id = ?", (round(total, 2), request["id"]))
        if plan["required_by"] and plan["required_by"] > request["required_by"]:
            conn.execute("UPDATE procurement_requests SET required_by = ? WHERE id = ?",
                         (plan["required_by"], request["id"]))

    logger.info("Recovery plan %s approved; disruption %s resolved", plan_id, disruption["id"])


def _retire_disrupted_commitment(conn, disruption, commitment, kept_qty: int, now: str) -> None:
    """Adjust the commitment that caused the disruption once the recovery is approved."""
    kind = disruption["disruption_type"]
    if kind == "DELAYED":
        # The late delivery is replaced by the new suppliers; free the supplier's reservation.
        release_supplier_stock(conn, commitment["supplier_id"], commitment["part_number"], commitment["committed_qty"])
        conn.execute("UPDATE supplier_commitments SET status = 'REPLACED', updated_at = ? WHERE id = ?",
                     (now, commitment["id"]))
    elif kind == "PRICE_INCREASE":
        released = commitment["committed_qty"] - kept_qty
        if released > 0:
            release_supplier_stock(conn, commitment["supplier_id"], commitment["part_number"], released)
        if kept_qty == 0:
            conn.execute("UPDATE supplier_commitments SET status = 'REPLACED', updated_at = ? WHERE id = ?",
                         (now, commitment["id"]))
        else:
            conn.execute("UPDATE supplier_commitments SET committed_qty = ?, updated_at = ? WHERE id = ?",
                         (kept_qty, now, commitment["id"]))
    # QUANTITY_REDUCED / CANCELLED were already applied when the disruption was detected.

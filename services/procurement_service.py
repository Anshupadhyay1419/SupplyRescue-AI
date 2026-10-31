"""
Procurement business logic: requests, supplier search, plans and approval.

Flow handled here:
  create request -> (agent investigates) -> save plan (PROPOSED)
  -> human APPROVES -> purchase order + reservations + supplier commitments
  -> or human REJECTS -> revised request with new constraints

Every write that touches several tables runs inside ONE transaction.
"""

import json
from datetime import date, datetime, timedelta

from database import repository
from database.connection import transaction
from services import planning_service
from utils.logger import get_logger
from utils.validators import normalize_part_number, parse_date, validate_requirement

logger = get_logger(__name__)


class PlanDecisionError(Exception):
    """Raised when a plan cannot be approved/rejected (already decided, stock changed, ...)."""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- requests

def create_procurement_request(requirement: dict, scenario: str | None = None,
                               parent_request_id: int | None = None) -> int:
    """Validate a requirement and store it. Returns the new request id."""
    errors = validate_requirement(requirement)
    if errors:
        raise ValueError(" ".join(errors))

    part_number = normalize_part_number(requirement["part_number"])
    if repository.get_component(part_number) is None:
        raise ValueError(f"Unknown part number {part_number}. It is not in the component catalogue.")

    with transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO procurement_requests (component_name, part_number, quantity, required_by, max_budget, "
            "quality_requirements, status, scenario, parent_request_id) VALUES (?, ?, ?, ?, ?, ?, 'NEW', ?, ?)",
            (
                requirement["component_name"].strip(), part_number, requirement["quantity"],
                parse_date(requirement["required_by"]).isoformat(), float(requirement["max_budget"]),
                (requirement.get("quality_requirements") or "").strip(), scenario, parent_request_id,
            ),
        )
        request_id = cursor.lastrowid
    logger.info("Created procurement request %s for %s x %s", request_id, requirement["quantity"], part_number)
    return request_id


def claim_request_for_planning(request_id: int) -> bool:
    """
    Atomically move a request to PLANNING.

    Returns False if the agent is already working on it or it is finished.
    This is what makes a double-clicked "Start AI Procurement" button harmless.
    """
    with transaction() as conn:
        cursor = conn.execute(
            "UPDATE procurement_requests SET status = 'PLANNING' WHERE id = ? AND status IN ('NEW', 'FAILED')",
            (request_id,),
        )
        return cursor.rowcount == 1


def set_request_status(request_id: int, status: str) -> None:
    with transaction() as conn:
        conn.execute("UPDATE procurement_requests SET status = ? WHERE id = ?", (status, request_id))


def create_revised_request(request_id: int, changes: dict) -> int:
    """After a rejection, create a NEW request with adjusted constraints (history is kept)."""
    original = repository.get_request(request_id)
    if original is None:
        raise ValueError(f"Request {request_id} not found.")
    requirement = {
        "component_name": original["component_name"],
        "part_number": original["part_number"],
        "quantity": original["quantity"],
        "required_by": original["required_by"],
        "max_budget": original["max_budget"],
        "quality_requirements": original["quality_requirements"],
    }
    requirement.update(changes)
    return create_procurement_request(requirement, scenario=f"Revision of request #{request_id}",
                                      parent_request_id=request_id)


# ---------------------------------------------------------------- inventory and search

def get_warehouse_stock(part_number: str) -> dict:
    """
    Warehouse stock for one part. Reserved units are NOT usable.

    Returns {"on_hand", "reserved", "free", "location"}.
    """
    row = repository.get_warehouse_row(normalize_part_number(part_number))
    if row is None:
        return {"part_number": part_number, "on_hand": 0, "reserved": 0, "free": 0, "location": None}
    free = row["quantity_on_hand"] - row["quantity_reserved"]
    return {
        "part_number": row["part_number"],
        "on_hand": row["quantity_on_hand"],
        "reserved": row["quantity_reserved"],
        "free": max(free, 0),
        "location": row["location"],
    }


def search_suppliers(part_number: str, required_by: date | None = None, include_unapproved: bool = False) -> dict:
    """
    Find suppliers that stock the EXACT part number with free stock.

    Unapproved suppliers are counted but not returned unless explicitly asked
    (they can never be selected automatically anyway).
    """
    part_number = normalize_part_number(part_number)
    offers = repository.list_supplier_offers_for_part(part_number)
    days_available = planning_service.days_until(required_by) if required_by else None

    suppliers = []
    unapproved = []
    for offer in offers:
        if offer["free_qty"] <= 0:
            continue
        entry = {
            "supplier_id": offer["supplier_id"],
            "supplier_name": offer["supplier_name"],
            "free_qty": offer["free_qty"],
            "unit_price": offer["unit_price"],
            "lead_time_days": offer["lead_time_days"],
            "reliability": offer["reliability"],
            "approved": bool(offer["approved"]),
            "meets_reliability_minimum": offer["reliability"] >= planning_service.DEFAULT_MIN_RELIABILITY,
        }
        if days_available is not None:
            entry["can_meet_deadline"] = offer["lead_time_days"] <= days_available
        if offer["approved"]:
            suppliers.append(entry)
        else:
            unapproved.append(entry)

    incompatible = repository.list_similar_part_numbers(part_number)
    return {
        "part_number": part_number,
        "approved_suppliers": suppliers,
        "unapproved_suppliers": unapproved if include_unapproved else [],
        "unapproved_count": len(unapproved),
        "incompatible_similar_parts": incompatible,
        "days_available": days_available,
    }


# ---------------------------------------------------------------- plans

def build_sources_from_quotations(request: dict) -> list[dict]:
    """Turn warehouse stock + RECEIVED quotations into allocation sources."""
    sources = []
    warehouse = get_warehouse_stock(request["part_number"])
    if warehouse["free"] > 0:
        sources.append({
            "source_type": "WAREHOUSE", "supplier_id": None, "supplier_name": "TechNova Warehouse",
            "part_number": request["part_number"], "available_qty": warehouse["free"],
            "unit_price": 0.0, "lead_time_days": 0, "reliability": 1.0, "approved": True,
        })
    for quote in repository.list_received_quotations(request["id"]):
        sources.append({
            "source_type": "SUPPLIER", "supplier_id": quote["supplier_id"], "supplier_name": quote["supplier_name"],
            "part_number": quote["part_number"], "available_qty": quote["offered_qty"],
            "unit_price": quote["unit_price"], "lead_time_days": quote["lead_time_days"],
            "reliability": quote["reliability"], "approved": bool(quote["approved"]),
        })
    return sources


def build_market_sources(part_number: str) -> list[dict]:
    """Warehouse + EVERY catalogue offer (approved or not). Used only to explain infeasibility."""
    sources = []
    warehouse = get_warehouse_stock(part_number)
    if warehouse["free"] > 0:
        sources.append({"source_type": "WAREHOUSE", "part_number": part_number, "available_qty": warehouse["free"],
                        "lead_time_days": 0, "reliability": 1.0, "approved": True})
    for offer in repository.list_supplier_offers_for_part(part_number):
        sources.append({"source_type": "SUPPLIER", "supplier_name": offer["supplier_name"],
                        "part_number": offer["part_number"], "available_qty": max(offer["free_qty"], 0),
                        "lead_time_days": offer["lead_time_days"], "reliability": offer["reliability"],
                        "approved": bool(offer["approved"])})
    return sources


def calculate_plan_for_request(request_id: int, today: date | None = None) -> dict:
    """Compute the lowest-cost feasible plan from warehouse stock and received quotations."""
    request = repository.get_request(request_id)
    if request is None:
        raise ValueError(f"Request {request_id} not found.")
    sources = build_sources_from_quotations(request)
    plan = planning_service.allocate_supply(
        sources=sources,
        part_number=request["part_number"],
        quantity_needed=request["quantity"],
        required_by=parse_date(request["required_by"]),
        budget=request["max_budget"],
        today=today,
        market_sources=build_market_sources(request["part_number"]),
    )
    plan["quotations_used"] = sum(1 for source in sources if source["source_type"] == "SUPPLIER")
    return plan


def validate_plan_for_request(request_id: int, plan: dict, today: date | None = None) -> dict:
    """Run the independent validation checklist against the request's constraints."""
    request = repository.get_request(request_id)
    return planning_service.validate_plan(
        plan,
        part_number=request["part_number"],
        quantity_needed=plan["quantity_needed"],
        required_by=parse_date(request["required_by"]),
        budget=plan["budget"],
        today=today,
    )


def save_plan(plan: dict, plan_type: str, request_id: int, agent_run_id: int | None = None,
              ai_explanation: str | None = None, disruption_id: int | None = None,
              cost_difference: float | None = None) -> int:
    """
    Persist a plan and its lines. Older PROPOSED plans for the same request
    (or disruption) are marked SUPERSEDED so only one can be approved.
    """
    status = "PROPOSED" if plan["feasible"] else "INFEASIBLE"
    with transaction() as conn:
        if disruption_id is not None:
            conn.execute(
                "UPDATE procurement_plans SET status = 'SUPERSEDED' WHERE disruption_id = ? AND status = 'PROPOSED'",
                (disruption_id,),
            )
        else:
            conn.execute(
                "UPDATE procurement_plans SET status = 'SUPERSEDED' "
                "WHERE request_id = ? AND plan_type = 'PROCUREMENT' AND status = 'PROPOSED'",
                (request_id,),
            )
        plan_id = conn.execute(
            """
            INSERT INTO procurement_plans (plan_type, request_id, disruption_id, agent_run_id, status, feasible,
                quantity_needed, quantity_covered, total_cost, budget_available, required_by, expected_delivery,
                risk_level, risk_notes, reasons, ai_explanation, cost_difference)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_type, request_id, disruption_id, agent_run_id, status, int(plan["feasible"]),
                plan["quantity_needed"], plan["quantity_covered"], plan["total_cost"], plan["budget"],
                plan["required_by"], plan["expected_delivery"], plan["risk_level"], " ".join(plan["risk_notes"]),
                json.dumps(plan["reasons"]), ai_explanation, cost_difference,
            ),
        ).lastrowid
        for item in plan["items"]:
            conn.execute(
                "INSERT INTO plan_items (plan_id, source_type, supplier_id, part_number, quantity, unit_price, "
                "line_cost, lead_time_days, reliability) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (plan_id, item["source_type"], item["supplier_id"], item["part_number"], item["quantity"],
                 item["unit_price"], item["line_cost"], item["lead_time_days"], item["reliability"]),
            )
    logger.info("Saved %s plan %s (status %s)", plan_type, plan_id, status)
    return plan_id


# ---------------------------------------------------------------- reservations (used inside transactions)

def reserve_warehouse_stock(conn, part_number: str, quantity: int) -> None:
    """Reserve warehouse units. Fails if not enough FREE stock remains right now."""
    cursor = conn.execute(
        "UPDATE warehouse_inventory SET quantity_reserved = quantity_reserved + ? "
        "WHERE part_number = ? AND quantity_on_hand - quantity_reserved >= ?",
        (quantity, part_number, quantity),
    )
    if cursor.rowcount != 1:
        raise PlanDecisionError(f"Warehouse no longer has {quantity} free units of {part_number}.")


def reserve_supplier_stock(conn, supplier_id: int, part_number: str, quantity: int) -> None:
    """Reserve supplier units. Fails if the supplier's free stock dropped meanwhile."""
    cursor = conn.execute(
        "UPDATE supplier_inventory SET reserved_qty = reserved_qty + ? "
        "WHERE supplier_id = ? AND part_number = ? AND stock_qty - reserved_qty >= ?",
        (quantity, supplier_id, part_number, quantity),
    )
    if cursor.rowcount != 1:
        name = (repository.get_supplier(supplier_id) or {}).get("name", supplier_id)
        raise PlanDecisionError(f"{name} no longer has {quantity} free units of {part_number}.")


def release_supplier_stock(conn, supplier_id: int, part_number: str, quantity: int) -> None:
    """Give back reserved supplier units (never below zero)."""
    conn.execute(
        "UPDATE supplier_inventory SET reserved_qty = MAX(reserved_qty - ?, 0) WHERE supplier_id = ? AND part_number = ?",
        (quantity, supplier_id, part_number),
    )


def check_supplier_still_eligible(conn, supplier_id: int) -> None:
    """Re-check approval and reliability at the moment of approval."""
    supplier = conn.execute("SELECT * FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
    if supplier is None or not supplier["approved"]:
        raise PlanDecisionError("A supplier in this plan is not approved. Approval blocked.")
    if supplier["reliability"] < planning_service.DEFAULT_MIN_RELIABILITY:
        raise PlanDecisionError(f"{supplier['name']} is below the reliability minimum. Approval blocked.")


def claim_plan_decision(conn, plan_id: int, new_status: str, decided_by: str, comment: str | None) -> dict:
    """
    Move a PROPOSED plan to APPROVED/REJECTED exactly once.

    The conditional UPDATE + UNIQUE(approval_events.plan_id) guarantee that a
    double click can never approve the same plan twice.
    """
    plan = conn.execute("SELECT * FROM procurement_plans WHERE id = ?", (plan_id,)).fetchone()
    if plan is None:
        raise PlanDecisionError(f"Plan {plan_id} does not exist.")
    if plan["status"] != "PROPOSED":
        raise PlanDecisionError(f"Plan {plan_id} is already {plan['status']} - it cannot be decided again.")
    if new_status == "APPROVED" and not plan["feasible"]:
        raise PlanDecisionError("An infeasible plan cannot be approved.")

    cursor = conn.execute(
        "UPDATE procurement_plans SET status = ?, decided_at = ? WHERE id = ? AND status = 'PROPOSED'",
        (new_status, _now(), plan_id),
    )
    if cursor.rowcount != 1:
        raise PlanDecisionError(f"Plan {plan_id} was decided by someone else just now.")
    conn.execute(
        "INSERT INTO approval_events (plan_id, decision, decided_by, comment) VALUES (?, ?, ?, ?)",
        (plan_id, new_status, decided_by, comment),
    )
    return dict(plan)


def approve_procurement_plan(plan_id: int, decided_by: str = "Procurement Officer",
                             comment: str | None = None, today: date | None = None) -> int:
    """
    Human approval of a PROCUREMENT plan. Returns the new purchase order id.

    Inside one transaction:
      1. claim the plan (blocks duplicate approvals)
      2. re-validate against CURRENT data (stock, approval, deadline, budget)
      3. create the purchase order, order lines and supplier commitments
      4. reserve warehouse and supplier stock (guarded against going negative)
    Any failure rolls everything back - no half-created orders.
    """
    today = today or date.today()
    with transaction() as conn:
        plan = claim_plan_decision(conn, plan_id, "APPROVED", decided_by, comment)
        if plan["plan_type"] != "PROCUREMENT":
            raise PlanDecisionError("Use the recovery approval for recovery plans.")

        request = conn.execute("SELECT * FROM procurement_requests WHERE id = ?", (plan["request_id"],)).fetchone()
        items = conn.execute("SELECT * FROM plan_items WHERE plan_id = ?", (plan_id,)).fetchall()

        # Re-validate deadline and budget using today's date (time may have passed).
        days_available = planning_service.days_until(parse_date(request["required_by"]), today)
        total_cost = sum(item["quantity"] * item["unit_price"] for item in items)
        if total_cost > request["max_budget"]:
            raise PlanDecisionError("Plan cost now exceeds the budget. Please re-run the agent.")
        if any(item["lead_time_days"] > days_available for item in items):
            raise PlanDecisionError("A delivery in this plan would now miss the deadline. Please re-run the agent.")
        if sum(item["quantity"] for item in items) != request["quantity"]:
            raise PlanDecisionError("Plan quantity does not match the requirement.")

        order_id = conn.execute(
            "INSERT INTO purchase_orders (request_id, plan_id, status, total_cost) VALUES (?, ?, 'CONFIRMED', ?)",
            (request["id"], plan_id, total_cost),
        ).lastrowid

        for item in items:
            if item["source_type"] == "WAREHOUSE":
                reserve_warehouse_stock(conn, item["part_number"], item["quantity"])
            else:
                check_supplier_still_eligible(conn, item["supplier_id"])
                reserve_supplier_stock(conn, item["supplier_id"], item["part_number"], item["quantity"])

            order_item_id = conn.execute(
                "INSERT INTO order_items (order_id, plan_item_id, source_type, supplier_id, part_number, quantity, "
                "unit_price, added_by) VALUES (?, ?, ?, ?, ?, ?, ?, 'PROCUREMENT')",
                (order_id, item["id"], item["source_type"], item["supplier_id"], item["part_number"],
                 item["quantity"], item["unit_price"]),
            ).lastrowid

            if item["source_type"] == "SUPPLIER":
                delivery = (today + timedelta(days=item["lead_time_days"])).isoformat()
                conn.execute(
                    "INSERT INTO supplier_commitments (order_item_id, order_id, supplier_id, part_number, "
                    "original_qty, committed_qty, unit_price, expected_delivery_date, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'CONFIRMED')",
                    (order_item_id, order_id, item["supplier_id"], item["part_number"], item["quantity"],
                     item["quantity"], item["unit_price"], delivery),
                )

        conn.execute("UPDATE procurement_requests SET status = 'ORDERED' WHERE id = ?", (request["id"],))
    logger.info("Plan %s approved -> purchase order %s", plan_id, order_id)
    return order_id


def reject_plan(plan_id: int, decided_by: str = "Procurement Officer", comment: str | None = None) -> None:
    """Record a human rejection. Procurement requests become REJECTED; disruptions re-open."""
    with transaction() as conn:
        plan = claim_plan_decision(conn, plan_id, "REJECTED", decided_by, comment)
        if plan["plan_type"] == "PROCUREMENT":
            conn.execute("UPDATE procurement_requests SET status = 'REJECTED' WHERE id = ?", (plan["request_id"],))
        else:
            conn.execute(
                "UPDATE disruptions SET status = 'OPEN' WHERE id = ? AND status = 'RECOVERY_PROPOSED'",
                (plan["disruption_id"],),
            )
    logger.info("Plan %s rejected by %s", plan_id, decided_by)

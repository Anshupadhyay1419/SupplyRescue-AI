"""
Read-only queries shared by the services, tools and dashboard.

Writes live in the services (they need business rules and transactions).
Keeping the SELECT statements here means the UI never builds SQL itself.
"""

import json

from database.connection import fetch_all, fetch_one

# Commitments in these states no longer supply goods to the order.
INACTIVE_COMMITMENT_STATUSES = ("CANCELLED", "REPLACED")


# ---------- Components, suppliers, inventory ----------

def get_component(part_number: str) -> dict | None:
    return fetch_one("SELECT * FROM components WHERE part_number = ?", (part_number,))


def list_components() -> list[dict]:
    return fetch_all("SELECT * FROM components ORDER BY part_number")


def get_supplier_by_name(name: str) -> dict | None:
    """Case-insensitive exact supplier lookup."""
    return fetch_one("SELECT * FROM suppliers WHERE LOWER(name) = LOWER(?)", (name.strip(),))


def get_supplier(supplier_id: int) -> dict | None:
    return fetch_one("SELECT * FROM suppliers WHERE id = ?", (supplier_id,))


def list_supplier_directory() -> list[dict]:
    """Every supplier offer with free (unreserved) stock - used by the Supplier Directory page."""
    return fetch_all(
        """
        SELECT s.id AS supplier_id, s.name AS supplier_name, s.country, s.reliability, s.approved,
               c.name AS component, si.part_number, si.stock_qty, si.reserved_qty,
               (si.stock_qty - si.reserved_qty) AS free_qty, si.unit_price, si.lead_time_days
        FROM supplier_inventory si
        JOIN suppliers s ON s.id = si.supplier_id
        JOIN components c ON c.part_number = si.part_number
        ORDER BY si.part_number, si.unit_price
        """
    )


def list_supplier_offers_for_part(part_number: str) -> list[dict]:
    """All offers for an EXACT part number (approved and unapproved)."""
    return fetch_all(
        """
        SELECT s.id AS supplier_id, s.name AS supplier_name, s.reliability, s.approved,
               si.part_number, (si.stock_qty - si.reserved_qty) AS free_qty,
               si.unit_price, si.lead_time_days
        FROM supplier_inventory si
        JOIN suppliers s ON s.id = si.supplier_id
        WHERE si.part_number = ?
        ORDER BY si.unit_price
        """,
        (part_number,),
    )


def list_similar_part_numbers(part_number: str) -> list[dict]:
    """Offers whose part number only LOOKS similar (e.g. MCU-X100B). Shown as incompatible."""
    return fetch_all(
        """
        SELECT s.name AS supplier_name, si.part_number
        FROM supplier_inventory si JOIN suppliers s ON s.id = si.supplier_id
        WHERE si.part_number LIKE ? AND si.part_number <> ?
        """,
        (part_number + "%", part_number),
    )


def get_warehouse_row(part_number: str) -> dict | None:
    return fetch_one("SELECT * FROM warehouse_inventory WHERE part_number = ?", (part_number,))


def list_warehouse_inventory() -> list[dict]:
    return fetch_all(
        "SELECT w.*, (w.quantity_on_hand - w.quantity_reserved) AS free_qty, c.name AS component "
        "FROM warehouse_inventory w JOIN components c ON c.part_number = w.part_number ORDER BY w.part_number"
    )


# ---------- Requests, RFQs, quotations ----------

def get_request(request_id: int) -> dict | None:
    return fetch_one("SELECT * FROM procurement_requests WHERE id = ?", (request_id,))


def list_requests(status: str | None = None) -> list[dict]:
    if status:
        return fetch_all("SELECT * FROM procurement_requests WHERE status = ? ORDER BY id", (status,))
    return fetch_all("SELECT * FROM procurement_requests ORDER BY id DESC")


def list_rfqs(request_id: int | None = None) -> list[dict]:
    sql = """
        SELECT r.*, s.name AS supplier_name, q.offered_qty, q.unit_price, q.lead_time_days,
               q.valid_until, q.supplier_message, q.created_at AS quoted_at
        FROM rfqs r
        JOIN suppliers s ON s.id = r.supplier_id
        LEFT JOIN quotations q ON q.rfq_id = r.id
    """
    if request_id is not None:
        return fetch_all(sql + " WHERE r.request_id = ? ORDER BY r.id", (request_id,))
    return fetch_all(sql + " ORDER BY r.id DESC")


def list_received_quotations(request_id: int) -> list[dict]:
    return fetch_all(
        """
        SELECT q.*, s.name AS supplier_name, s.reliability, s.approved
        FROM quotations q
        JOIN rfqs r ON r.id = q.rfq_id
        JOIN suppliers s ON s.id = q.supplier_id
        WHERE r.request_id = ? AND r.status = 'RECEIVED'
        ORDER BY q.unit_price
        """,
        (request_id,),
    )


# ---------- Plans ----------

def get_plan(plan_id: int) -> dict | None:
    plan = fetch_one("SELECT * FROM procurement_plans WHERE id = ?", (plan_id,))
    if plan:
        plan["reasons"] = json.loads(plan["reasons"] or "[]")
        plan["items"] = get_plan_items(plan_id)
    return plan


def get_plan_items(plan_id: int) -> list[dict]:
    return fetch_all(
        """
        SELECT pi.*, COALESCE(s.name, 'TechNova Warehouse') AS supplier_name
        FROM plan_items pi LEFT JOIN suppliers s ON s.id = pi.supplier_id
        WHERE pi.plan_id = ? ORDER BY pi.id
        """,
        (plan_id,),
    )


def list_plans(plan_type: str | None = None, request_id: int | None = None,
               disruption_id: int | None = None) -> list[dict]:
    conditions, params = [], []
    if plan_type:
        conditions.append("plan_type = ?")
        params.append(plan_type)
    if request_id is not None:
        conditions.append("request_id = ?")
        params.append(request_id)
    if disruption_id is not None:
        conditions.append("disruption_id = ?")
        params.append(disruption_id)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    plans = fetch_all(f"SELECT * FROM procurement_plans {where} ORDER BY id DESC", tuple(params))
    for plan in plans:
        plan["reasons"] = json.loads(plan["reasons"] or "[]")
    return plans


def get_approval_event(plan_id: int) -> dict | None:
    return fetch_one("SELECT * FROM approval_events WHERE plan_id = ?", (plan_id,))


# ---------- Orders and commitments ----------

def list_orders() -> list[dict]:
    return fetch_all(
        """
        SELECT o.*, r.part_number, r.component_name, r.quantity, r.required_by, r.max_budget, r.scenario
        FROM purchase_orders o JOIN procurement_requests r ON r.id = o.request_id
        ORDER BY o.id DESC
        """
    )


def get_order(order_id: int) -> dict | None:
    return fetch_one(
        """
        SELECT o.*, r.part_number, r.component_name, r.quantity, r.required_by, r.max_budget
        FROM purchase_orders o JOIN procurement_requests r ON r.id = o.request_id
        WHERE o.id = ?
        """,
        (order_id,),
    )


def get_order_items(order_id: int) -> list[dict]:
    return fetch_all(
        """
        SELECT oi.*, COALESCE(s.name, 'TechNova Warehouse') AS supplier_name,
               c.id AS commitment_id, c.original_qty, c.committed_qty, c.expected_delivery_date,
               c.status AS commitment_status, c.unit_price AS committed_unit_price
        FROM order_items oi
        LEFT JOIN suppliers s ON s.id = oi.supplier_id
        LEFT JOIN supplier_commitments c ON c.order_item_id = oi.id
        WHERE oi.order_id = ? ORDER BY oi.id
        """,
        (order_id,),
    )


def get_commitment(commitment_id: int) -> dict | None:
    return fetch_one(
        """
        SELECT c.*, s.name AS supplier_name, r.required_by, r.max_budget, r.id AS request_id
        FROM supplier_commitments c
        JOIN suppliers s ON s.id = c.supplier_id
        JOIN purchase_orders o ON o.id = c.order_id
        JOIN procurement_requests r ON r.id = o.request_id
        WHERE c.id = ?
        """,
        (commitment_id,),
    )


def list_active_commitments() -> list[dict]:
    """Commitments that can still be disrupted (not delivered, cancelled or replaced)."""
    return fetch_all(
        """
        SELECT c.*, s.name AS supplier_name, o.status AS order_status
        FROM supplier_commitments c
        JOIN suppliers s ON s.id = c.supplier_id
        JOIN purchase_orders o ON o.id = c.order_id
        WHERE c.status NOT IN ('CANCELLED', 'REPLACED', 'DELIVERED') AND c.committed_qty > 0
              AND o.status IN ('CONFIRMED', 'AT_RISK')
        ORDER BY c.id DESC
        """
    )


def list_supplier_updates() -> list[dict]:
    return fetch_all(
        "SELECT u.*, s.name AS supplier_name FROM supplier_updates u "
        "JOIN supplier_commitments c ON c.id = u.commitment_id JOIN suppliers s ON s.id = c.supplier_id "
        "ORDER BY u.id DESC"
    )


# ---------- Disruptions ----------

def get_disruption(disruption_id: int) -> dict | None:
    return fetch_one(
        """
        SELECT d.*, s.name AS supplier_name, c.part_number, c.supplier_id, r.id AS request_id,
               r.required_by, r.max_budget, r.quantity AS order_quantity
        FROM disruptions d
        JOIN supplier_commitments c ON c.id = d.commitment_id
        JOIN suppliers s ON s.id = c.supplier_id
        JOIN purchase_orders o ON o.id = d.order_id
        JOIN procurement_requests r ON r.id = o.request_id
        WHERE d.id = ?
        """,
        (disruption_id,),
    )


def list_disruptions(statuses: tuple | None = None) -> list[dict]:
    sql = """
        SELECT d.*, s.name AS supplier_name, c.part_number
        FROM disruptions d
        JOIN supplier_commitments c ON c.id = d.commitment_id
        JOIN suppliers s ON s.id = c.supplier_id
    """
    if statuses:
        placeholders = ", ".join("?" for _ in statuses)
        return fetch_all(sql + f" WHERE d.status IN ({placeholders}) ORDER BY d.id DESC", tuple(statuses))
    return fetch_all(sql + " ORDER BY d.id DESC")


# ---------- Agent activity ----------

def list_agent_runs() -> list[dict]:
    return fetch_all("SELECT * FROM agent_runs ORDER BY id DESC")


def get_agent_run(run_id: int) -> dict | None:
    return fetch_one("SELECT * FROM agent_runs WHERE id = ?", (run_id,))


def list_activity(run_id: int) -> list[dict]:
    return fetch_all(
        "SELECT * FROM agent_activity_logs WHERE run_id = ? ORDER BY step_number, id", (run_id,)
    )


# ---------- Dashboard KPIs ----------

def get_dashboard_kpis() -> dict:
    """Numbers for the Overview page, all computed from the database."""

    def scalar(sql, params=()):
        row = fetch_one(sql, params)
        return list(row.values())[0] if row else 0

    open_statuses = "('OPEN', 'RECOVERY_PROPOSED', 'UNRESOLVED')"
    return {
        "total_requests": scalar("SELECT COUNT(*) FROM procurement_requests"),
        "active_orders": scalar("SELECT COUNT(*) FROM purchase_orders WHERE status IN ('CONFIRMED', 'AT_RISK')"),
        "active_disruptions": scalar(f"SELECT COUNT(*) FROM disruptions WHERE status IN {open_statuses}"),
        "units_at_risk": scalar(
            f"SELECT COALESCE(SUM(shortage_qty), 0) FROM disruptions WHERE status IN {open_statuses}"
        ),
        "recovery_plans": scalar("SELECT COUNT(*) FROM procurement_plans WHERE plan_type = 'RECOVERY'"),
        "recovery_cost": scalar(
            "SELECT COALESCE(SUM(total_cost), 0) FROM procurement_plans "
            "WHERE plan_type = 'RECOVERY' AND status IN ('PROPOSED', 'APPROVED')"
        ),
    }

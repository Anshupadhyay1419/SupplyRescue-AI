"""
Disruption detection (the "commitment monitor").

A disruption is ONLY raised from an observable signal: a row in the
`supplier_updates` table (a supplier changed quantity, date or price, or
cancelled). In this project those signals are created by the Simulation Lab.
Nothing here "predicts" shortages without evidence.

Detection = compare the supplier's NEW promise with its CURRENT promise:
    old 600 units, new 350 units  ->  shortage 250 units (pre-delivery risk)
"""

from datetime import date, datetime

from database import repository
from database.connection import transaction
from services.planning_service import calculate_shortage
from utils.logger import get_logger
from utils.validators import parse_date

logger = get_logger(__name__)

OPEN_DISRUPTION_STATUSES = ("OPEN", "RECOVERY_PROPOSED", "UNRESOLVED")


def record_supplier_update(commitment_id: int, update_type: str, new_quantity: int | None = None,
                           new_date: str | None = None, new_unit_price: float | None = None,
                           message: str | None = None) -> int:
    """Store an inbound (simulated) supplier signal. Returns its id."""
    commitment = repository.get_commitment(commitment_id)
    if commitment is None:
        raise ValueError(f"Commitment {commitment_id} does not exist.")
    if commitment["status"] in ("CANCELLED", "REPLACED", "DELIVERED"):
        raise ValueError(f"Commitment {commitment_id} is {commitment['status']} and can no longer change.")
    if new_quantity is not None and new_quantity < 0:
        raise ValueError("New quantity cannot be negative.")

    with transaction() as conn:
        update_id = conn.execute(
            "INSERT INTO supplier_updates (commitment_id, update_type, new_quantity, new_date, new_unit_price, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (commitment_id, update_type, new_quantity, new_date, new_unit_price, message),
        ).lastrowid
    logger.info("Supplier update %s recorded for commitment %s (%s)", update_id, commitment_id, update_type)
    return update_id


def classify_severity(shortage_qty: int, order_quantity: int) -> str:
    """HIGH if 30%+ of the order is at risk, MEDIUM if 10%+, else LOW."""
    if order_quantity <= 0:
        return "LOW"
    share = shortage_qty / order_quantity
    if share >= 0.30:
        return "HIGH"
    if share >= 0.10:
        return "MEDIUM"
    return "LOW"


def analyze_commitment_change(commitment: dict, update: dict, order_quantity: int,
                              required_by: date, order_total: float, budget: float) -> dict | None:
    """
    Pure function: decide whether a supplier update is a disruption.

    Returns None when the update is harmless (e.g. quantity increased),
    otherwise a dictionary describing the disruption.
    """
    old_qty = commitment["committed_qty"]
    result = {
        "original_qty": old_qty, "updated_qty": old_qty, "shortage_qty": 0,
        "original_date": commitment["expected_delivery_date"], "updated_date": commitment["expected_delivery_date"],
        "original_price": commitment["unit_price"], "updated_price": commitment["unit_price"],
        "needs_recovery": True,
    }

    if update["update_type"] == "CANCELLATION" or (
        update["update_type"] == "QUANTITY_CHANGE" and update["new_quantity"] == 0
    ):
        result.update(disruption_type="CANCELLED", updated_qty=0, shortage_qty=old_qty, severity="HIGH",
                      details=f"Supplier cancelled its commitment of {old_qty} units.")
        return result

    if update["update_type"] == "QUANTITY_CHANGE":
        new_qty = update["new_quantity"]
        shortage = calculate_shortage(old_qty, new_qty)
        if shortage == 0:
            return None  # supplier promised the same or more - not a risk
        result.update(disruption_type="QUANTITY_REDUCED", updated_qty=new_qty, shortage_qty=shortage,
                      severity=classify_severity(shortage, order_quantity),
                      details=f"Commitment reduced from {old_qty} to {new_qty} units "
                              f"({shortage} units short) before delivery.")
        return result

    if update["update_type"] == "DATE_CHANGE":
        new_date = parse_date(update["new_date"])
        old_date = parse_date(commitment["expected_delivery_date"])
        if new_date is None or new_date <= old_date:
            return None  # earlier delivery is good news
        result["updated_date"] = new_date.isoformat()
        if new_date > required_by:
            # The whole delayed delivery arrives too late, so those units are effectively missing.
            result.update(disruption_type="DELAYED", shortage_qty=old_qty, severity="HIGH",
                          details=f"Delivery moved from {old_date} to {new_date}, after the required date "
                                  f"{required_by}. {old_qty} units will arrive too late.")
        else:
            result.update(disruption_type="DELAYED", severity="LOW", needs_recovery=False,
                          details=f"Delivery moved from {old_date} to {new_date}, still before the required "
                                  f"date {required_by}. No action needed.")
        return result

    if update["update_type"] == "PRICE_CHANGE":
        new_price = update["new_unit_price"]
        if new_price is None or new_price <= commitment["unit_price"]:
            return None
        extra_cost = round((new_price - commitment["unit_price"]) * old_qty, 2)
        new_total = order_total + extra_cost
        result.update(disruption_type="PRICE_INCREASE", updated_price=new_price,
                      severity="HIGH" if new_total > budget else "MEDIUM",
                      details=f"Unit price rose from ₹{commitment['unit_price']:,.2f} to ₹{new_price:,.2f} "
                              f"(+₹{extra_cost:,.0f} on {old_qty} units). Order total would become "
                              f"₹{new_total:,.0f} against a budget of ₹{budget:,.0f}.")
        return result

    return None


def detect_supply_disruptions() -> list[int]:
    """
    Process every unprocessed supplier update and raise disruptions.

    For each signal, in ONE transaction:
      - compute the change (analyze_commitment_change)
      - apply the supplier's new promise to the commitment
      - release supplier reservations the supplier can no longer honour
      - create the disruption row and mark the order AT_RISK
      - mark the signal processed (so it is never detected twice)
    Returns the ids of new disruptions.
    """
    new_disruption_ids = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with transaction() as conn:
        updates = conn.execute("SELECT * FROM supplier_updates WHERE processed = 0 ORDER BY id").fetchall()
        for update in updates:
            commitment = conn.execute(
                "SELECT * FROM supplier_commitments WHERE id = ?", (update["commitment_id"],)
            ).fetchone()
            order = conn.execute("SELECT * FROM purchase_orders WHERE id = ?", (commitment["order_id"],)).fetchone()
            request = conn.execute(
                "SELECT * FROM procurement_requests WHERE id = ?", (order["request_id"],)
            ).fetchone()

            analysis = analyze_commitment_change(
                dict(commitment), dict(update), request["quantity"], parse_date(request["required_by"]),
                order["total_cost"], request["max_budget"],
            )
            conn.execute("UPDATE supplier_updates SET processed = 1 WHERE id = ?", (update["id"],))
            if analysis is None:
                continue

            _apply_supplier_change(conn, commitment, analysis, now)

            status = "OPEN" if analysis["needs_recovery"] else "NO_ACTION_NEEDED"
            disruption_id = conn.execute(
                """
                INSERT INTO disruptions (supplier_update_id, commitment_id, order_id, disruption_type, original_qty,
                    updated_qty, shortage_qty, original_date, updated_date, original_price, updated_price,
                    severity, status, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (update["id"], commitment["id"], order["id"], analysis["disruption_type"], analysis["original_qty"],
                 analysis["updated_qty"], analysis["shortage_qty"], analysis["original_date"],
                 analysis["updated_date"], analysis["original_price"], analysis["updated_price"],
                 analysis["severity"], status, analysis["details"]),
            ).lastrowid
            new_disruption_ids.append(disruption_id)

            if analysis["needs_recovery"]:
                conn.execute(
                    "UPDATE purchase_orders SET status = 'AT_RISK', updated_at = ? WHERE id = ?", (now, order["id"])
                )
            logger.warning("Disruption %s detected: %s", disruption_id, analysis["details"])

    return new_disruption_ids


def _apply_supplier_change(conn, commitment, analysis: dict, now: str) -> None:
    """Write the supplier's new promise into the commitment and supplier inventory."""
    kind = analysis["disruption_type"]
    lost_units = analysis["original_qty"] - analysis["updated_qty"]

    if kind in ("CANCELLED", "QUANTITY_REDUCED"):
        new_status = "CANCELLED" if kind == "CANCELLED" else "REDUCED"
        conn.execute(
            "UPDATE supplier_commitments SET committed_qty = ?, status = ?, updated_at = ? WHERE id = ?",
            (analysis["updated_qty"], new_status, now, commitment["id"]),
        )
        # The supplier no longer has these units: release the reservation and remove the stock.
        conn.execute(
            "UPDATE supplier_inventory SET reserved_qty = MAX(reserved_qty - ?, 0), stock_qty = MAX(stock_qty - ?, 0) "
            "WHERE supplier_id = ? AND part_number = ?",
            (lost_units, lost_units, commitment["supplier_id"], commitment["part_number"]),
        )
    elif kind == "DELAYED":
        conn.execute(
            "UPDATE supplier_commitments SET expected_delivery_date = ?, status = 'DELAYED', updated_at = ? WHERE id = ?",
            (analysis["updated_date"], now, commitment["id"]),
        )
    elif kind == "PRICE_INCREASE":
        conn.execute(
            "UPDATE supplier_commitments SET unit_price = ?, status = 'PRICE_CHANGED', updated_at = ? WHERE id = ?",
            (analysis["updated_price"], now, commitment["id"]),
        )
        conn.execute(
            "UPDATE supplier_inventory SET unit_price = ? WHERE supplier_id = ? AND part_number = ?",
            (analysis["updated_price"], commitment["supplier_id"], commitment["part_number"]),
        )


def list_open_disruptions() -> list[dict]:
    return repository.list_disruptions(OPEN_DISRUPTION_STATUSES)

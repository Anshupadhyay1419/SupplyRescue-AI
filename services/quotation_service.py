"""
Quotation simulation (RFQs and supplier responses).

IMPORTANT: no email or message is ever sent to a real company.
"Sending" an RFQ means inserting a row with status PENDING.
"Receiving" a quote means the simulation engine answers on the supplier's
behalf using fixed rules and the supplier's database records:

    rfq_behavior = 'QUOTE'        -> quotes its free stock, catalogue price and lead time
    rfq_behavior = 'DECLINE'      -> declines the RFQ
    rfq_behavior = 'NO_RESPONSE'  -> never answers (RFQ stays PENDING)

The LLM never writes quotations - they only come from these rules.
"""

from datetime import date, datetime, timedelta

from database import repository
from database.connection import transaction
from utils.logger import get_logger

logger = get_logger(__name__)

QUOTE_VALIDITY_DAYS = 7


def create_rfqs(request_id: int, supplier_ids: list[int], requested_qty: int) -> list[int]:
    """
    Create PENDING RFQs for the given suppliers.

    An RFQ for the same request + supplier is never duplicated
    (INSERT OR IGNORE + UNIQUE constraint). Returns the ids of new RFQs.
    """
    request = repository.get_request(request_id)
    if request is None:
        raise ValueError(f"Procurement request {request_id} does not exist.")
    if requested_qty <= 0:
        raise ValueError("Requested quantity for an RFQ must be positive.")

    new_ids = []
    with transaction() as conn:
        for supplier_id in supplier_ids:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO rfqs (request_id, supplier_id, part_number, requested_qty) VALUES (?, ?, ?, ?)",
                (request_id, supplier_id, request["part_number"], requested_qty),
            )
            if cursor.rowcount == 1:
                new_ids.append(cursor.lastrowid)
    logger.info("Request %s: created %d new RFQs.", request_id, len(new_ids))
    return new_ids


def simulate_supplier_responses(request_id: int, today: date | None = None) -> dict:
    """
    Let the simulated suppliers answer every PENDING RFQ of a request.

    Returns counts: {"received": n, "declined": n, "pending": n}.
    """
    today = today or date.today()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    counts = {"received": 0, "declined": 0, "pending": 0}

    with transaction() as conn:
        pending_rfqs = conn.execute(
            """
            SELECT r.id, r.supplier_id, r.part_number, r.requested_qty, s.rfq_behavior, s.quote_note
            FROM rfqs r JOIN suppliers s ON s.id = r.supplier_id
            WHERE r.request_id = ? AND r.status = 'PENDING'
            """,
            (request_id,),
        ).fetchall()

        for rfq in pending_rfqs:
            if rfq["rfq_behavior"] == "NO_RESPONSE":
                counts["pending"] += 1
                continue

            offer = conn.execute(
                "SELECT stock_qty - reserved_qty AS free_qty, unit_price, lead_time_days "
                "FROM supplier_inventory WHERE supplier_id = ? AND part_number = ?",
                (rfq["supplier_id"], rfq["part_number"]),
            ).fetchone()

            if rfq["rfq_behavior"] == "DECLINE" or offer is None or offer["free_qty"] <= 0:
                conn.execute(
                    "UPDATE rfqs SET status = 'DECLINED', responded_at = ? WHERE id = ?", (now, rfq["id"])
                )
                counts["declined"] += 1
                continue

            # The supplier offers what it has free, up to the requested quantity.
            offered_qty = min(offer["free_qty"], rfq["requested_qty"])
            conn.execute(
                "INSERT INTO quotations (rfq_id, supplier_id, part_number, offered_qty, unit_price, lead_time_days, "
                "valid_until, supplier_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (rfq["id"], rfq["supplier_id"], rfq["part_number"], offered_qty, offer["unit_price"],
                 offer["lead_time_days"], (today + timedelta(days=QUOTE_VALIDITY_DAYS)).isoformat(),
                 rfq["quote_note"]),
            )
            conn.execute("UPDATE rfqs SET status = 'RECEIVED', responded_at = ? WHERE id = ?", (now, rfq["id"]))
            counts["received"] += 1

    logger.info("Request %s: simulated supplier responses %s", request_id, counts)
    return counts


def summarize_rfq_status(request_id: int) -> dict:
    """Count RFQs of a request by lifecycle state."""
    summary = {"PENDING": 0, "RECEIVED": 0, "DECLINED": 0}
    for rfq in repository.list_rfqs(request_id):
        summary[rfq["status"]] += 1
    return summary

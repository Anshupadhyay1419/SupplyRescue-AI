"""
Simulation engine for the Simulation Lab page.

We have no real supplier systems, so these functions play the role of the
suppliers: each one writes a supplier update signal into SQLite and then runs
the disruption monitor, exactly as a real integration would.
All values are deterministic (no randomness) so the demo is repeatable.
"""

from datetime import timedelta

from database import repository
from database.seed import reset_database
from services import disruption_service
from utils.logger import get_logger
from utils.validators import parse_date

logger = get_logger(__name__)

SCENARIO_GUIDE = [
    ("S1", "Normal procurement", "Run the agent on the CAP-220UF request: one supplier plus warehouse stock covers it."),
    ("S2", "Partial supplier availability", "Run the agent on SEN-T200: no single supplier has 800 units."),
    ("S3", "Pre-delivery disruption", "Approve an order, then 'Simulate Partial Supply' (e.g. 600 -> 350)."),
    ("S4", "Supplier cancellation", "Approve an order, then 'Simulate Supplier Cancellation'."),
    ("S5", "Budget constraint", "Run the agent on MEM-DDR4-8G: cheapest plan is above the ₹200,000 budget."),
    ("S6", "Delivery deadline constraint", "Run the agent on RES-10K: the cheapest supplier needs 14 days."),
    ("S7", "No feasible solution", "Run the agent on CHIP-AI900: approved suppliers are too slow."),
    ("S8", "Multiple supplier allocation", "Run the agent on MCU-X100: warehouse + two suppliers are combined."),
]


def _get_active_commitment(commitment_id: int) -> dict:
    commitment = repository.get_commitment(commitment_id)
    if commitment is None:
        raise ValueError(f"Commitment {commitment_id} not found.")
    if commitment["status"] in ("CANCELLED", "REPLACED", "DELIVERED") or commitment["committed_qty"] == 0:
        raise ValueError(f"Commitment {commitment_id} is no longer active.")
    return commitment


def simulate_partial_supply(commitment_id: int, new_quantity: int) -> list[int]:
    """Supplier reduces its confirmed quantity. Returns new disruption ids."""
    commitment = _get_active_commitment(commitment_id)
    if not 0 <= new_quantity < commitment["committed_qty"]:
        raise ValueError(f"New quantity must be between 0 and {commitment['committed_qty'] - 1}.")
    disruption_service.record_supplier_update(
        commitment_id, "QUANTITY_CHANGE", new_quantity=new_quantity,
        message=f"Due to a production issue we can only confirm {new_quantity} units.",
    )
    return disruption_service.detect_supply_disruptions()


def simulate_supplier_cancellation(commitment_id: int) -> list[int]:
    """Supplier cancels its whole commitment."""
    _get_active_commitment(commitment_id)
    disruption_service.record_supplier_update(
        commitment_id, "CANCELLATION", new_quantity=0, message="We regret that we must cancel this order.",
    )
    return disruption_service.detect_supply_disruptions()


def simulate_delivery_delay(commitment_id: int, delay_days: int) -> list[int]:
    """Supplier pushes the delivery date back by `delay_days`."""
    commitment = _get_active_commitment(commitment_id)
    if delay_days <= 0:
        raise ValueError("Delay must be at least one day.")
    new_date = parse_date(commitment["expected_delivery_date"]) + timedelta(days=delay_days)
    disruption_service.record_supplier_update(
        commitment_id, "DATE_CHANGE", new_date=new_date.isoformat(),
        message=f"Shipment delayed by {delay_days} days due to logistics problems.",
    )
    return disruption_service.detect_supply_disruptions()


def simulate_price_increase(commitment_id: int, percent: float) -> list[int]:
    """Supplier raises the unit price of an existing commitment by `percent`."""
    commitment = _get_active_commitment(commitment_id)
    if percent <= 0:
        raise ValueError("Price increase must be positive.")
    new_price = round(commitment["unit_price"] * (1 + percent / 100), 2)
    disruption_service.record_supplier_update(
        commitment_id, "PRICE_CHANGE", new_unit_price=new_price,
        message=f"Raw material costs forced a {percent:.0f}% price revision.",
    )
    return disruption_service.detect_supply_disruptions()


def reset_demo() -> None:
    """Wipe all data and reload the deterministic demo dataset."""
    reset_database()
    logger.info("Demo data reset.")

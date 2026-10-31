"""Disruption detection from supplier update signals."""

from datetime import date, timedelta

from database import repository
from database.connection import fetch_one
from services import disruption_service, simulation_service
from tests.conftest import approve_main_scenario, commitment_of


def test_quantity_reduction_is_detected_with_correct_shortage():
    approve_main_scenario()
    apex = commitment_of("Apex Microsystems")
    [disruption_id] = simulation_service.simulate_partial_supply(apex["id"], 350)
    disruption = repository.get_disruption(disruption_id)
    assert (disruption["original_qty"], disruption["updated_qty"], disruption["shortage_qty"]) == (600, 350, 250)
    assert disruption["disruption_type"] == "QUANTITY_REDUCED" and disruption["status"] == "OPEN"
    assert repository.get_order(disruption["order_id"])["status"] == "AT_RISK"
    commitment = repository.get_commitment(apex["id"])
    assert commitment["committed_qty"] == 350 and commitment["original_qty"] == 600
    supplier_row = fetch_one("SELECT reserved_qty FROM supplier_inventory WHERE supplier_id = ? AND part_number = ?",
                             (apex["supplier_id"], "MCU-X100"))
    assert supplier_row["reserved_qty"] == 350


def test_signal_is_processed_only_once():
    approve_main_scenario()
    apex = commitment_of("Apex Microsystems")
    simulation_service.simulate_partial_supply(apex["id"], 350)
    assert disruption_service.detect_supply_disruptions() == []  # nothing new on second pass
    assert len(repository.list_disruptions()) == 1


def test_cancellation_is_detected():
    approve_main_scenario()
    delta = commitment_of("Delta Circuitworks")
    [disruption_id] = simulation_service.simulate_supplier_cancellation(delta["id"])
    disruption = repository.get_disruption(disruption_id)
    assert disruption["disruption_type"] == "CANCELLED" and disruption["shortage_qty"] == 300
    assert disruption["severity"] == "HIGH"
    assert repository.get_commitment(delta["id"])["status"] == "CANCELLED"


def test_delay_within_deadline_needs_no_action():
    approve_main_scenario()
    apex = commitment_of("Apex Microsystems")  # due in 3 days, deadline in 7
    [disruption_id] = simulation_service.simulate_delivery_delay(apex["id"], 2)
    disruption = repository.get_disruption(disruption_id)
    assert disruption["status"] == "NO_ACTION_NEEDED" and disruption["shortage_qty"] == 0
    assert repository.get_order(disruption["order_id"])["status"] == "CONFIRMED"


def test_delay_past_deadline_is_a_shortage():
    approve_main_scenario()
    apex = commitment_of("Apex Microsystems")
    [disruption_id] = simulation_service.simulate_delivery_delay(apex["id"], 10)
    disruption = repository.get_disruption(disruption_id)
    assert disruption["status"] == "OPEN" and disruption["shortage_qty"] == 600


def test_quantity_increase_is_not_a_disruption():
    commitment = {"committed_qty": 600, "expected_delivery_date": date.today().isoformat(), "unit_price": 100}
    update = {"update_type": "QUANTITY_CHANGE", "new_quantity": 700}
    assert disruption_service.analyze_commitment_change(
        commitment, update, 1000, date.today() + timedelta(days=7), 90000, 150000) is None


def test_price_increase_detection():
    approve_main_scenario()
    apex = commitment_of("Apex Microsystems")
    [disruption_id] = simulation_service.simulate_price_increase(apex["id"], 20)
    disruption = repository.get_disruption(disruption_id)
    assert disruption["disruption_type"] == "PRICE_INCREASE" and disruption["updated_price"] == 120
    assert disruption["shortage_qty"] == 0

"""Warehouse inventory, supplier search and exact part-number matching."""

from datetime import date, timedelta

from services import procurement_service
from services.planning_service import calculate_shortage


def test_reserved_warehouse_stock_is_not_usable():
    stock = procurement_service.get_warehouse_stock("MCU-X100")
    assert stock == {"part_number": "MCU-X100", "on_hand": 250, "reserved": 150, "free": 100,
                     "location": stock["location"]}


def test_remaining_procurement_need():
    free = procurement_service.get_warehouse_stock("MCU-X100")["free"]
    assert calculate_shortage(1000, free) == 900


def test_shortage_is_never_negative():
    assert calculate_shortage(100, 250) == 0
    assert calculate_shortage(600, 350) == 250


def test_unknown_part_has_zero_stock():
    assert procurement_service.get_warehouse_stock("NOPE-1")["free"] == 0


def test_supplier_search_returns_only_approved_suppliers():
    result = procurement_service.search_suppliers("MCU-X100")
    names = {s["supplier_name"] for s in result["approved_suppliers"]}
    assert names == {"Apex Microsystems", "BlueRiver Components", "Cobalt Semicon",
                     "Delta Circuitworks", "Granite Chipworks"}
    assert "EastWind Electronics Trading" not in names
    assert result["unapproved_count"] == 1


def test_exact_part_number_matching():
    """MCU-X100B (Ferrotek) must never be returned for MCU-X100."""
    result = procurement_service.search_suppliers("mcu-x100 ")  # normalised to MCU-X100
    assert all(s["supplier_name"] != "Ferrotek Devices" for s in result["approved_suppliers"])
    assert {"supplier_name": "Ferrotek Devices", "part_number": "MCU-X100B"} in result["incompatible_similar_parts"]


def test_search_flags_suppliers_that_cannot_meet_deadline():
    result = procurement_service.search_suppliers("MCU-X100", date.today() + timedelta(days=7))
    by_name = {s["supplier_name"]: s for s in result["approved_suppliers"]}
    assert by_name["Cobalt Semicon"]["can_meet_deadline"] is False
    assert by_name["Apex Microsystems"]["can_meet_deadline"] is True

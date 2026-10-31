"""Database initialisation, seed data and constraints."""

import sqlite3

import pytest

from database import repository
from database.connection import fetch_one, transaction
from database.schema import ALL_TABLES


def test_all_tables_created():
    for table in ALL_TABLES:
        assert fetch_one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)), table


def test_seed_meets_minimum_data_requirements():
    assert fetch_one("SELECT COUNT(*) AS n FROM suppliers")["n"] >= 20
    assert fetch_one("SELECT COUNT(*) AS n FROM components")["n"] >= 8
    assert len(repository.list_requests("NEW")) >= 6
    assert len(repository.list_orders()) >= 2
    assert len(repository.list_active_commitments()) >= 2


def test_seed_reservations_match_orders():
    """The 150 reserved MCU-X100 warehouse units belong to a real seeded order."""
    warehouse = repository.get_warehouse_row("MCU-X100")
    assert warehouse["quantity_reserved"] == 150
    reserved_by_orders = fetch_one(
        "SELECT SUM(oi.quantity) AS n FROM order_items oi JOIN purchase_orders o ON o.id = oi.order_id "
        "WHERE oi.source_type='WAREHOUSE' AND oi.part_number='MCU-X100' AND o.status IN ('CONFIRMED','AT_RISK')"
    )["n"]
    assert reserved_by_orders == 150


def test_negative_inventory_is_rejected():
    with pytest.raises(sqlite3.IntegrityError):
        with transaction() as conn:
            conn.execute("UPDATE warehouse_inventory SET quantity_on_hand = -5 WHERE part_number='MCU-X100'")


def test_reserving_more_than_stock_is_rejected():
    with pytest.raises(sqlite3.IntegrityError):
        with transaction() as conn:
            conn.execute("UPDATE supplier_inventory SET reserved_qty = stock_qty + 1")


def test_invalid_status_is_rejected():
    with pytest.raises(sqlite3.IntegrityError):
        with transaction() as conn:
            conn.execute("UPDATE procurement_requests SET status = 'MAGIC' WHERE id = 1")


def test_failed_transaction_rolls_back_everything():
    before = repository.get_warehouse_row("CAP-220UF")["quantity_on_hand"]
    with pytest.raises(sqlite3.IntegrityError):
        with transaction() as conn:
            conn.execute("UPDATE warehouse_inventory SET quantity_on_hand = 1 WHERE part_number='CAP-220UF'")
            conn.execute("UPDATE warehouse_inventory SET quantity_on_hand = -1 WHERE part_number='RES-10K'")
    assert repository.get_warehouse_row("CAP-220UF")["quantity_on_hand"] == before

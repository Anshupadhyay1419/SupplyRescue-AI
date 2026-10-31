"""Page 5 - Active Orders: simulated purchase orders and their supplier commitments."""

import streamlit as st

from database import repository
from ui.components import kpi_card, page_header, show_table, status_badge
from utils.validators import format_inr


def render() -> None:
    page_header("Active Orders", "Simulated purchase orders created after human approval.")
    orders = repository.list_orders()
    if not orders:
        st.info("No orders yet. Approve a plan on the New Procurement page.")
        return

    show_table(orders, {"id": "Order", "request_id": "Request", "part_number": "Part", "quantity": "Quantity",
                        "status": "Status", "total_cost": "Total cost (₹)", "required_by": "Required by",
                        "created_at": "Created", "updated_at": "Updated"})

    order_id = st.selectbox("Open an order", [order["id"] for order in orders],
                            format_func=lambda oid: _order_label(oid, orders))
    order = repository.get_order(order_id)
    items = repository.get_order_items(order_id)

    covered = 0
    for item in items:
        if item["source_type"] == "WAREHOUSE":
            covered += item["quantity"]
        elif item["commitment_status"] not in ("CANCELLED", "REPLACED"):
            covered += item["committed_qty"] or 0

    st.markdown(f"### Order #{order['id']} &nbsp; {status_badge(order['status'])}", unsafe_allow_html=True)
    columns = st.columns(4)
    with columns[0]:
        kpi_card("Part", order["part_number"], order["component_name"])
    with columns[1]:
        kpi_card("Covered", f"{covered:,} / {order['quantity']:,}", "warehouse + active commitments")
    with columns[2]:
        kpi_card("Total cost", format_inr(order["total_cost"]), f"Budget {format_inr(order['max_budget'])}")
    with columns[3]:
        kpi_card("Required by", order["required_by"])

    st.markdown("**Order lines and supplier commitments**")
    show_table(items, {"supplier_name": "Source", "added_by": "Added by", "quantity": "Ordered qty",
                       "original_qty": "Original commitment", "committed_qty": "Current commitment",
                       "committed_unit_price": "Unit price (₹)", "expected_delivery_date": "Expected delivery",
                       "commitment_status": "Commitment status"})

    related = [d for d in repository.list_disruptions() if d["order_id"] == order_id]
    if related:
        st.markdown("**Disruptions on this order**")
        show_table(related, {"id": "Disruption", "disruption_type": "Type", "supplier_name": "Supplier",
                             "shortage_qty": "Shortage", "severity": "Severity", "status": "Status",
                             "detected_at": "Detected"})


def _order_label(order_id: int, orders: list[dict]) -> str:
    order = next(o for o in orders if o["id"] == order_id)
    return f"Order #{order_id} · {order['quantity']:,} × {order['part_number']} · {order['status']}"

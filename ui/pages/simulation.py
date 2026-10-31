"""Page 9 - Simulation Lab: create supplier events that change real database state."""

import streamlit as st

from database import repository
from services import simulation_service
from ui.components import page_header, show_table


def render() -> None:
    page_header("Simulation Lab", "Play the role of a supplier. Every button writes a supplier update into SQLite "
                                  "and runs the commitment monitor.")

    commitments = repository.list_active_commitments()
    if not commitments:
        st.info("No active supplier commitments. Approve a procurement plan first (New Procurement page).")
    else:
        commitment_id = st.selectbox(
            "Supplier commitment to disrupt", [c["id"] for c in commitments],
            format_func=lambda cid: _label(next(c for c in commitments if c["id"] == cid)),
        )
        commitment = next(c for c in commitments if c["id"] == commitment_id)

        col1, col2 = st.columns(2)
        with col1:
            with st.container(border=True):
                st.markdown("**Partial supply** - supplier lowers its confirmed quantity")
                default = min(350, commitment["committed_qty"] - 1) if commitment["committed_qty"] > 1 else 0
                new_qty = st.number_input("New confirmed quantity", min_value=0,
                                          max_value=max(commitment["committed_qty"] - 1, 0), value=max(default, 0))
                if st.button("Simulate Partial Supply", type="primary", width="stretch"):
                    _run(simulation_service.simulate_partial_supply, commitment_id, int(new_qty))
            with st.container(border=True):
                st.markdown("**Delivery delay** - supplier moves the delivery date")
                delay = st.number_input("Delay (days)", min_value=1, max_value=60, value=5)
                if st.button("Simulate Delivery Delay", width="stretch"):
                    _run(simulation_service.simulate_delivery_delay, commitment_id, int(delay))
        with col2:
            with st.container(border=True):
                st.markdown("**Cancellation** - supplier cancels the whole commitment")
                st.caption(f"Cancels {commitment['committed_qty']:,} units from {commitment['supplier_name']}.")
                if st.button("Simulate Supplier Cancellation", width="stretch"):
                    _run(simulation_service.simulate_supplier_cancellation, commitment_id)
            with st.container(border=True):
                st.markdown("**Price increase** - supplier revises the unit price")
                percent = st.number_input("Increase (%)", min_value=1.0, max_value=300.0, value=20.0, step=5.0)
                if st.button("Simulate Price Increase", width="stretch"):
                    _run(simulation_service.simulate_price_increase, commitment_id, float(percent))

    st.divider()
    st.markdown("**Scenario guide**")
    show_table([{"Scenario": code, "Name": name, "How to demonstrate": how}
                for code, name, how in simulation_service.SCENARIO_GUIDE])

    st.markdown("**Supplier update signals received**")
    show_table(repository.list_supplier_updates(), {
        "id": "ID", "commitment_id": "Commitment", "supplier_name": "Supplier", "update_type": "Type",
        "new_quantity": "New qty", "new_date": "New date", "new_unit_price": "New price", "message": "Message",
        "processed": "Processed", "received_at": "Received"})

    st.divider()
    with st.container(border=True):
        st.markdown("**Reset Demo** - deletes ALL data (orders, plans, agent logs) and reloads the seed data.")
        confirm = st.checkbox("I understand that all demo data will be replaced")
        if st.button("Reset Demo", disabled=not confirm):
            simulation_service.reset_demo()
            st.session_state.clear()
            st.success("Demo data reset.")
            st.rerun()


def _label(commitment: dict) -> str:
    return (f"#{commitment['id']} · {commitment['supplier_name']} · {commitment['committed_qty']:,} × "
            f"{commitment['part_number']} · order #{commitment['order_id']} · due {commitment['expected_delivery_date']}"
            f" · {commitment['status']}")


def _run(function, *args) -> None:
    try:
        new_disruptions = function(*args)
    except ValueError as error:
        st.error(str(error))
        return
    if not new_disruptions:
        st.info("Supplier update recorded. The monitor found no risk.")
        return
    for disruption_id in new_disruptions:
        disruption = repository.get_disruption(disruption_id)
        if disruption["status"] == "NO_ACTION_NEEDED":
            st.info(f"Change detected but no action needed: {disruption['details']}")
        else:
            st.error(f"**Supply Risk Detected** (disruption #{disruption_id}) - {disruption['details']} "
                     "Go to the Disruption Center and click 'Launch AI Recovery'.")

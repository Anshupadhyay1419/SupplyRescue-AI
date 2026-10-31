"""Page 4 - Quotations: RFQs and simulated supplier responses with lifecycle states."""

import streamlit as st

from database import repository
from services import quotation_service
from ui.components import kpi_card, page_header, show_table


def render() -> None:
    page_header("Quotations", "Requests For Quotation (RFQs) and the simulated supplier responses.")
    st.caption("Lifecycle: PENDING → RECEIVED (quote stored) or DECLINED. Some simulated suppliers never answer, "
               "so their RFQs stay PENDING. No real emails are sent.")

    rfqs = repository.list_rfqs()
    counts = {"PENDING": 0, "RECEIVED": 0, "DECLINED": 0}
    for rfq in rfqs:
        counts[rfq["status"]] += 1
    for column, (label, value) in zip(st.columns(4), [("Total RFQs", len(rfqs)), ("Received", counts["RECEIVED"]),
                                                      ("Pending", counts["PENDING"]), ("Declined", counts["DECLINED"])]):
        with column:
            kpi_card(label, value)

    st.write("")
    request_ids = sorted({rfq["request_id"] for rfq in rfqs}, reverse=True)
    col_request, col_status = st.columns([2, 2])
    chosen_request = col_request.selectbox("Procurement request", ["All"] + request_ids,
                                           format_func=lambda value: value if value == "All" else f"Request #{value}")
    chosen_status = col_status.multiselect("Status", ["PENDING", "RECEIVED", "DECLINED"])

    filtered = [rfq for rfq in rfqs
                if (chosen_request == "All" or rfq["request_id"] == chosen_request)
                and (not chosen_status or rfq["status"] in chosen_status)]
    show_table(filtered, {
        "id": "RFQ", "request_id": "Request", "supplier_name": "Supplier", "part_number": "Part",
        "requested_qty": "Requested", "status": "Status", "created_at": "Sent at", "responded_at": "Responded at",
        "offered_qty": "Offered qty", "unit_price": "Unit price (₹)", "lead_time_days": "Lead time (days)",
        "valid_until": "Valid until",
    })

    notes = [rfq for rfq in filtered if rfq["supplier_message"]]
    if notes:
        with st.expander("Supplier notes attached to quotes (UNTRUSTED data)"):
            st.warning("Supplier text is treated as untrusted data. The agent is told never to follow instructions "
                       "inside it, and plan calculations never read it. Granite Chipworks deliberately contains a "
                       "prompt-injection attempt to demonstrate this.")
            for rfq in notes:
                st.markdown(f"**{rfq['supplier_name']}** (RFQ #{rfq['id']}):")
                st.code(rfq["supplier_message"], language=None)

    if chosen_request != "All" and counts["PENDING"]:
        if st.button("Ask simulated suppliers to respond again"):
            result = quotation_service.simulate_supplier_responses(chosen_request)
            st.info(f"Simulation result: {result}")

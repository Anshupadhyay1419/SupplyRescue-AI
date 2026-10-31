"""Page 6 - Disruption Center: detected supply risks and the 'Launch AI Recovery' button."""

import streamlit as st

from agents.graph import run_recovery_agent
from database import repository
from services import disruption_service
from ui.components import STATUS_COLORS, page_header, run_agent_with_progress, show_table, status_badge


def render() -> None:
    page_header("Disruption Center", "Pre-delivery supply risks detected from supplier commitment updates.")

    pending_signals = [u for u in repository.list_supplier_updates() if not u["processed"]]
    if pending_signals:
        st.warning(f"{len(pending_signals)} supplier update(s) not yet checked by the commitment monitor.")
    if st.button("Run commitment monitor now"):
        new_ids = disruption_service.detect_supply_disruptions()
        st.success(f"Monitor finished: {len(new_ids)} new disruption(s) detected.")

    open_disruptions = disruption_service.list_open_disruptions()
    if not open_disruptions:
        st.success("No active supply risks. Use the Simulation Lab to simulate a supplier event.")
    for disruption in open_disruptions:
        _render_disruption(disruption)

    st.divider()
    st.markdown("**All disruptions (history)**")
    show_table(repository.list_disruptions(), {
        "id": "ID", "order_id": "Order", "supplier_name": "Supplier", "part_number": "Part",
        "disruption_type": "Type", "original_qty": "Original qty", "updated_qty": "Updated qty",
        "shortage_qty": "Shortage", "severity": "Severity", "status": "Status", "detected_at": "Detected",
    })


def _render_disruption(disruption: dict) -> None:
    color = STATUS_COLORS["critical"] if disruption["severity"] == "HIGH" else STATUS_COLORS["serious"]
    st.markdown(
        f"<div class='sr-risk' style='border-color:{color}'><b style='color:{color}'>▲ Supply Risk Detected</b>"
        f" &nbsp; {status_badge(disruption['status'])} {status_badge(disruption['severity'])}<br>"
        f"Disruption #{disruption['id']} · {disruption['disruption_type'].replace('_', ' ').title()} · "
        f"{disruption['supplier_name']} · {disruption['part_number']} · affected order #{disruption['order_id']}<br>"
        f"<span style='opacity:.8'>{disruption['details']}</span></div>",
        unsafe_allow_html=True,
    )
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Original commitment", f"{disruption['original_qty']:,}")
    col2.metric("Updated commitment", f"{disruption['updated_qty']:,}")
    col3.metric("Potential shortage", f"{disruption['shortage_qty']:,}")
    if disruption["disruption_type"] == "DELAYED":
        col4.metric("New delivery date", disruption["updated_date"], f"was {disruption['original_date']}",
                    delta_color="off")
    elif disruption["disruption_type"] == "PRICE_INCREASE":
        col4.metric("New unit price", f"₹{disruption['updated_price']:,.2f}", f"was ₹{disruption['original_price']:,.2f}",
                    delta_color="off")
    else:
        col4.metric("Affected order", f"#{disruption['order_id']}")

    if disruption["status"] == "RECOVERY_PROPOSED":
        st.info("A recovery plan is already waiting for approval on the Recovery Plans page. "
                "Launching the agent again replaces it with a new proposal.")

    with st.expander("Optional: relax constraints for this recovery (only if a previous plan was rejected "
                     "or infeasible)"):
        extra_budget = st.number_input("Extra budget allowed (₹)", min_value=0.0, value=0.0, step=5000.0,
                                       key=f"extra_{disruption['id']}")
        extension = st.number_input("Deadline extension allowed (days)", min_value=0, max_value=60, value=0,
                                    key=f"ext_{disruption['id']}")
        st.caption("These only widen what the agent may propose. If you approve a plan that uses them, the "
                   "order's budget and/or required date are updated to match.")

    if st.button("Launch AI Recovery", type="primary", key=f"recover_{disruption['id']}"):
        result = run_agent_with_progress(run_recovery_agent, disruption["id"], extra_budget=extra_budget,
                                         deadline_extension_days=int(extension))
        if result and result.get("plan_id"):
            plan = repository.get_plan(result["plan_id"])
            if plan["feasible"]:
                st.success(f"Recovery plan #{plan['id']} generated. Review and approve it on the Recovery Plans page.")
            else:
                st.error("No feasible recovery plan found under current constraints. Details are on the "
                         "Recovery Plans page.")
    st.write("")

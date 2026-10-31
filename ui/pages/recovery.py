"""Page 8 - Recovery Plans: before/after view of every recovery and its approval."""

import streamlit as st

from database import repository
from services import recovery_service
from ui.components import page_header, render_plan_card, show_table, status_badge


def render() -> None:
    page_header("Recovery Plans", "How the agent proposes to recover from each disruption - approval required.")

    plans = repository.list_plans("RECOVERY")
    if not plans:
        st.info("No recovery plans yet. Simulate a disruption in the Simulation Lab, then click "
                "'Launch AI Recovery' in the Disruption Center.")
        return

    waiting = [plan for plan in plans if plan["status"] == "PROPOSED"]
    st.caption(f"{len(plans)} recovery plan(s), {len(waiting)} waiting for approval.")
    plan_id = st.selectbox("Recovery plan", [plan["id"] for plan in plans],
                           format_func=lambda pid: _label(next(p for p in plans if p["id"] == pid)))
    plan = repository.get_plan(plan_id)
    disruption = repository.get_disruption(plan["disruption_id"])

    st.markdown(f"### Disruption #{disruption['id']} &nbsp; {status_badge(disruption['status'])}",
                unsafe_allow_html=True)
    st.write(disruption["details"])

    before, after = st.columns(2)
    with before:
        with st.container(border=True):
            st.markdown("**Original plan / current order lines**")
            items = repository.get_order_items(disruption["order_id"])
            show_table(items, {"supplier_name": "Source", "quantity": "Ordered", "original_qty": "Original commitment",
                               "committed_qty": "Updated commitment", "commitment_status": "Status",
                               "expected_delivery_date": "Delivery"})
            st.caption(f"Detected shortage: {disruption['shortage_qty']:,} units · "
                       f"original {disruption['original_qty']:,} → updated {disruption['updated_qty']:,}")
    with after:
        with st.container(border=True):
            st.markdown("**Alternative suppliers considered**")
            try:
                context = recovery_service.get_recovery_context(disruption["id"])
                options = recovery_service.search_recovery_options(context)
                show_table(options, {"supplier_name": "Source", "available_qty": "Free qty",
                                     "unit_price": "Unit price (₹)", "lead_time_days": "Lead time (days)",
                                     "reliability": "Reliability", "approved": "Approved"})
                st.caption("Current data. Eligibility (approval, reliability ≥ minimum, deadline) is decided by "
                           "the deterministic planner.")
            except Exception as error:
                st.warning(f"Could not load alternatives: {error}")

    if plan["expected_delivery"] and plan["required_by"]:
        impact = "on time" if plan["expected_delivery"] <= plan["required_by"] else "LATE"
        st.caption(f"Delivery impact: replacement units expected {plan['expected_delivery']} vs required "
                   f"{plan['required_by']} ({impact}).")
    with st.container(border=True):
        render_plan_card(plan, key_prefix="recovery")

    st.markdown("**All recovery plans**")
    show_table(plans, {"id": "Plan", "disruption_id": "Disruption", "status": "Status", "total_cost": "Cost (₹)",
                       "cost_difference": "Cost difference (₹)", "expected_delivery": "Delivery",
                       "created_at": "Created", "decided_at": "Decided"})


def _label(plan: dict) -> str:
    return f"Plan #{plan['id']} · disruption #{plan['disruption_id']} · {plan['status']}"

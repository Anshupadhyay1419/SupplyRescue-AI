"""Page 2 - New Procurement: create a requirement, run the agent, approve or reject."""

from datetime import date, timedelta

import streamlit as st

from agents.graph import run_procurement_agent
from database import repository
from services import procurement_service
from ui.components import (
    page_header,
    render_activity_timeline,
    render_plan_card,
    run_agent_with_progress,
    show_table,
    status_badge,
)
from utils.validators import format_inr


def render() -> None:
    page_header("New Procurement", "Describe what TechNova needs - the AI agent investigates and recommends a plan.")

    tab_new, tab_queue, tab_plans = st.tabs(["Create requirement", "Requests waiting for the agent",
                                             "Plans & approvals"])
    with tab_new:
        _render_new_requirement_form()
    with tab_queue:
        _render_request_queue()
    with tab_plans:
        _render_plans()

    # Result of the last run started from this page (kept across reruns so the user can approve).
    last_run = st.session_state.get("last_procurement_run")
    if last_run:
        st.divider()
        _render_run_result(last_run)


def _render_new_requirement_form() -> None:
    components = repository.list_components()
    part_numbers = [component["part_number"] for component in components]
    with st.form("new_requirement"):
        col1, col2 = st.columns(2)
        component_name = col1.text_input("Component", value="Microchip")
        part_number = col2.selectbox("Part number", part_numbers, index=part_numbers.index("MCU-X100"))
        col3, col4, col5 = st.columns(3)
        quantity = col3.number_input("Quantity", min_value=1, max_value=1_000_000, value=1000, step=50)
        required_by = col4.date_input("Required delivery date", value=date.today() + timedelta(days=7),
                                      min_value=date.today() + timedelta(days=1))
        max_budget = col5.number_input("Maximum budget (₹)", min_value=1.0, value=150000.0, step=5000.0)
        quality = st.text_input("Quality requirements", value="Industrial grade, RoHS compliant")
        submitted = st.form_submit_button("Start AI Procurement", type="primary")

    if submitted:
        requirement = {"component_name": component_name, "part_number": part_number, "quantity": int(quantity),
                       "required_by": required_by, "max_budget": float(max_budget), "quality_requirements": quality}
        try:
            request_id = procurement_service.create_procurement_request(requirement)
        except ValueError as error:
            st.error(f"Invalid requirement: {error}")
            return
        st.success(f"Requirement saved as request #{request_id}. Starting the agent...")
        _start_agent(request_id)


def _render_request_queue() -> None:
    waiting = repository.list_requests("NEW") + repository.list_requests("FAILED")
    if not waiting:
        st.info("No requests are waiting. Create one in the first tab or reset the demo in the Simulation Lab.")
        return
    st.caption("Seeded scenario requests are ready for demonstration. Each is processed by the agent from live data.")
    for request in waiting:
        with st.container(border=True):
            col_info, col_button = st.columns([5, 1])
            col_info.markdown(
                f"**#{request['id']} · {request['scenario'] or 'Manual request'}** &nbsp; {status_badge(request['status'])}<br>"
                f"{request['quantity']:,} × {request['part_number']} ({request['component_name']}) · by "
                f"{request['required_by']} · budget {format_inr(request['max_budget'])}",
                unsafe_allow_html=True,
            )
            if col_button.button("Run agent", key=f"run_{request['id']}", width="stretch"):
                _start_agent(request["id"])


def _start_agent(request_id: int) -> None:
    result = run_agent_with_progress(run_procurement_agent, request_id)
    if result:
        st.session_state["last_procurement_run"] = {"request_id": request_id, **result}
        st.rerun()


def _render_run_result(run: dict) -> None:
    request = repository.get_request(run["request_id"])
    st.subheader(f"Agent result for request #{request['id']}: {request['quantity']:,} × {request['part_number']}")
    col_left, col_right = st.columns([3, 2])
    with col_right:
        with st.container(border=True, height=620):
            st.markdown("**What the agent did (live log)**")
            render_activity_timeline(run["run_id"], show_details=True)
    with col_left:
        rfqs = repository.list_rfqs(request["id"])
        if rfqs:
            st.markdown("**Quotations (simulated supplier responses)**")
            show_table(rfqs, {"supplier_name": "Supplier", "status": "RFQ status", "offered_qty": "Offered qty",
                              "unit_price": "Unit price (₹)", "lead_time_days": "Lead time (days)"})
        if run.get("plan_id"):
            render_plan_card(repository.get_plan(run["plan_id"]), key_prefix="run")
            _render_revision_form(request)
    if st.button("Clear result"):
        st.session_state.pop("last_procurement_run", None)
        st.rerun()


def _render_revision_form(request: dict) -> None:
    """After a rejection (or infeasible result), let the user change constraints and ask again."""
    if request["status"] not in ("REJECTED", "INFEASIBLE"):
        return
    with st.expander("Change constraints and generate a revised recommendation", expanded=True):
        with st.form(f"revise_{request['id']}"):
            col1, col2, col3 = st.columns(3)
            quantity = col1.number_input("Quantity", min_value=1, value=int(request["quantity"]))
            required_by = col2.date_input("Required by", value=date.fromisoformat(request["required_by"]),
                                          min_value=date.today() + timedelta(days=1))
            budget = col3.number_input("Budget (₹)", min_value=1.0, value=float(request["max_budget"]), step=5000.0)
            if st.form_submit_button("Create revised request and run agent", type="primary"):
                try:
                    new_id = procurement_service.create_revised_request(
                        request["id"], {"quantity": int(quantity), "required_by": required_by, "max_budget": float(budget)})
                except ValueError as error:
                    st.error(str(error))
                    return
                _start_agent(new_id)


def _render_plans() -> None:
    plans = repository.list_plans("PROCUREMENT")
    waiting = [plan for plan in plans if plan["status"] == "PROPOSED"]
    st.markdown(f"**{len(waiting)} plan(s) waiting for approval**")
    for plan in waiting:
        with st.container(border=True):
            request = repository.get_request(plan["request_id"])
            st.caption(f"Request #{request['id']}: {request['quantity']:,} × {request['part_number']}")
            render_plan_card(plan, key_prefix="queue")
    st.markdown("**Plan history**")
    show_table(plans, {"id": "Plan", "request_id": "Request", "status": "Status", "feasible": "Feasible",
                       "total_cost": "Cost (₹)", "expected_delivery": "Delivery", "risk_level": "Risk",
                       "created_at": "Created"})

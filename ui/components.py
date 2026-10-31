"""
Reusable dashboard pieces: styling, KPI cards, status badges, chart styling,
the plan card (with APPROVE / REJECT) and the agent activity timeline.

Pages import from here so that every page looks and behaves the same way.
"""

import html

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from database import repository
from services import procurement_service, recovery_service
from services.procurement_service import PlanDecisionError
from utils.validators import format_inr

# ---- Colours (validated reference palette; status colours are reserved for states) ----
SERIES_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
SEQUENTIAL_BLUE = "#2a78d6"
MUTED_GRAY = "#a3a29b"
STATUS_COLORS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b",
                 "info": "#2a78d6", "neutral": "#8a8983"}

# Every business status maps to one of the reserved status roles + an icon (never colour alone).
STATUS_STYLE = {
    "CONFIRMED": ("good", "●"), "APPROVED": ("good", "✔"), "RESOLVED": ("good", "✔"), "RECEIVED": ("good", "✔"),
    "SUCCESS": ("good", "✔"), "ORDERED": ("good", "✔"), "DELIVERED": ("info", "■"), "COMPLETED": ("good", "✔"),
    "PROPOSED": ("info", "◆"), "PLAN_PROPOSED": ("info", "◆"), "RECOVERY_PROPOSED": ("info", "◆"),
    "AWAITING_APPROVAL": ("info", "◆"), "PLANNING": ("info", "↻"), "RUNNING": ("info", "↻"), "NEW": ("neutral", "○"),
    "PENDING": ("warning", "…"), "INFO": ("info", "i"), "WARNING": ("warning", "!"), "MEDIUM": ("warning", "!"),
    "AT_RISK": ("serious", "▲"), "OPEN": ("serious", "▲"), "REDUCED": ("serious", "▲"), "DELAYED": ("serious", "▲"),
    "PRICE_CHANGED": ("warning", "!"), "HIGH": ("critical", "▲"), "LOW": ("good", "●"),
    "REJECTED": ("critical", "✖"), "INFEASIBLE": ("critical", "✖"), "UNRESOLVED": ("critical", "✖"),
    "CANCELLED": ("critical", "✖"), "FAILED": ("critical", "✖"), "DECLINED": ("critical", "✖"),
    "SUPERSEDED": ("neutral", "–"), "REPLACED": ("neutral", "–"), "NO_ACTION_NEEDED": ("neutral", "●"),
}

CSS = """
<style>
:root { --sr-border: rgba(128,128,128,0.25); --sr-muted: rgba(128,128,128,0.95); }
.block-container { padding-top: 2rem; max-width: 1400px; }
.sr-header h1 { font-size: 1.6rem; margin-bottom: 0; }
.sr-header p { color: var(--sr-muted); margin-top: .15rem; }
.sr-card { border: 1px solid var(--sr-border); border-radius: 10px; padding: 14px 16px; height: 100%; }
.sr-card .label { font-size: .78rem; text-transform: uppercase; letter-spacing: .04em; color: var(--sr-muted); }
.sr-card .value { font-size: 1.55rem; font-weight: 650; margin-top: 4px; }
.sr-card .hint { font-size: .78rem; color: var(--sr-muted); margin-top: 2px; }
.sr-badge { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: .75rem; font-weight: 600;
            border: 1px solid; white-space: nowrap; }
.sr-step { border-left: 3px solid var(--sr-border); padding: 4px 0 10px 14px; margin-left: 6px; }
.sr-step .title { font-weight: 600; }
.sr-step .meta { font-size: .78rem; color: var(--sr-muted); }
.sr-step code { font-size: .78rem; white-space: pre-wrap; }
.sr-risk { border: 1px solid; border-radius: 10px; padding: 12px 16px; margin: 6px 0 12px 0; }
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def page_header(title: str, subtitle: str) -> None:
    st.markdown(f"<div class='sr-header'><h1>{html.escape(title)}</h1><p>{html.escape(subtitle)}</p></div>",
                unsafe_allow_html=True)


def status_badge(status: str | None) -> str:
    """HTML badge: colour + icon + text, so meaning never relies on colour alone."""
    status = status or "-"
    role, icon = STATUS_STYLE.get(status, ("neutral", "●"))
    color = STATUS_COLORS[role]
    label = status.replace("_", " ").title()
    return (f"<span class='sr-badge' style='color:{color};border-color:{color}'>"
            f"{icon} {html.escape(label)}</span>")


def kpi_card(label: str, value, hint: str = "") -> None:
    st.markdown(
        f"<div class='sr-card'><div class='label'>{html.escape(label)}</div>"
        f"<div class='value'>{html.escape(str(value))}</div><div class='hint'>{html.escape(hint)}</div></div>",
        unsafe_allow_html=True,
    )


def style_figure(fig: go.Figure, height: int = 320) -> go.Figure:
    """Recessive axes, thin grid, transparent background (works in light and dark themes)."""
    fig.update_layout(
        height=height, margin=dict(l=10, r=10, t=10, b=10),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(size=12), legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        hoverlabel=dict(font_size=12), bargap=0.35,
    )
    fig.update_xaxes(showgrid=False, linecolor="rgba(128,128,128,0.4)", zeroline=False)
    fig.update_yaxes(gridcolor="rgba(128,128,128,0.15)", zeroline=False)
    return fig


def show_table(rows: list[dict], columns: dict | None = None, height: int | None = None) -> None:
    """Display rows as a table. `columns` maps raw keys to friendly headers (and selects them)."""
    if not rows:
        st.info("No records to show.")
        return
    frame = pd.DataFrame(rows)
    if columns:
        frame = frame[[key for key in columns if key in frame.columns]].rename(columns=columns)
    st.dataframe(frame, hide_index=True, width="stretch", height=height or "auto")


# ------------------------------------------------------------------ plan card

def render_plan_card(plan: dict, key_prefix: str, allow_decision: bool = True) -> None:
    """
    Show a plan with its numbers (from SQLite - authoritative), the AI explanation,
    and APPROVE / REJECT buttons when the plan is waiting for a human.
    """
    plan = repository.get_plan(plan["id"]) if "items" not in plan else plan
    title = "Recovery plan" if plan["plan_type"] == "RECOVERY" else "Procurement plan"
    st.markdown(f"#### {title} #{plan['id']} &nbsp; {status_badge(plan['status'])} &nbsp; "
                f"{status_badge(plan['risk_level']) if plan['feasible'] else ''}", unsafe_allow_html=True)

    is_recovery = plan["plan_type"] == "RECOVERY"
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        kpi_card("Units re-sourced" if is_recovery else "Units covered",
                 f"{plan['quantity_covered']:,} / {plan['quantity_needed']:,}")
    with col2:
        # A recovery plan is checked against the budget still unused on the order, not the full budget.
        budget_label = "Remaining order budget" if is_recovery else "Budget"
        kpi_card("Estimated cost", format_inr(plan["total_cost"]),
                 f"{budget_label} {format_inr(plan['budget_available'])}")
    with col3:
        kpi_card("Expected delivery", plan["expected_delivery"] or "-", f"Required by {plan['required_by'] or '-'}")
    with col4:
        if is_recovery and plan["cost_difference"] is not None:
            kpi_card("Cost difference", f"{'+' if plan['cost_difference'] >= 0 else '-'}"
                     f"{format_inr(abs(plan['cost_difference']))}", "vs. original commitment")
        else:
            kpi_card("Risk level", plan["risk_level"] or "-", "rule-based assessment")

    if plan["items"]:
        st.markdown("**Recommended suppliers and quantities**")
        rows = [{**item, "reliability": f"{item['reliability']:.0%}" if item["reliability"] is not None else "-"}
                for item in plan["items"]]
        show_table(rows, {
            "supplier_name": "Source", "quantity": "Quantity", "unit_price": "Unit price (₹)",
            "line_cost": "Line cost (₹)", "lead_time_days": "Lead time (days)", "reliability": "Reliability",
        })

    request = repository.get_request(plan["request_id"])
    if request and request["quality_requirements"]:
        st.warning(f"**Quality requirements:** {request['quality_requirements']} — not verified automatically "
                   "(the simulation has no supplier certification data). Check them before approving.")

    if plan["reasons"]:
        st.error("**No feasible plan found under current constraints.**\n\n" +
                 "\n".join(f"- {reason}" for reason in plan["reasons"]))
    if plan["risk_notes"]:
        st.caption(f"Risk assessment: {plan['risk_notes']}")
    if plan["ai_explanation"]:
        with st.container(border=True):
            st.markdown("**Reason for recommendation (AI-written summary)**")
            st.write(plan["ai_explanation"])
            st.caption("Figures in the tables above come from validated database calculations and take "
                       "precedence over the wording of this summary.")

    if plan["status"] == "PROPOSED" and allow_decision:
        _render_decision_buttons(plan, key_prefix)
    else:
        event = repository.get_approval_event(plan["id"])
        if event:
            st.caption(f"Decision: {event['decision']} by {event['decided_by']} at {event['created_at']}"
                       + (f" - \"{event['comment']}\"" if event["comment"] else ""))


def _render_decision_buttons(plan: dict, key_prefix: str) -> None:
    st.info("The agent cannot purchase anything. A human must approve or reject this plan. "
            "All purchasing is simulated.")
    comment = st.text_input("Comment (optional)", key=f"{key_prefix}_comment_{plan['id']}")
    col_approve, col_reject, _ = st.columns([1, 1, 3])
    if col_approve.button("APPROVE PLAN", type="primary", key=f"{key_prefix}_approve_{plan['id']}",
                          width="stretch"):
        try:
            if plan["plan_type"] == "RECOVERY":
                recovery_service.approve_recovery_plan(plan["id"], comment=comment or None)
                st.success("Recovery plan approved. New supplier commitments created and stock reserved (simulated).")
            else:
                order_id = procurement_service.approve_procurement_plan(plan["id"], comment=comment or None)
                st.success(f"Plan approved. Simulated purchase order #{order_id} created and stock reserved.")
            st.rerun()
        except PlanDecisionError as error:
            st.error(f"Approval blocked: {error}")
    if col_reject.button("REJECT PLAN", key=f"{key_prefix}_reject_{plan['id']}", width="stretch"):
        try:
            procurement_service.reject_plan(plan["id"], comment=comment or None)
            st.warning("Plan rejected and recorded. You can change the constraints and ask the agent again.")
            st.rerun()
        except PlanDecisionError as error:
            st.error(f"Rejection blocked: {error}")


# ------------------------------------------------------------------ activity timeline

EVENT_ICONS = {
    "REQUIREMENT": "📥", "LLM_DECISION": "🧠", "TOOL_CALL": "🛠️", "VALIDATION": "✅", "RECOMMENDATION": "📝",
    "APPROVAL_REQUEST": "🙋", "FALLBACK": "⚙️", "ERROR": "⛔",
}


def render_activity_timeline(run_id: int, show_details: bool = True) -> None:
    """Human-readable timeline of what the agent actually did in one run."""
    steps = repository.list_activity(run_id)
    if not steps:
        st.info("No activity recorded for this run.")
        return
    for step in steps:
        icon = EVENT_ICONS.get(step["event_type"], "•")
        parts = [f"<div class='sr-step'><div class='title'>{icon} {step['step_number']}. "
                 f"{html.escape(step['title'])} &nbsp;{status_badge(step['status'])}</div>",
                 f"<div class='meta'>{step['created_at']} · {step['event_type'].replace('_', ' ').title()}"
                 + (f" · tool <b>{html.escape(step['tool_name'])}</b>" if step["tool_name"] else "") + "</div>"]
        if show_details:
            if step["input_summary"]:
                parts.append(f"<div class='meta'>Input: <code>{html.escape(step['input_summary'])}</code></div>")
            if step["result_summary"]:
                parts.append(f"<div>Result: {html.escape(step['result_summary'])}</div>")
            if step["explanation"]:
                parts.append(f"<div class='meta'>Decision note: {html.escape(step['explanation'])}</div>")
        parts.append("</div>")
        st.markdown("".join(parts), unsafe_allow_html=True)


def run_agent_with_progress(run_function, *args, **kwargs) -> dict | None:
    """Run an agent function while streaming its real activity steps into a status box."""
    with st.status("AI agent is working... (local Ollama model, usually 30-90 seconds)", expanded=True) as box:
        def on_progress(text: str):
            box.write(text)
        try:
            result = run_function(*args, progress_callback=on_progress, **kwargs)
        except Exception as error:
            box.update(label="Agent could not start", state="error")
            st.error(str(error))
            return None
        label = {"AWAITING_APPROVAL": "Recommendation ready - human approval required",
                 "INFEASIBLE": "Finished - no feasible plan under current constraints",
                 "FAILED": "Agent run failed"}.get(result["status"], f"Finished: {result['status']}")
        box.update(label=label, state="error" if result["status"] == "FAILED" else "complete", expanded=False)
    if not result["llm_used"]:
        st.warning("The local LLM was unavailable, so a clearly-labelled deterministic fallback produced this "
                   "result. Check the Settings page for the Ollama status.")
    for error in result.get("errors", []):
        st.error(error)
    return result

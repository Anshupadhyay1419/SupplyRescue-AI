"""Page 7 - AI Agent Activity: the real, recorded timeline of every agent run."""

import streamlit as st

from database import repository
from ui.components import kpi_card, page_header, render_activity_timeline, show_table, status_badge


def render() -> None:
    page_header("AI Agent Activity", "Every step below was written to SQLite at the moment it actually executed.")

    with st.expander("How to read this page", expanded=False):
        st.markdown(
            "- 🧠 **LLM decision** - the local Qwen3 model chose the next tool (its one-sentence reason is shown).\n"
            "- 🛠️ **Tool call** - Python executed the tool against SQLite; the result is real data.\n"
            "- ✅ **Validation** - deterministic checks (quantity, part, approval, deadline, budget).\n"
            "- 📝 **Recommendation** / 🙋 **Approval request** - the plan is saved and waits for a human.\n"
            "- ⚙️ **Fallback** - a deterministic step used only when the LLM could not do it (clearly labelled).\n\n"
            "Hidden model reasoning is never shown or stored; only the short public rationale the model wrote."
        )

    runs = repository.list_agent_runs()
    if not runs:
        st.info("The agent has not run yet. Start one from New Procurement or the Disruption Center.")
        return

    run_id = st.selectbox(
        "Agent run", [run["id"] for run in runs],
        format_func=lambda rid: _run_label(next(run for run in runs if run["id"] == rid)),
    )
    run = repository.get_agent_run(run_id)
    steps = repository.list_activity(run_id)
    tool_steps = [step for step in steps if step["event_type"] == "TOOL_CALL"]

    columns = st.columns(5)
    with columns[0]:
        kpi_card("Mission", run["run_type"].title())
    with columns[1]:
        kpi_card("Status", run["status"].title())
    with columns[2]:
        kpi_card("Tool rounds", run["iterations"])
    with columns[3]:
        kpi_card("Tools executed", len(tool_steps), f"{sum(s['status'] == 'FAILED' for s in tool_steps)} failed")
    with columns[4]:
        kpi_card("LLM", "Used" if run["llm_used"] else "Fallback", run["model"])

    st.write("")
    left, right = st.columns([3, 2])
    with left:
        st.markdown(f"**Timeline** &nbsp; {status_badge(run['status'])}", unsafe_allow_html=True)
        render_activity_timeline(run_id)
    with right:
        st.markdown("**Tool executions**")
        show_table(tool_steps, {"step_number": "Step", "tool_name": "Tool", "input_summary": "Input",
                                "result_summary": "Result", "status": "Status", "created_at": "Time"})
        if run["final_summary"]:
            with st.container(border=True):
                st.markdown("**Final recommendation note**")
                st.write(run["final_summary"])


def _run_label(run: dict) -> str:
    target = f"request #{run['request_id']}" if run["run_type"] == "PROCUREMENT" else f"disruption #{run['disruption_id']}"
    return f"Run #{run['id']} · {run['run_type'].title()} · {target} · {run['status']} · {run['started_at']}"

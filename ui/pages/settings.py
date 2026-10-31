"""Page 10 - Settings / About: model and database status, architecture, disclaimer."""

import streamlit as st

from agents.procurement_agent import check_ollama_status
from config import settings
from database.connection import fetch_one, get_database_path
from database.schema import ALL_TABLES
from ui.components import page_header

ARCHITECTURE = """
Streamlit dashboard (ui/)
        │  buttons / forms
        ▼
LangGraph agent (agents/)  ──►  Ollama · {model}  (local LLM: chooses tools, explains)
        │  tool calls
        ▼
Tools (tools/)  ──►  Services (services/): deterministic Python rules
        │                 allocation · validation · approval · disruption detection
        ▼
SQLite (database/)  ◄──  Simulation engine (supplier responses & events)
"""


def render() -> None:
    page_header("Settings / About", "System status, architecture and technology.")

    status = check_ollama_status()
    col1, col2, col3 = st.columns(3)
    with col1:
        with st.container(border=True):
            st.markdown("**Ollama**")
            if status["running"]:
                st.success("Server reachable")
            else:
                st.error(f"Not reachable: {status['error']}\n\nStart it with `ollama serve`.")
            st.caption(settings.OLLAMA_BASE_URL)
    with col2:
        with st.container(border=True):
            st.markdown("**Active model**")
            st.code(settings.OLLAMA_MODEL, language=None)
            if status["model_available"]:
                st.success("Model installed")
            else:
                st.warning(f"Model not found. Run `ollama pull {settings.OLLAMA_MODEL}`. "
                           f"Backup model configured: {settings.OLLAMA_BACKUP_MODEL}")
    with col3:
        with st.container(border=True):
            st.markdown("**Database**")
            path = get_database_path()
            st.success("SQLite ready" if path.exists() else "Not created yet")
            st.caption(str(path))

    with st.expander("Database table row counts"):
        counts = {table: fetch_one(f"SELECT COUNT(*) AS n FROM {table}")["n"] for table in ALL_TABLES}
        st.dataframe([{"Table": table, "Rows": count} for table, count in counts.items()], hide_index=True)

    st.markdown("**Agent safety limits**")
    st.write(f"Max tool rounds per run: **{settings.AGENT_MAX_ITERATIONS}** · Tool timeout: "
             f"**{settings.TOOL_TIMEOUT_SECONDS}s** · LLM timeout: **{settings.OLLAMA_REQUEST_TIMEOUT_SECONDS}s** · "
             f"Minimum supplier reliability: **{settings.MIN_SUPPLIER_RELIABILITY:.0%}**")

    st.markdown("**Architecture**")
    st.code(ARCHITECTURE.format(model=settings.OLLAMA_MODEL), language=None)

    st.markdown("**Technology stack**")
    st.markdown("Streamlit · LangGraph · langchain-ollama · Ollama (Qwen3 4B, local) · SQLite · Pandas · Plotly · "
                "python-dotenv · pytest · Python logging")

    st.warning("**Simulation disclaimer.** TechNova Electronics and every supplier are fictional. RFQs, "
               "quotations, purchase orders and supplier events are simulated inside SQLite. No email is sent, "
               "no real purchase order is placed and no payment is processed.")

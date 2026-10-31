"""
SupplyRescue AI - Streamlit entry point.

Start with:   streamlit run app.py

This file only sets up the page, makes sure the database exists, and wires
the sidebar navigation. Each page lives in ui/pages/.
"""

import streamlit as st

from config import settings
from database.seed import initialize_database_if_needed
from ui.components import inject_css
from ui.pages import (
    agent_activity,
    disruptions,
    orders,
    overview,
    procurement,
    quotations,
    recovery,
    settings as settings_page,
    simulation,
    suppliers,
)

st.set_page_config(page_title=settings.APP_NAME, page_icon="🛟", layout="wide")
inject_css()


@st.cache_resource
def _prepare_database() -> bool:
    """Create + seed the SQLite database on first start (runs once per server process)."""
    return initialize_database_if_needed()


try:
    _prepare_database()
except Exception as error:  # keep the UI alive and explain the problem
    st.error(f"Database could not be initialised: {error}")
    st.stop()

with st.sidebar:
    st.markdown(f"### 🛟 {settings.APP_NAME}")
    st.caption(settings.APP_SUBTITLE)
    st.caption(f"{settings.COMPANY_NAME} · simulated data")

pages = [
    st.Page(overview.render, title="Overview", icon=":material/dashboard:", default=True),  # served at "/"
    st.Page(procurement.render, title="New Procurement", icon=":material/add_shopping_cart:", url_path="procurement"),
    st.Page(suppliers.render, title="Supplier Directory", icon=":material/factory:", url_path="suppliers"),
    st.Page(quotations.render, title="Quotations", icon=":material/request_quote:", url_path="quotations"),
    st.Page(orders.render, title="Active Orders", icon=":material/inventory_2:", url_path="orders"),
    st.Page(disruptions.render, title="Disruption Center", icon=":material/warning:", url_path="disruptions"),
    st.Page(agent_activity.render, title="AI Agent Activity", icon=":material/smart_toy:", url_path="agent-activity"),
    st.Page(recovery.render, title="Recovery Plans", icon=":material/healing:", url_path="recovery"),
    st.Page(simulation.render, title="Simulation Lab", icon=":material/science:", url_path="simulation"),
    st.Page(settings_page.render, title="Settings / About", icon=":material/settings:", url_path="settings"),
]

st.navigation(pages).run()

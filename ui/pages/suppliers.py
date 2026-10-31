"""Page 3 - Supplier Directory with search and filters."""

import pandas as pd
import streamlit as st

from database import repository
from ui.components import page_header


def render() -> None:
    page_header("Supplier Directory", "Fictional suppliers and what they can deliver (free stock = stock − reserved).")

    rows = repository.list_supplier_directory()
    frame = pd.DataFrame(rows)

    col_search, col_part, col_approval = st.columns([2, 2, 1])
    search = col_search.text_input("Search supplier or component", placeholder="e.g. Apex, sensor")
    parts = col_part.multiselect("Part number", sorted(frame["part_number"].unique()))
    approval = col_approval.selectbox("Approval", ["All", "Approved only", "Not approved"])

    if search:
        mask = (frame["supplier_name"].str.contains(search, case=False)
                | frame["component"].str.contains(search, case=False))
        frame = frame[mask]
    if parts:
        frame = frame[frame["part_number"].isin(parts)]
    if approval == "Approved only":
        frame = frame[frame["approved"] == 1]
    elif approval == "Not approved":
        frame = frame[frame["approved"] == 0]

    display = pd.DataFrame({
        "Supplier": frame["supplier_name"],
        "Country": frame["country"],
        "Component": frame["component"],
        "Part number": frame["part_number"],
        "Available (free)": frame["free_qty"],
        "Reserved": frame["reserved_qty"],
        "Unit price (₹)": frame["unit_price"],
        "Lead time (days)": frame["lead_time_days"],
        "Reliability": frame["reliability"] * 100,
        "Approval": frame["approved"].map({1: "✔ Approved", 0: "✖ Not approved"}),
    })
    st.dataframe(
        display, hide_index=True, width="stretch",
        column_config={"Reliability": st.column_config.ProgressColumn("Reliability", format="%.0f%%",
                                                                      min_value=0, max_value=100)},
    )
    st.caption(f"{len(display)} offer(s) shown. Unapproved suppliers are visible here but are never selected "
               "automatically. Look-alike part numbers (e.g. MCU-X100B) are not compatible with MCU-X100.")

    st.markdown("**TechNova warehouse inventory**")
    warehouse = pd.DataFrame(repository.list_warehouse_inventory())
    st.dataframe(warehouse[["part_number", "component", "location", "quantity_on_hand", "quantity_reserved", "free_qty"]]
                 .rename(columns={"part_number": "Part number", "component": "Component", "location": "Location",
                                  "quantity_on_hand": "On hand", "quantity_reserved": "Reserved", "free_qty": "Free"}),
                 hide_index=True, width="stretch")

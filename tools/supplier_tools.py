"""Tools: search the supplier base and look up one supplier."""

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from database import repository
from services import procurement_service
from tools.context import check_part_number, get_run_context
from utils.validators import parse_date


@tool
def search_suppliers(part_number: str, config: RunnableConfig) -> dict:
    """Search APPROVED suppliers that stock the exact part number, with free stock, price, lead time, reliability and whether they can meet the deadline."""
    error = check_part_number(part_number, config)
    if error:
        return {"ok": False, "error": error, "summary": error}

    request_id = get_run_context(config).get("request_id")
    required_by = parse_date(repository.get_request(request_id)["required_by"]) if request_id else None
    found = procurement_service.search_suppliers(part_number, required_by)

    suppliers = [
        {
            "supplier_name": s["supplier_name"], "free_qty": s["free_qty"], "unit_price_inr": s["unit_price"],
            "lead_time_days": s["lead_time_days"], "reliability": s["reliability"],
            "can_meet_deadline": s.get("can_meet_deadline"), "meets_reliability_minimum": s["meets_reliability_minimum"],
        }
        for s in found["approved_suppliers"]
    ]
    incompatible = [f"{row['supplier_name']} ({row['part_number']})" for row in found["incompatible_similar_parts"]]
    names = ", ".join(s["supplier_name"] for s in suppliers) or "none"
    return {
        "ok": True,
        "part_number": found["part_number"],
        "days_until_deadline": found["days_available"],
        "approved_suppliers": suppliers,
        "unapproved_suppliers_excluded": found["unapproved_count"],
        "incompatible_similar_parts_ignored": incompatible,
        "summary": (f"{len(suppliers)} approved supplier(s) found: {names}. "
                    f"{found['unapproved_count']} unapproved excluded. "
                    f"Incompatible look-alike parts ignored: {', '.join(incompatible) or 'none'}."),
    }


@tool
def get_supplier_details(supplier_name: str) -> dict:
    """Get the profile of one supplier: country, reliability, approval status and the parts it offers."""
    supplier = repository.get_supplier_by_name(supplier_name)
    if supplier is None:
        message = f"No supplier named '{supplier_name}' exists."
        return {"ok": False, "error": message, "summary": message}
    offers = [row for row in repository.list_supplier_directory() if row["supplier_id"] == supplier["id"]]
    return {
        "ok": True,
        "supplier_name": supplier["name"],
        "country": supplier["country"],
        "reliability": supplier["reliability"],
        "approved": bool(supplier["approved"]),
        "offers": [
            {"part_number": o["part_number"], "free_qty": o["free_qty"], "unit_price_inr": o["unit_price"],
             "lead_time_days": o["lead_time_days"]}
            for o in offers
        ],
        "summary": (f"{supplier['name']} ({supplier['country']}): reliability {supplier['reliability']:.0%}, "
                    f"{'approved' if supplier['approved'] else 'NOT approved'}, {len(offers)} part(s) offered."),
    }

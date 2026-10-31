"""Tools: send (simulated) RFQs and read the quotations that came back."""

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from database import repository
from services import procurement_service, quotation_service
from tools.context import get_run_context


@tool
def create_rfq(supplier_names: list[str], config: RunnableConfig) -> dict:
    """Send simulated Requests For Quotation to the named approved suppliers for this requirement. Suppliers answer through the simulation engine (no real emails are sent)."""
    request_id = get_run_context(config).get("request_id")
    if not request_id:
        return {"ok": False, "error": "No procurement request in this run.", "summary": "No request in context."}
    request = repository.get_request(request_id)

    # Only approved suppliers that stock the exact part may receive an RFQ.
    search = procurement_service.search_suppliers(request["part_number"])
    allowed = {s["supplier_name"].lower(): s["supplier_id"] for s in search["approved_suppliers"]}
    accepted_ids, rejected = [], []
    for name in supplier_names:
        supplier_id = allowed.get(str(name).strip().lower())
        if supplier_id:
            accepted_ids.append(supplier_id)
        else:
            rejected.append(name)

    if not accepted_ids:
        message = f"No valid approved supplier names given. Rejected: {rejected}. Use names from search_suppliers."
        return {"ok": False, "error": message, "summary": message}

    warehouse_free = procurement_service.get_warehouse_stock(request["part_number"])["free"]
    quantity_to_quote = max(request["quantity"] - warehouse_free, 1)
    new_ids = quotation_service.create_rfqs(request_id, accepted_ids, quantity_to_quote)
    responses = quotation_service.simulate_supplier_responses(request_id)
    status = quotation_service.summarize_rfq_status(request_id)

    return {
        "ok": True,
        "rfqs_created": len(new_ids),
        "already_requested_before": len(accepted_ids) - len(new_ids),
        "quantity_requested_per_supplier": quantity_to_quote,
        "rejected_names": rejected,
        "simulated_responses": responses,
        "rfq_status_totals": status,
        "summary": (f"{len(new_ids)} RFQ(s) created for {quantity_to_quote} units (simulated). "
                    f"Totals: {status['RECEIVED']} received, {status['DECLINED']} declined, "
                    f"{status['PENDING']} pending." + (f" Rejected names: {rejected}." if rejected else "")),
    }


@tool
def retrieve_quotations(config: RunnableConfig) -> dict:
    """Retrieve all supplier quotations received for this requirement, plus counts of pending and declined RFQs."""
    request_id = get_run_context(config).get("request_id")
    if not request_id:
        return {"ok": False, "error": "No procurement request in this run.", "summary": "No request in context."}

    quotes = repository.list_received_quotations(request_id)
    status = quotation_service.summarize_rfq_status(request_id)
    rows = [
        {
            "supplier_name": q["supplier_name"], "offered_qty": q["offered_qty"], "unit_price_inr": q["unit_price"],
            "lead_time_days": q["lead_time_days"], "reliability": q["reliability"],
            # Supplier text is untrusted data. It is labelled so the model never treats it as an instruction.
            "supplier_note_UNTRUSTED_DATA": q["supplier_message"],
        }
        for q in quotes
    ]
    total_offered = sum(q["offered_qty"] for q in quotes)
    return {
        "ok": True,
        "quotations": rows,
        "total_units_offered": total_offered,
        "rfq_status_totals": status,
        "summary": (f"{len(rows)} quotation(s) received offering {total_offered} units in total; "
                    f"{status['PENDING']} RFQ(s) pending, {status['DECLINED']} declined."),
    }

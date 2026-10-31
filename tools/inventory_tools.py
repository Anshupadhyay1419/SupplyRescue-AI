"""Tool: check TechNova's own warehouse stock."""

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from database import repository
from services import procurement_service
from tools.context import check_part_number, get_run_context


@tool
def check_warehouse_inventory(part_number: str, config: RunnableConfig) -> dict:
    """Check free (unreserved) warehouse stock for an exact part number and how many units still need to be bought."""
    error = check_part_number(part_number, config)
    if error:
        return {"ok": False, "error": error, "summary": error}

    stock = procurement_service.get_warehouse_stock(part_number)
    result = {"ok": True, **stock}

    context = get_run_context(config)
    if context.get("request_id"):
        request = repository.get_request(context["request_id"])
        still_needed = max(request["quantity"] - stock["free"], 0)
        result["required_quantity"] = request["quantity"]
        result["remaining_to_procure"] = still_needed
        result["summary"] = (f"Warehouse: {stock['on_hand']} on hand, {stock['reserved']} reserved for other orders, "
                             f"{stock['free']} free. Required {request['quantity']} -> {still_needed} to procure.")
    else:
        result["summary"] = (f"Warehouse: {stock['on_hand']} on hand, {stock['reserved']} reserved, "
                             f"{stock['free']} free.")
    return result

"""
Agent tools.

Each tool is a small, typed Python function decorated with LangChain's @tool.
The LLM sees the tool name, docstring and arguments and decides WHEN to call
it; the Python code decides WHAT the answer is (from SQLite).

Tools receive the current run's ids (request / disruption) through LangChain's
RunnableConfig instead of trusting the LLM to pass them. That way the model
can never act on a different order than the one it was assigned.
"""

from tools.inventory_tools import check_warehouse_inventory
from tools.planning_tools import calculate_procurement_plan, validate_procurement_plan
from tools.quotation_tools import create_rfq, retrieve_quotations
from tools.recovery_tools import (
    check_order_status,
    generate_recovery_plan,
    get_disruption_details,
    search_recovery_options,
    validate_recovery_plan,
)
from tools.supplier_tools import get_supplier_details, search_suppliers

PROCUREMENT_TOOLS = [
    check_warehouse_inventory,
    search_suppliers,
    get_supplier_details,
    create_rfq,
    retrieve_quotations,
    calculate_procurement_plan,
    validate_procurement_plan,
]

RECOVERY_TOOLS = [
    get_disruption_details,
    check_order_status,
    check_warehouse_inventory,
    search_recovery_options,
    get_supplier_details,
    generate_recovery_plan,
    validate_recovery_plan,
]

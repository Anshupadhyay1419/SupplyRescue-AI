"""Helpers shared by all tools: reading the run context and checking part numbers."""

from langchain_core.runnables import RunnableConfig

from database import repository
from utils.validators import normalize_part_number


def get_run_context(config: RunnableConfig) -> dict:
    """Return the ids the graph attached to this run (request_id, disruption_id, ...)."""
    return (config or {}).get("configurable", {})


def get_expected_part_number(config: RunnableConfig) -> str | None:
    """The part number this run is allowed to work on."""
    context = get_run_context(config)
    if context.get("request_id"):
        request = repository.get_request(context["request_id"])
        return request["part_number"] if request else None
    if context.get("disruption_id"):
        disruption = repository.get_disruption(context["disruption_id"])
        return disruption["part_number"] if disruption else None
    return None


def check_part_number(part_number: str, config: RunnableConfig) -> str | None:
    """
    Return an error message if the model asked about a different part.

    Exact matching matters: MCU-X100B is NOT a substitute for MCU-X100.
    """
    expected = get_expected_part_number(config)
    if expected and normalize_part_number(part_number) != expected:
        return (f"This run is for part {expected}. '{part_number}' is not an exact match and "
                f"cannot be used. Call the tool again with part_number='{expected}'.")
    return None


def compact_plan(plan: dict) -> dict:
    """A short version of a plan for the LLM (keeps its context window small)."""
    return {
        "feasible": plan["feasible"],
        "quantity_needed": plan["quantity_needed"],
        "quantity_covered": plan["quantity_covered"],
        "total_cost_inr": plan["total_cost"],
        "budget_inr": plan["budget"],
        "expected_delivery": plan["expected_delivery"],
        "required_by": plan["required_by"],
        "allocation": [
            {"source": item["supplier_name"], "quantity": item["quantity"], "unit_price_inr": item["unit_price"],
             "lead_time_days": item["lead_time_days"]}
            for item in plan["items"]
        ],
        "excluded_sources": plan["excluded_sources"],
        "infeasibility_reasons": plan["reasons"],
        "risk_level": plan["risk_level"],
    }

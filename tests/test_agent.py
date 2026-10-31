"""
Agent tests.

1. Unit tests (no Ollama): a scripted fake LLM drives the real LangGraph graph,
   so routing, tool execution, iteration limits, fallbacks and approval
   behaviour are tested deterministically.
2. Integration tests (marked `ollama`): the real local model. Run with
       pytest -m ollama
"""

import pytest
from langchain_core.messages import AIMessage

from agents import procurement_agent
from agents.graph import AgentBusyError, run_procurement_agent, run_recovery_agent
from config import settings
from database import repository
from services import simulation_service
from tests.conftest import approve_main_scenario, commitment_of, get_scenario_request


class ScriptedLLM:
    """Pretends to be the chat model: returns pre-written replies in order."""

    def __init__(self, replies):
        self.replies = list(replies)

    def invoke(self, messages):
        if self.replies:
            return self.replies.pop(0)
        return AIMessage(content="Done.")


def call(name, args=None, call_id=None):
    return AIMessage(content=f"Calling {name}.",
                     tool_calls=[{"name": name, "args": args or {}, "id": call_id or name, "type": "tool_call"}])


def use_fake_llm(monkeypatch, replies):
    llm = ScriptedLLM(replies)
    monkeypatch.setattr(procurement_agent, "build_llm", lambda tools=None: llm)
    return llm


def tool_steps(run_id):
    return [s["tool_name"] for s in repository.list_activity(run_id) if s["event_type"] == "TOOL_CALL"]


def test_agent_follows_llm_tool_choices_and_requests_approval(monkeypatch):
    request = get_scenario_request("S8")
    use_fake_llm(monkeypatch, [
        call("check_warehouse_inventory", {"part_number": "MCU-X100"}),
        call("search_suppliers", {"part_number": "MCU-X100"}),
        call("create_rfq", {"supplier_names": ["Apex Microsystems", "Delta Circuitworks", "BlueRiver Components"]}),
        call("retrieve_quotations"),
        call("calculate_procurement_plan"),
        call("validate_procurement_plan"),
        AIMessage(content="Recommend warehouse + Apex + Delta."),
        AIMessage(content="Explanation of validated plan."),
    ])
    result = run_procurement_agent(request["id"])
    assert result["status"] == "AWAITING_APPROVAL"
    assert tool_steps(result["run_id"]) == ["check_warehouse_inventory", "search_suppliers", "create_rfq",
                                            "retrieve_quotations", "calculate_procurement_plan",
                                            "validate_procurement_plan"]
    plan = repository.get_plan(result["plan_id"])
    assert plan["status"] == "PROPOSED" and plan["total_cost"] == 91500  # numbers from Python, not the LLM
    assert repository.list_orders()[0]["request_id"] != request["id"]  # nothing purchased without a human
    events = [s["event_type"] for s in repository.list_activity(result["run_id"])]
    assert events[-1] == "APPROVAL_REQUEST"


def test_agent_reacts_to_insufficient_quotes(monkeypatch):
    """Only one supplier asked -> plan infeasible -> LLM asks more suppliers -> feasible."""
    request = get_scenario_request("S8")
    use_fake_llm(monkeypatch, [
        call("create_rfq", {"supplier_names": ["Granite Chipworks"]}),
        call("calculate_procurement_plan", call_id="calc1"),
        call("create_rfq", {"supplier_names": ["Apex Microsystems", "Delta Circuitworks"]}, call_id="rfq2"),
        call("calculate_procurement_plan", call_id="calc2"),
        AIMessage(content="Feasible now."),
    ])
    result = run_procurement_agent(request["id"])
    first_calc = [s for s in repository.list_activity(result["run_id"])
                  if s["tool_name"] == "calculate_procurement_plan"][0]
    assert "Infeasible" in first_calc["result_summary"]
    assert result["status"] == "AWAITING_APPROVAL"


def test_wrong_part_number_and_unknown_tool_are_handled(monkeypatch):
    request = get_scenario_request("S8")
    use_fake_llm(monkeypatch, [
        call("check_warehouse_inventory", {"part_number": "MCU-X100B"}),
        call("approve_plan", {"plan_id": 1}),  # no such tool: the agent cannot approve
        AIMessage(content="stop"),
    ])
    result = run_procurement_agent(request["id"])
    steps = [s for s in repository.list_activity(result["run_id"]) if s["event_type"] == "TOOL_CALL"]
    assert steps[0]["status"] == "FAILED" and "not an exact match" in steps[0]["result_summary"]
    assert steps[1]["status"] == "FAILED" and "Unknown tool" in steps[1]["result_summary"]


def test_iteration_limit_stops_the_loop(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_MAX_ITERATIONS", 3)
    request = get_scenario_request("S1")
    use_fake_llm(monkeypatch, [call("retrieve_quotations", call_id=f"r{i}") for i in range(20)])
    result = run_procurement_agent(request["id"])
    assert result["iterations"] == 3
    titles = [s["title"] for s in repository.list_activity(result["run_id"])]
    assert "Iteration limit reached - agent stopped" in titles
    assert result["plan_id"] is not None  # still ends with a validated (fallback) answer


def test_ollama_unavailable_uses_labelled_fallback(monkeypatch):
    def broken_llm(tools=None):
        raise ConnectionError("Ollama is not running")
    monkeypatch.setattr(procurement_agent, "build_llm", broken_llm)
    request = get_scenario_request("S8")
    result = run_procurement_agent(request["id"])
    assert result["llm_used"] is False and result["status"] == "AWAITING_APPROVAL"
    events = {s["event_type"] for s in repository.list_activity(result["run_id"])}
    assert {"ERROR", "FALLBACK"} <= events
    assert repository.get_agent_run(result["run_id"])["llm_used"] == 0


def test_same_request_cannot_be_run_twice(monkeypatch):
    use_fake_llm(monkeypatch, [AIMessage(content="x")])
    request = get_scenario_request("S1")
    run_procurement_agent(request["id"])
    with pytest.raises(AgentBusyError):
        run_procurement_agent(request["id"])


def test_recovery_agent_with_fake_llm(monkeypatch):
    approve_main_scenario()
    [disruption_id] = simulation_service.simulate_partial_supply(commitment_of("Apex Microsystems")["id"], 350)
    use_fake_llm(monkeypatch, [
        call("get_disruption_details"), call("search_recovery_options"), call("generate_recovery_plan"),
        AIMessage(content="Use BlueRiver."), AIMessage(content="Explanation."),
    ])
    result = run_recovery_agent(disruption_id)
    plan = repository.get_plan(result["plan_id"])
    assert plan["plan_type"] == "RECOVERY" and plan["status"] == "PROPOSED" and plan["quantity_covered"] == 250
    assert repository.get_disruption(disruption_id)["status"] == "RECOVERY_PROPOSED"


# ------------------------------------------------------------------ real Ollama integration

def _ollama_ready() -> bool:
    status = procurement_agent.check_ollama_status()
    return status["running"] and status["model_available"]


@pytest.mark.ollama
def test_real_model_returns_structured_tool_call():
    if not _ollama_ready():
        pytest.skip("Ollama or the configured model is not available")
    from langchain_core.messages import HumanMessage
    from tools import PROCUREMENT_TOOLS
    reply = procurement_agent.build_llm(PROCUREMENT_TOOLS).invoke(
        [HumanMessage("Check the free warehouse stock of part MCU-X100.")])
    assert reply.tool_calls and reply.tool_calls[0]["name"] == "check_warehouse_inventory"


@pytest.mark.ollama
def test_real_agent_end_to_end_procurement_and_recovery():
    if not _ollama_ready():
        pytest.skip("Ollama or the configured model is not available")
    request = get_scenario_request("S8")
    result = run_procurement_agent(request["id"])
    assert result["llm_used"] and result["status"] == "AWAITING_APPROVAL"
    assert "calculate_procurement_plan" in tool_steps(result["run_id"])
    plan = repository.get_plan(result["plan_id"])
    assert plan["feasible"] and plan["quantity_covered"] == 1000 and plan["total_cost"] <= 150000

    from services import procurement_service, recovery_service
    procurement_service.approve_procurement_plan(plan["id"])
    apex = next(c for c in repository.list_active_commitments() if c["supplier_name"] == "Apex Microsystems")
    [disruption_id] = simulation_service.simulate_partial_supply(apex["id"], 350)
    recovery = run_recovery_agent(disruption_id)
    assert recovery["llm_used"] and recovery["status"] == "AWAITING_APPROVAL"
    recovery_service.approve_recovery_plan(recovery["plan_id"])
    assert repository.get_disruption(disruption_id)["status"] == "RESOLVED"


@pytest.mark.ollama
def test_real_agent_reports_infeasibility():
    if not _ollama_ready():
        pytest.skip("Ollama or the configured model is not available")
    result = run_procurement_agent(get_scenario_request("S7")["id"])
    assert result["status"] == "INFEASIBLE"
    assert repository.get_plan(result["plan_id"])["status"] == "INFEASIBLE"


# ------------------------------------------------------------------ tool timeouts

def test_cancellation_token_blocks_late_commit_and_reports_early_commit():
    from database.connection import get_connection
    from utils.cancellation import CancellationToken, ToolCancelledError

    late = CancellationToken()
    assert late.cancel() is True  # agent gave up before the tool saved
    with pytest.raises(ToolCancelledError):
        late.commit_if_not_cancelled(get_connection())

    early = CancellationToken()
    early.commit_if_not_cancelled(get_connection())  # tool saved first
    assert early.cancel() is False  # agent learns the changes were already saved


def test_timed_out_tool_cannot_write_to_database_later():
    """A slow tool that tries to create RFQs after its timeout must save nothing."""
    import threading
    import time

    from langchain_core.runnables import RunnableConfig
    from langchain_core.tools import tool

    from services import procurement_service, quotation_service
    from utils.cancellation import ToolCancelledError

    request = get_scenario_request("S8")
    supplier_id = procurement_service.search_suppliers("MCU-X100")["approved_suppliers"][0]["supplier_id"]
    finished = threading.Event()
    outcome = {}

    @tool
    def slow_rfq_tool(config: RunnableConfig) -> dict:
        """Test tool: waits, then tries to save RFQs."""
        try:
            time.sleep(1.0)
            quotation_service.send_rfqs_and_collect_responses(
                request["id"], [supplier_id], 100, config["configurable"]["cancellation_token"])
            outcome["saved"] = True
        except ToolCancelledError:
            outcome["cancelled"] = True
        finally:
            finished.set()
        return {"ok": True}

    result = procurement_agent.run_tool_with_timeout(slow_rfq_tool, {}, {"request_id": request["id"]},
                                                     timeout_seconds=0.2)
    assert result["ok"] is False and "Nothing was saved" in result["error"]

    assert finished.wait(5)  # let the abandoned thread run to completion
    assert outcome == {"cancelled": True}
    assert repository.list_rfqs(request["id"]) == []  # no late database change


# ------------------------------------------------------------------ explanation facts

def test_infeasible_plan_facts_contain_no_recommendation():
    plan = {"feasible": False, "quantity_needed": 600, "quantity_covered": 400, "total_cost": 47000,
            "budget": 100000, "reasons": ["Insufficient eligible supply"], "cost_difference": -13000,
            "original_cost_of_units": 60000, "items": [{"supplier_name": "X"}]}
    facts = procurement_agent.build_explanation_facts(plan, "RoHS")
    assert facts["result"].startswith("No feasible plan")
    assert "allocation" not in facts and "cost_change" not in facts  # nothing that looks like a recommendation
    assert "not verified" in facts["quality_requirements"]


def test_cost_change_direction_is_spelled_out():
    plan = {"feasible": True, "quantity_needed": 250, "quantity_covered": 250, "total_cost": 27500, "budget": 83500,
            "expected_delivery": "2026-01-05", "required_by": "2026-01-07", "risk_level": "LOW", "risk_notes": ["ok"],
            "reasons": [], "cost_difference": 2500, "original_cost_of_units": 25000,
            "items": [{"supplier_name": "B", "quantity": 250, "unit_price": 110, "lead_time_days": 5,
                       "source_type": "SUPPLIER"}]}
    facts = procurement_agent.build_explanation_facts(plan, None)
    assert facts["cost_change"] == "₹2,500 MORE than the original cost of ₹25,000 for these units"
    assert "remaining order budget" in facts["total_cost_vs_budget"]

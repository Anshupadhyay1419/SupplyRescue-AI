"""
LangGraph wiring and the public "run the agent" functions.

    START
      |
    understand_requirement --(invalid)--> END
      |
    agent  <---------------------+
      |  (LLM chose tools?)      |
      +--yes--> tools -----------+     bounded by AGENT_MAX_ITERATIONS
      |
      +--no / limit / LLM down--> finalize_plan --> explain_recommendation --> request_human_approval --> END

The same graph serves two missions: PROCUREMENT (new requirement) and
RECOVERY (after a disruption). Only the prompt and the tool set differ.
"""

from langgraph.graph import END, START, StateGraph

from agents import procurement_agent as nodes
from agents.activity_log import (
    create_agent_run,
    finish_agent_run,
    has_running_recovery,
    log_activity,
    register_progress_callback,
    unregister_progress_callback,
)
from agents.state import AgentState
from config import settings
from database import repository
from services import procurement_service
from services.disruption_service import OPEN_DISRUPTION_STATUSES
from utils.logger import get_logger

logger = get_logger(__name__)


class AgentBusyError(Exception):
    """Raised when the same request/disruption is already being processed."""


# ------------------------------------------------------------------ routing functions (conditional edges)

def route_after_requirement(state: AgentState) -> str:
    """Stop immediately if the requirement failed validation."""
    return END if state.get("status") == "FAILED" else "agent"


def route_after_agent(state: AgentState) -> str:
    """
    Decide where to go after the LLM answered:
      - LLM failed               -> finalize_plan (deterministic fallback)
      - LLM asked for tools      -> tools (unless the iteration limit is reached)
      - we reminded the model    -> agent again
      - LLM gave a final answer  -> finalize_plan
    """
    if state.get("llm_failed"):
        return "finalize_plan"
    last_message = state["messages"][-1]
    if getattr(last_message, "tool_calls", None):
        if state.get("iteration", 0) >= settings.AGENT_MAX_ITERATIONS:
            return "finalize_plan"
        return "tools"
    if last_message.type == "human":  # a reminder was just added
        return "agent"
    return "finalize_plan"


def build_agent_graph():
    """Create and compile the LangGraph state machine."""
    graph = StateGraph(AgentState)

    graph.add_node("understand_requirement", nodes.understand_requirement)
    graph.add_node("agent", nodes.agent_decide)
    graph.add_node("tools", nodes.execute_tools)
    graph.add_node("finalize_plan", nodes.finalize_plan)
    graph.add_node("explain_recommendation", nodes.explain_recommendation)
    graph.add_node("request_human_approval", nodes.request_human_approval)

    graph.add_edge(START, "understand_requirement")
    graph.add_conditional_edges("understand_requirement", route_after_requirement, ["agent", END])
    graph.add_conditional_edges("agent", route_after_agent, ["tools", "agent", "finalize_plan"])
    graph.add_edge("tools", "agent")
    graph.add_edge("finalize_plan", "explain_recommendation")
    graph.add_edge("explain_recommendation", "request_human_approval")
    graph.add_edge("request_human_approval", END)

    return graph.compile()


_compiled_graph = None


def get_agent_graph():
    """Compile once and reuse."""
    global _compiled_graph
    if _compiled_graph is None:
        _compiled_graph = build_agent_graph()
    return _compiled_graph


# ------------------------------------------------------------------ public entry points

def _run_graph(initial_state: dict, run_id: int, progress_callback=None) -> dict:
    """Execute the graph and record the run outcome. Never raises for agent errors."""
    register_progress_callback(run_id, progress_callback)
    try:
        # recursion_limit is LangGraph's own safety net on top of AGENT_MAX_ITERATIONS.
        final_state = get_agent_graph().invoke(
            initial_state, config={"recursion_limit": settings.AGENT_MAX_ITERATIONS * 2 + 10}
        )
        run_status = "FAILED" if final_state.get("status") == "FAILED" else "COMPLETED"
        finish_agent_run(run_id, run_status, final_state.get("iteration", 0),
                         not final_state.get("llm_failed", False), final_state.get("final_recommendation"))
        return {
            "run_id": run_id, "status": final_state.get("status"), "plan_id": final_state.get("plan_id"),
            "errors": final_state.get("errors", []), "llm_used": not final_state.get("llm_failed", False),
            "iterations": final_state.get("iteration", 0),
        }
    except Exception as error:
        logger.exception("Agent run %s crashed", run_id)
        log_activity(run_id, "ERROR", "Agent run failed", status="FAILED", result_summary=f"{type(error).__name__}: {error}")
        finish_agent_run(run_id, "FAILED", 0, False, str(error))
        return {"run_id": run_id, "status": "FAILED", "plan_id": None, "errors": [str(error)],
                "llm_used": False, "iterations": 0}
    finally:
        unregister_progress_callback(run_id)


def run_procurement_agent(request_id: int, progress_callback=None) -> dict:
    """Run the agent for a NEW procurement request. Safe against double clicks."""
    if repository.get_request(request_id) is None:
        raise ValueError(f"Request {request_id} not found.")
    if not procurement_service.claim_request_for_planning(request_id):
        raise AgentBusyError(f"Request {request_id} is already being processed or has been completed.")

    run_id = create_agent_run("PROCUREMENT", settings.OLLAMA_MODEL, request_id=request_id)
    result = _run_graph({"run_id": run_id, "run_type": "PROCUREMENT", "request_id": request_id,
                         "iteration": 0, "nudges": 0}, run_id, progress_callback)
    if result["status"] == "FAILED":
        procurement_service.set_request_status(request_id, "FAILED")
    return result


def run_recovery_agent(disruption_id: int, extra_budget: float = 0, deadline_extension_days: int = 0,
                       progress_callback=None) -> dict:
    """Run the agent to recover from an open disruption."""
    disruption = repository.get_disruption(disruption_id)
    if disruption is None:
        raise ValueError(f"Disruption {disruption_id} not found.")
    if disruption["status"] not in OPEN_DISRUPTION_STATUSES:
        raise AgentBusyError(f"Disruption {disruption_id} is {disruption['status']} - nothing to recover.")
    if has_running_recovery(disruption_id):
        raise AgentBusyError(f"A recovery run for disruption {disruption_id} is already in progress.")

    run_id = create_agent_run("RECOVERY", settings.OLLAMA_MODEL, request_id=disruption["request_id"],
                              disruption_id=disruption_id)
    return _run_graph({"run_id": run_id, "run_type": "RECOVERY", "disruption_id": disruption_id,
                       "extra_budget": extra_budget, "deadline_extension_days": deadline_extension_days,
                       "iteration": 0, "nudges": 0}, run_id, progress_callback)

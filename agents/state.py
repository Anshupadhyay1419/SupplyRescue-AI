"""
LangGraph state: the shared "memory" passed between graph nodes.

Each node returns a dictionary with only the keys it wants to change.
Keys with a reducer (Annotated[..., reducer]) are MERGED instead of replaced:
  - messages      -> new messages are appended (LangGraph's add_messages)
  - tool_results  -> latest result per tool name is merged in
  - tools_called / errors -> lists are concatenated
"""

import operator
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


def merge_dicts(old: dict, new: dict) -> dict:
    """Reducer: combine two dictionaries, newer values win."""
    merged = dict(old or {})
    merged.update(new or {})
    return merged


class AgentState(TypedDict, total=False):
    # --- What the agent is working on ---
    run_id: int
    run_type: str                     # "PROCUREMENT" or "RECOVERY"
    request_id: int | None
    disruption_id: int | None
    requirement: dict                 # validated requirement or disruption facts
    extra_budget: float               # recovery: extra budget allowed by a human
    deadline_extension_days: int      # recovery: extra days allowed by a human

    # --- Conversation with the LLM (system prompt is added per call) ---
    messages: Annotated[list[AnyMessage], add_messages]

    # --- Loop control ---
    iteration: int                    # number of tool rounds executed so far
    nudges: int                       # times we reminded the model to finish its job
    llm_failed: bool                  # True if Ollama could not be used

    # --- Results ---
    tool_results: Annotated[dict, merge_dicts]
    tools_called: Annotated[list[str], operator.add]
    candidate_plan: dict | None       # authoritative plan computed by Python
    validation: dict | None           # validation checklist for that plan
    final_recommendation: str | None  # explanation shown to the human
    plan_id: int | None               # saved plan waiting for approval
    status: str
    errors: Annotated[list[str], operator.add]

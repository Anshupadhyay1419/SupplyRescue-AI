"""
Verify that the configured Ollama model produces structured tool calls.

Run:  python scripts/verify_tool_calling.py [model_name]

This is the check performed before choosing the agent's model. It binds one
simple tool to the model and asks a question that requires that tool.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_ollama import ChatOllama

from config import settings


@tool
def check_warehouse_inventory(part_number: str) -> dict:
    """Return the free (unreserved) warehouse stock for an exact part number."""
    return {"part_number": part_number, "free_quantity": 100}


def verify(model_name: str) -> bool:
    """Return True if the model called the tool with the correct argument."""
    llm = ChatOllama(
        model=model_name,
        base_url=settings.OLLAMA_BASE_URL,
        temperature=0,
        num_ctx=4096,
        reasoning=False,
    ).bind_tools([check_warehouse_inventory])

    started = time.time()
    reply = llm.invoke([HumanMessage("How many MCU-X100 units are free in the warehouse? Use your tool.")])
    elapsed = time.time() - started

    print(f"Model: {model_name}  ({elapsed:.1f}s)")
    print(f"Tool calls: {reply.tool_calls}")
    ok = any(
        call["name"] == "check_warehouse_inventory" and call["args"].get("part_number", "").upper() == "MCU-X100"
        for call in reply.tool_calls
    )
    print("RESULT:", "PASS - structured tool call received" if ok else "FAIL - no valid tool call")
    return ok


if __name__ == "__main__":
    chosen_model = sys.argv[1] if len(sys.argv) > 1 else settings.OLLAMA_MODEL
    sys.exit(0 if verify(chosen_model) else 1)

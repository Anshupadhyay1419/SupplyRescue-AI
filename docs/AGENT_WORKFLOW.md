# Agent Workflow (LangGraph)

One compiled graph (`agents/graph.py`) serves two missions:

* **PROCUREMENT** – a new requirement (`run_procurement_agent(request_id)`)
* **RECOVERY** – an open disruption (`run_recovery_agent(disruption_id)`)

Only the system prompt and the tool set differ.

```
START
  │
  ▼
understand_requirement ──(status == FAILED)──► END
  │
  ▼
agent ◄────────────────────────┐
  │ route_after_agent          │
  ├── tool calls ──► tools ────┘   (iteration += 1)
  ├── reminder added ──► agent
  └── final answer / iteration limit / LLM failed
        │
        ▼
finalize_plan ──► explain_recommendation ──► request_human_approval ──► END
```

## State (`agents/state.py`)

| Key | Meaning |
|---|---|
| `run_id`, `run_type`, `request_id`, `disruption_id` | what is being worked on |
| `requirement` | validated requirement or disruption facts |
| `messages` | conversation with the LLM (append reducer) |
| `iteration`, `nudges` | loop counters |
| `tool_results`, `tools_called` | latest result per tool / successful tool names |
| `llm_failed`, `errors` | failure tracking |
| `candidate_plan`, `validation` | authoritative plan + checklist |
| `final_recommendation`, `plan_id`, `status` | outcome |

## Nodes

### 1. `understand_requirement`
* PROCUREMENT: loads the request from SQLite, validates it with `utils/validators.validate_requirement`, logs **"Requirement received and validated"**, and writes the first `HumanMessage` for the LLM.
* RECOVERY: loads the disruption and logs **"Supply disruption received"**.
* Conditional edge `route_after_requirement`: invalid → `END` (run marked FAILED).

### 2. `agent` (`agent_decide`) — the LLM decision point
* Calls `ChatOllama(...).bind_tools(tools)` with the system prompt + history.
* If the model returns **tool calls** → logs `LLM_DECISION` with the tool names and the model's one-sentence public reason.
* If it returns **no tool call** but never computed a plan → adds one reminder message (`nudges`), loops back.
* If Ollama fails (not running, model missing, timeout, malformed output) → sets `llm_failed` and continues to the deterministic fallback.

### 3. `tools` (`execute_tools`)
* Runs each requested tool in a worker thread with `TOOL_TIMEOUT_SECONDS`.
* Unknown tool names, wrong part numbers and argument errors become **error results** the LLM can read (no crash).
* Logs every execution as `TOOL_CALL` (input summary, result summary, SUCCESS/FAILED).
* Returns `ToolMessage`s and increments `iteration`.

### Conditional edge `route_after_agent`
| Situation | Next node |
|---|---|
| `llm_failed` | `finalize_plan` |
| tool calls and `iteration < AGENT_MAX_ITERATIONS` | `tools` |
| tool calls but limit reached | `finalize_plan` |
| last message is a reminder | `agent` |
| final answer | `finalize_plan` |

### 4. `finalize_plan` (deterministic)
* Logs a WARNING if the iteration limit was hit.
* PROCUREMENT: if no quotations exist (LLM down or skipped it) → labelled `FALLBACK` RFQs to all approved suppliers.
* Recomputes the plan from SQLite and runs the validation checklist (Quantity, Part compatibility, Supplier approval, Deadline, Budget); each check is logged as `VALIDATION`.
* The plan is feasible only if the allocator **and** the checklist agree.

### 5. `explain_recommendation`
* Sends pre-formatted, validated facts to the LLM (no tools) and asks for a 3–5 sentence note. Falls back to a Python template if the LLM is unavailable. Logged as `RECOMMENDATION`.

### 6. `request_human_approval`
* Saves the plan as `PROPOSED` (feasible) or `INFEASIBLE`, updates the request/disruption status and logs **"Human approval requested"**. The graph ends here — approval happens only through the dashboard.

## Tools

| Mission | Tool | What it does |
|---|---|---|
| Both | `check_warehouse_inventory(part_number)` | on hand / reserved / free stock, remaining need |
| Procurement | `search_suppliers(part_number)` | approved exact-match suppliers, deadline flag, ignored look-alikes |
| Both | `get_supplier_details(supplier_name)` | supplier profile and offers |
| Procurement | `create_rfq(supplier_names)` | simulated RFQs + simulated responses |
| Procurement | `retrieve_quotations()` | received quotes, pending/declined counts, untrusted notes |
| Procurement | `calculate_procurement_plan()` | cheapest feasible allocation or reasons + suppliers not yet asked |
| Procurement | `validate_procurement_plan()` | independent checklist |
| Recovery | `get_disruption_details()` | shortage, units to re-source, remaining budget, days left |
| Recovery | `check_order_status()` | order lines and current commitments |
| Recovery | `search_recovery_options()` | warehouse + alternatives with eligibility problems |
| Recovery | `generate_recovery_plan()` | cheapest feasible recovery + cost difference |
| Recovery | `validate_recovery_plan()` | independent checklist |

## Example of adaptive behaviour

Observed in a real run on MCU-X100: after `search_suppliers` returned five approved suppliers, the model sent RFQs to only four of them — it skipped Cobalt Semicon because the tool reported `can_meet_deadline: false` (12-day lead time vs 7 days). In the unit test `test_agent_reacts_to_insufficient_quotes`, an initially infeasible plan (only Granite asked) makes the agent request more quotes and recalculate — the `calculate_procurement_plan` result explicitly lists `approved_suppliers_not_yet_asked_for_quotes` to support that decision.

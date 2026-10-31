# Architecture

## Layers

| Layer | Folder | Responsibility | Talks to |
|---|---|---|---|
| Presentation | `ui/`, `app.py` | Streamlit pages, forms, buttons, charts | agents, services, repository |
| Agent | `agents/` | LangGraph graph, LLM calls, tool execution, activity log | tools, services, Ollama |
| Tools | `tools/` | Typed `@tool` functions the LLM may call | services, repository |
| Services | `services/` | All business rules and calculations (deterministic) | database |
| Data | `database/` | Connections, schema, seed data, read queries | SQLite file |
| Utilities | `utils/`, `config/` | Settings, logging, input validation | – |

Imports only go **downwards** (ui → agents → tools → services → database), which prevents circular imports.

## Key design decisions

1. **The LLM never calculates.** Quantities, costs, deadlines and approval checks live in `services/planning_service.py` (pure functions, fully unit-tested). Tools return their results; the LLM only reads them.
2. **The plan shown to humans is recomputed by Python** in the `finalize_plan` node, independently of what the LLM said. The LLM explanation is generated *from* the validated plan using pre-formatted facts.
3. **No approval tool exists.** The agent physically cannot approve or purchase; approval is a dashboard button calling `approve_procurement_plan` / `approve_recovery_plan`.
4. **Context injection instead of trusting the model with IDs.** Tools receive `request_id` / `disruption_id` through LangChain's `RunnableConfig`, so the LLM cannot act on another order. Part numbers passed by the LLM are checked for an exact match.
5. **Untrusted supplier text.** Supplier notes are labelled `..._UNTRUSTED_DATA` in tool results, the system prompt forbids following them, and no calculation reads them. Granite Chipworks carries a deliberate prompt-injection note to demonstrate this.
6. **Every multi-table write is one transaction** (`database/connection.py: transaction()`, `BEGIN IMMEDIATE`). If any step fails, nothing is written.
7. **Disruptions require evidence.** Only rows in `supplier_updates` (simulated supplier signals) can create disruptions.

## Database (17 tables)

```
components ─┬─ supplier_inventory ── suppliers
            └─ warehouse_inventory
procurement_requests ─┬─ rfqs ── quotations
                      ├─ procurement_plans ── plan_items
                      │        └─ approval_events (UNIQUE plan_id)
                      └─ purchase_orders (UNIQUE request_id, UNIQUE plan_id)
                               └─ order_items (UNIQUE plan_item_id)
                                       └─ supplier_commitments
                                               ├─ supplier_updates (signals)
                                               └─ disruptions (UNIQUE supplier_update_id)
agent_runs ── agent_activity_logs
```

### Integrity protections

| Risk | Protection |
|---|---|
| Negative inventory | `CHECK (stock_qty >= 0)`, `CHECK (reserved_qty <= stock_qty)` and guarded `UPDATE ... WHERE stock - reserved >= qty` |
| Duplicate allocation | `order_items.plan_item_id UNIQUE`; reservation only inside approval transaction |
| Repeated approval side effects | Conditional `UPDATE ... WHERE status='PROPOSED'` + `approval_events.plan_id UNIQUE` |
| Unapproved supplier | Filtered in planner, validator, RFQ tool and re-checked at approval |
| Invalid statuses | `CHECK (status IN (...))` on every status column |
| Same signal detected twice | `supplier_updates.processed` + `disruptions.supplier_update_id UNIQUE` |
| Double-clicked "Run agent" | Atomic claim `NEW → PLANNING`; running-recovery check |

## Allocation algorithm

1. Exclude sources that break a hard rule (wrong part, unapproved, reliability < 80 %, no stock, too slow) and record why.
2. Use free warehouse stock first.
3. Fill the rest from the cheapest eligible supplier upward (ties: higher reliability, then faster).
4. Infeasible if quantity is not covered or cost > budget; explain which constraint blocks it by relaxing one rule at a time against the whole supplier catalogue.

With linear per-unit prices and interchangeable units this greedy order **is** the minimum-cost solution, so "over budget" means *no* compliant plan exists. With MOQs/discounts it would only be a heuristic (documented in the code).

## Tool timeouts (why a late tool can't change the database)

Python threads cannot be killed, so after a timeout the worker thread keeps running. To make that harmless:

1. Every tool call gets a `CancellationToken` (`utils/cancellation.py`), passed in the tool's `RunnableConfig`.
2. The only agent tool that writes (`create_rfq`) saves everything in **one** transaction
   (`quotation_service.send_rfqs_and_collect_responses`) and commits through the token.
3. On timeout the agent calls `token.cancel()`. A lock makes *cancel* and *commit* mutually exclusive:
   * cancel wins → the tool's later commit raises `ToolCancelledError` and the transaction rolls back;
   * commit already happened → the agent is told "already saved; read the current data".

All other agent tools only read data, so abandoning them changes nothing. Covered by
`test_timed_out_tool_cannot_write_to_database_later`.

## Order totals

`purchase_orders.total_cost` always equals Σ(committed_qty × unit_price) of the order's active commitments
(warehouse lines add ₹0). It is recalculated by `procurement_service.recalculate_order_total` whenever a
commitment changes: on supplier updates (reduction, cancellation, price change) and on recovery approval.
`tests/conftest.py: check_inventory_and_order_consistency` re-derives every reservation and total from raw
records and is asserted after each disruption/recovery test.

## Quality requirements

Stored with each request and displayed on every plan with a "not verified automatically" warning. The agent
prompt forbids claiming they are met, and the explanation facts mark them as unverified. There is no
certification data in the simulation, so no automatic check is performed.

## Agent safety limits

| Limit | Default | Setting |
|---|---|---|
| Tool rounds per run | 10 | `AGENT_MAX_ITERATIONS` |
| Tool timeout | 20 s | `TOOL_TIMEOUT_SECONDS` |
| LLM request timeout | 120 s | `OLLAMA_REQUEST_TIMEOUT_SECONDS` |
| LangGraph recursion limit | 2 × iterations + 10 | in `agents/graph.py` |
| Early-finish reminder | 1 | `agent_decide` |

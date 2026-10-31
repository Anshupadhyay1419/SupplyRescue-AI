# SupplyRescue AI — Explained Simply

## 1. What is SupplyRescue AI?
A local web application for a fictional electronics company, **TechNova Electronics**. It helps a procurement officer buy components (microchips, sensors, capacitors…) and rescues orders when a supplier lets them down before delivery. An AI agent does the investigation work; a human makes the final decision.

## 2. What problem does it solve?
Buying parts means checking stock, finding qualified suppliers, comparing quotes, splitting orders and watching deliveries. If a supplier reduces a confirmed order from 600 to 350 units a few days before the deadline, somebody must notice, calculate the shortage (250) and find a replacement fast. SupplyRescue AI automates the investigation and makes the problem visible immediately.

## 3. Why is Agentic AI useful here?
The job is not one question with one answer. It is a **sequence of decisions that depends on what you discover**: if the warehouse has stock, buy less; if quotes are not enough, ask more suppliers; if the cheapest supplier is too slow, use a faster one; if nothing works, say so. An agent — an LLM that can call tools in a loop — fits this "look, decide, act, look again" pattern.

## 4. What does the LLM do?
The local Qwen3 4B model (through Ollama):
* reads the requirement and the tool results,
* **chooses which tool to call next** (and writes one sentence explaining why),
* decides when the investigation is finished,
* writes the plain-language recommendation note.

It does **not** calculate costs, check budgets or approve anything.

## 5. What does LangGraph do?
LangGraph is the "flowchart engine" for the agent. It holds the **state** (requirement, messages, tool results, plan…) and moves between **nodes**: understand requirement → LLM decides → run tools → (repeat) → finalize plan → explain → request approval. **Conditional edges** decide the next node, e.g. "if the LLM asked for tools, go to the tools node; if it is done, go to finalize". It also enforces the iteration limit.

## 6. What does SQLite do?
SQLite is the database file that stores *everything*: suppliers, stock, requests, RFQs, quotes, plans, orders, commitments, supplier updates, disruptions, approvals and the agent's activity log. Constraints in the tables prevent impossible data (negative stock, a plan approved twice).

## 7. How does supplier search work?
`search_suppliers` looks for suppliers that sell the **exact** part number (MCU-X100B is *not* MCU-X100), are **approved**, and have **free** stock (stock minus units already reserved for other customers). It also reports whether each supplier can deliver before the deadline.

## 8. How does the agent detect disruptions?
Only from evidence. When a supplier changes a commitment (in the demo, via the Simulation Lab) a row is written to `supplier_updates`. The **commitment monitor** compares the new promise with the current one:
`old 600, new 350 → shortage 250`. It records a disruption, marks the order **AT RISK** and shows "Supply Risk Detected". Delays that still arrive before the deadline are recorded as "no action needed".

## 9. How does recovery planning work?
The recovery agent reads the disruption, checks the order and warehouse, searches alternative approved suppliers (never the one that just failed, except after a price increase, when "keep at the new price" is compared too), and generates the cheapest plan that covers the missing units within the **remaining** budget and the original deadline. If no plan works, it says *"No feasible recovery plan found under current constraints"* and lists why.

## 10. How does human approval work?
Every plan is saved as **PROPOSED**. The dashboard shows suppliers, quantities, costs, delivery dates, risk and the reason. Clicking **APPROVE PLAN** runs one database transaction that re-checks stock, approval, deadline and budget, then creates the (simulated) purchase order, supplier commitments and stock reservations. Clicking it twice does nothing harmful — the second click is refused. **REJECT PLAN** records the rejection and lets you change the constraints and ask again.

**What about quality requirements?** They are saved and shown to the approver, but the simulation has no supplier certification data, so the system does *not* check them and says so on every plan. The approver must confirm them.

## 11. Why use Python tools?
Because LLMs are good at language and choices but unreliable at arithmetic and rules. During development the model once described a +₹2,500 cost difference as "₹2,500 below the original" — a perfect example. So money, quantities and rules are Python functions with unit tests; the LLM only decides what to look at and explains the result.

## 12. What makes this agentic instead of a normal chatbot?
| Chatbot | SupplyRescue agent |
|---|---|
| Answers from its training data | Calls **tools** that read and write a real database |
| One reply per question | **Loop**: decide → act → observe → decide again |
| Same path every time | Path depends on results (skips slow suppliers, asks more suppliers, stops when infeasible) |
| No memory of actions | **State** and a persisted activity log of every action |
| Can only talk | Produces a validated plan that changes business data **after human approval** |

At the same time it is honest about its limits: the deterministic parts are labelled as deterministic, and if Ollama is down the app says it used a fallback instead of pretending the AI did it.

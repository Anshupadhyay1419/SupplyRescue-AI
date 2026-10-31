"""
Deterministic planning logic: supplier allocation and plan validation.

This file contains NO database access and NO AI. It is plain Python math,
which makes it easy to test and impossible for an LLM to "hallucinate".

How allocation works (easy to explain in a viva):
  1. Filter out sources that break a HARD rule:
       wrong part number, unapproved supplier, reliability below minimum,
       no free stock, or delivery later than the deadline.
  2. Take free warehouse stock first (it is already owned, so it costs
     nothing extra to procure).
  3. Fill the rest from the cheapest eligible supplier, then the next
     cheapest, and so on (ties broken by higher reliability, then faster delivery).
  4. If the quantity cannot be covered, or the total cost is above budget,
     the plan is marked infeasible with clear reasons.

Why "cheapest first" is optimal here:
  Every unit of the same part is interchangeable and each supplier charges a
  fixed price per unit with no minimum order or fixed fee. Under those
  assumptions, filling from the cheapest eligible supplier upward always gives
  the lowest possible total cost. So if this plan is over budget, NO plan made
  from the same eligible suppliers can be within budget.
  If real suppliers had minimum order quantities, volume discounts or shipping
  fees, this would become a heuristic and would no longer be guaranteed optimal.
"""

from datetime import date, timedelta

from config import settings

# A plan may not rely on a supplier below this reliability.
DEFAULT_MIN_RELIABILITY = settings.MIN_SUPPLIER_RELIABILITY


def calculate_shortage(required_quantity: int, available_quantity: int) -> int:
    """Calculate how many additional units are needed (never negative)."""
    shortage = required_quantity - available_quantity
    return max(shortage, 0)


def days_until(deadline: date, today: date | None = None) -> int:
    """Number of whole days between today and the deadline."""
    today = today or date.today()
    return (deadline - today).days


def find_eligibility_problems(source: dict, part_number: str, days_available: int, min_reliability: float) -> list[str]:
    """
    Return the reasons a source may NOT be used (empty list = eligible).

    `source` is a dictionary describing the warehouse or one supplier offer.
    """
    problems = []

    if source["part_number"] != part_number:
        problems.append(f"part number {source['part_number']} is not an exact match for {part_number}")

    if source["source_type"] == "SUPPLIER":
        if not source.get("approved"):
            problems.append("supplier is not on the approved vendor list")
        if source.get("reliability", 0) < min_reliability:
            problems.append(
                f"reliability {source.get('reliability', 0):.0%} is below the {min_reliability:.0%} minimum"
            )

    if source["available_qty"] <= 0:
        problems.append("no free stock available")

    if source["lead_time_days"] > days_available:
        problems.append(
            f"lead time {source['lead_time_days']} days exceeds the {days_available} days available before the deadline"
        )

    return problems


def sort_sources_for_allocation(sources: list[dict]) -> list[dict]:
    """
    Order eligible sources: warehouse first, then cheapest supplier.

    Ties on price are broken by higher reliability, then shorter lead time,
    then name (so the result is always the same for the same data).
    """

    def sort_key(source):
        is_supplier = 1 if source["source_type"] == "SUPPLIER" else 0
        return (
            is_supplier,
            source["unit_price"],
            -source.get("reliability", 1.0),
            source["lead_time_days"],
            source.get("supplier_name") or "",
        )

    return sorted(sources, key=sort_key)


def assess_plan_risk(items: list[dict], days_available: int) -> tuple[str, list[str]]:
    """
    Give a simple, explainable risk level (LOW / MEDIUM / HIGH) for a plan.

    Rules:
      - any supplier below 90% reliability adds risk
      - less than 2 days of slack between delivery and deadline adds risk
      - depending on 3 or more suppliers adds coordination risk
    """
    notes = []
    risk_points = 0

    supplier_items = [item for item in items if item["source_type"] == "SUPPLIER"]
    for item in supplier_items:
        if item.get("reliability", 1.0) < 0.90:
            risk_points += 1
            notes.append(f"{item['supplier_name']} reliability is {item['reliability']:.0%} (below 90%).")

    if supplier_items:
        slowest = max(item["lead_time_days"] for item in supplier_items)
        slack = days_available - slowest
        if slack < 2:
            risk_points += 1
            notes.append(f"Only {slack} day(s) of slack between the latest delivery and the deadline.")

    if len(supplier_items) >= 3:
        risk_points += 1
        notes.append(f"Plan depends on {len(supplier_items)} different suppliers.")

    if not notes:
        notes.append("Reliable suppliers with comfortable delivery slack.")

    if risk_points == 0:
        return "LOW", notes
    if risk_points == 1:
        return "MEDIUM", notes
    return "HIGH", notes


def explain_infeasibility(
    sources: list[dict],
    part_number: str,
    quantity_needed: int,
    days_available: int,
    min_reliability: float,
) -> list[str]:
    """
    When no plan works, explain WHICH constraint is responsible.

    We recount supply while relaxing one rule at a time, so the user learns
    e.g. "enough stock exists, but only from slow or unapproved suppliers".
    """
    hints = []
    exact_sources = [source for source in sources if source["part_number"] == part_number]

    def total_qty(selected):
        return sum(source["available_qty"] for source in selected)

    usable_ignoring_deadline = [
        source for source in exact_sources
        if source["source_type"] == "WAREHOUSE"
        or (source.get("approved") and source.get("reliability", 0) >= min_reliability)
    ]
    if total_qty(usable_ignoring_deadline) >= quantity_needed:
        hints.append(
            "Enough approved supply exists only if the deadline is extended "
            f"(slowest needed supplier is outside the {days_available}-day window)."
        )

    fast_sources = [source for source in exact_sources if source["lead_time_days"] <= days_available]
    fast_and_approved = [source for source in fast_sources if source in usable_ignoring_deadline]
    if total_qty(fast_sources) >= quantity_needed and total_qty(fast_and_approved) < quantity_needed:
        hints.append(
            "Enough fast supply exists only from unapproved or low-reliability suppliers, "
            "which the system will never select automatically."
        )

    if total_qty(exact_sources) < quantity_needed:
        hints.append(
            f"Total known supply of {part_number} ({total_qty(exact_sources)} units) is below the "
            f"{quantity_needed} units needed, even ignoring every other rule."
        )

    return hints


def allocate_supply(
    sources: list[dict],
    part_number: str,
    quantity_needed: int,
    required_by: date,
    budget: float,
    today: date | None = None,
    min_reliability: float = DEFAULT_MIN_RELIABILITY,
    market_sources: list[dict] | None = None,
) -> dict:
    """
    Build the lowest-cost plan that satisfies all hard constraints.

    `market_sources` (optional) is the whole known supplier catalogue. It is
    used ONLY to explain an infeasible result, never to allocate units.

    Input:
      sources  - list of dicts with keys: source_type ('WAREHOUSE'/'SUPPLIER'),
                 supplier_id, supplier_name, part_number, available_qty,
                 unit_price, lead_time_days, reliability, approved
      quantity_needed, required_by, budget - the constraints
    Output:
      a plan dictionary (feasible flag, items, cost, reasons, risk ...)
    """
    today = today or date.today()
    days_available = days_until(required_by, today)

    # Step 1: hard-constraint filtering, remembering why sources were excluded.
    eligible = []
    excluded = []
    for source in sources:
        problems = find_eligibility_problems(source, part_number, days_available, min_reliability)
        if problems:
            excluded.append({"name": source.get("supplier_name") or "Warehouse", "reasons": problems})
        else:
            eligible.append(source)

    # Step 2 + 3: warehouse first, then cheapest suppliers.
    items = []
    remaining = quantity_needed
    for source in sort_sources_for_allocation(eligible):
        if remaining <= 0:
            break
        take = min(source["available_qty"], remaining)
        items.append({
            "source_type": source["source_type"],
            "supplier_id": source.get("supplier_id"),
            "supplier_name": source.get("supplier_name") or "TechNova Warehouse",
            "part_number": source["part_number"],
            "quantity": take,
            "unit_price": source["unit_price"],
            "line_cost": round(take * source["unit_price"], 2),
            "lead_time_days": source["lead_time_days"],
            "reliability": source.get("reliability", 1.0),
            "approved": bool(source.get("approved", True)),
        })
        remaining -= take

    quantity_covered = quantity_needed - remaining
    total_cost = round(sum(item["line_cost"] for item in items), 2)

    # Step 4: decide feasibility and explain it.
    reasons = []
    if remaining > 0:
        reasons.append(
            f"Insufficient eligible supply: {quantity_covered} of {quantity_needed} units covered "
            f"({remaining} units short)."
        )
        reasons.extend(explain_infeasibility(market_sources or sources, part_number, quantity_needed,
                                             days_available, min_reliability))
    if total_cost > budget:
        reasons.append(
            f"Lowest possible cost ₹{total_cost:,.0f} exceeds the budget of ₹{budget:,.0f} "
            f"by ₹{total_cost - budget:,.0f}."
        )

    feasible = not reasons
    if days_available <= 0:
        feasible = False
        reasons.append("The required delivery date has already passed.")

    lead_times = [item["lead_time_days"] for item in items]
    expected_delivery = today + timedelta(days=max(lead_times)) if lead_times else None
    risk_level, risk_notes = assess_plan_risk(items, days_available)

    return {
        "feasible": feasible,
        "part_number": part_number,
        "quantity_needed": quantity_needed,
        "quantity_covered": quantity_covered,
        "shortfall": remaining,
        "total_cost": total_cost,
        "budget": budget,
        "budget_remaining": round(budget - total_cost, 2),
        "required_by": required_by.isoformat(),
        "days_available": days_available,
        "expected_delivery": expected_delivery.isoformat() if expected_delivery else None,
        "items": items,
        "excluded_sources": excluded,
        "reasons": reasons,
        "risk_level": risk_level if feasible else "HIGH",
        "risk_notes": risk_notes,
    }


def validate_plan(
    plan: dict,
    part_number: str,
    quantity_needed: int,
    required_by: date,
    budget: float,
    today: date | None = None,
    min_reliability: float = DEFAULT_MIN_RELIABILITY,
) -> dict:
    """
    Independently re-check a plan against every business rule.

    This is a second line of defence: even if a plan came from somewhere else
    (or was edited), it is only valid if every check below passes.
    Returns {"valid": bool, "checks": [{"name", "passed", "detail"}, ...]}.
    """
    today = today or date.today()
    days_available = days_until(required_by, today)
    items = plan.get("items", [])
    checks = []

    covered = sum(item["quantity"] for item in items)
    checks.append({
        "name": "Quantity",
        "passed": covered == quantity_needed,
        "detail": f"{covered} of {quantity_needed} units allocated.",
    })

    wrong_parts = [item["supplier_name"] for item in items if item["part_number"] != part_number]
    checks.append({
        "name": "Part compatibility",
        "passed": not wrong_parts,
        "detail": "All lines use the exact part number." if not wrong_parts
        else f"Wrong part number from: {', '.join(wrong_parts)}.",
    })

    unapproved = [
        item["supplier_name"] for item in items
        if item["source_type"] == "SUPPLIER" and not item.get("approved", True)
    ]
    low_reliability = [
        item["supplier_name"] for item in items
        if item["source_type"] == "SUPPLIER" and item.get("reliability", 1.0) < min_reliability
    ]
    checks.append({
        "name": "Supplier approval",
        "passed": not unapproved and not low_reliability,
        "detail": "All suppliers are approved and meet the reliability minimum."
        if not unapproved and not low_reliability
        else f"Unapproved: {unapproved or 'none'}; below reliability minimum: {low_reliability or 'none'}.",
    })

    late = [item["supplier_name"] for item in items if item["lead_time_days"] > days_available]
    checks.append({
        "name": "Deadline",
        "passed": not late and days_available > 0,
        "detail": f"All deliveries arrive within {days_available} days." if not late and days_available > 0
        else f"Late or impossible deliveries: {', '.join(late) or 'deadline already passed'}.",
    })

    total_cost = round(sum(item["quantity"] * item["unit_price"] for item in items), 2)
    checks.append({
        "name": "Budget",
        "passed": total_cost <= budget,
        "detail": f"Total ₹{total_cost:,.0f} vs budget ₹{budget:,.0f}.",
    })

    return {"valid": all(check["passed"] for check in checks), "checks": checks}

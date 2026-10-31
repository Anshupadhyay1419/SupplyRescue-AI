"""
Input validation for procurement requirements.

The dashboard form and the agent both call `validate_requirement` before any
work starts, so invalid data never reaches the database or the LLM.
"""

import re
from datetime import date, datetime

# Part numbers look like "MCU-X100" or "MEM-DDR4-8G": letters, digits and dashes.
PART_NUMBER_PATTERN = re.compile(r"^[A-Z0-9]+(-[A-Z0-9]+)*$")


def normalize_part_number(part_number: str) -> str:
    """Trim spaces and upper-case a part number so comparisons are exact."""
    return (part_number or "").strip().upper()


def parse_date(value) -> date | None:
    """Accept a date, datetime or 'YYYY-MM-DD' string and return a date (or None)."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def validate_requirement(requirement: dict, today: date | None = None) -> list[str]:
    """
    Check a procurement requirement and return a list of error messages.

    An empty list means the requirement is valid.
    Expected keys: component_name, part_number, quantity, required_by, max_budget.
    """
    today = today or date.today()
    errors = []

    if not str(requirement.get("component_name", "")).strip():
        errors.append("Component name is required.")

    part_number = normalize_part_number(requirement.get("part_number", ""))
    if not part_number:
        errors.append("Part number is required.")
    elif not PART_NUMBER_PATTERN.match(part_number):
        errors.append("Part number may only contain letters, digits and single dashes (e.g. MCU-X100).")

    quantity = requirement.get("quantity")
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
        errors.append("Quantity must be a whole number greater than zero.")
    elif quantity > 1_000_000:
        errors.append("Quantity is unrealistically large (maximum 1,000,000).")

    required_by = parse_date(requirement.get("required_by"))
    if required_by is None:
        errors.append("Required delivery date must be a valid date (YYYY-MM-DD).")
    elif required_by <= today:
        errors.append("Required delivery date must be in the future.")

    budget = requirement.get("max_budget")
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget <= 0:
        errors.append("Maximum budget must be a positive number.")

    return errors


def format_inr(amount: float | int | None) -> str:
    """Format a number as Indian Rupees, e.g. 93000 -> '₹93,000'."""
    if amount is None:
        return "-"
    if float(amount).is_integer():
        return f"₹{int(amount):,}"
    return f"₹{amount:,.2f}"

"""
Testing several creative concepts in one campaign (PRD: explore → validate → scale).

Each concept is a genuinely different reason to buy, not a reworded copy of the same
ad. They launch as one Meta campaign with one ad set per concept and equal budgets
(ABO), so each gets a fair chance before any is judged. How many a budget can carry
comes from growth.stages.recommend_structure.
"""
from __future__ import annotations

from typing import Optional

from .growth.stages import recommend_structure

ANGLES = [
    ("problem", "Lead with the problem the customer has and show this as the fix."),
    ("offer", "Lead with the offer itself: what they get and the real price if one is given."),
    ("outcome", "Lead with the result or feeling the customer gets after buying."),
]

MAX = len(ANGLES)


def max_concepts(budget_ngn: float, days: int, target_ngn: Optional[float] = None) -> int:
    """How many concepts this budget can test, by Meta's daily floor and (with a target)
    the spend each needs before it can be judged."""
    days = max(1, int(days or 1))
    rec = recommend_structure(budget_ngn / days, days, target_ngn)
    return max(1, min(MAX, int(rec.get("concepts") or 1)))


def brief_for(description: str, angle: str) -> str:
    """The campaign description with this concept's angle added for the writer."""
    for key, line in ANGLES:
        if key == angle:
            return f"{description.strip()}\n\nCreative angle for this version: {line}"
    return description

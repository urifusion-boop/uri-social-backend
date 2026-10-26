"""Small, dependency-free statistics shared by detection, creative tests and
evaluation. Every helper refuses to answer on too little data rather than guess."""
from __future__ import annotations

import math
from typing import Iterable, Optional

METRIC_KEYS = ("spend_ngn", "impressions", "reach", "clicks", "conversations")


def totals(rows: Iterable[dict]) -> dict:
    out = {k: 0.0 for k in METRIC_KEYS}
    days = set()
    for r in rows:
        for k in METRIC_KEYS:
            out[k] += float(r.get(k) or 0)
        if r.get("date"):
            days.add(r["date"])
    out["days"] = len(days)
    return derive(out)


def derive(t: dict) -> dict:
    """Ratios from summed counts — never an average of daily ratios, which weights a
    ₦50 day the same as a ₦50,000 one."""
    t = dict(t)
    imp, clicks, conv, spend = t.get("impressions", 0), t.get("clicks", 0), \
        t.get("conversations", 0), t.get("spend_ngn", 0)
    t["ctr"] = clicks / imp if imp else None
    t["cpc_ngn"] = spend / clicks if clicks else None
    t["cpm_ngn"] = spend / imp * 1000 if imp else None
    t["cost_per_conversation_ngn"] = spend / conv if conv else None
    t["conversion_rate"] = conv / clicks if clicks else None
    t["frequency"] = imp / t["reach"] if t.get("reach") else None
    return t


def ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or not b:
        return None
    return a / b


def two_proportion_p(success_a: float, n_a: float, success_b: float, n_b: float) -> Optional[float]:
    """Two-sided p-value for "these two rates differ". None when either side is too
    small for the normal approximation to mean anything."""
    if min(n_a, n_b) < 100 or min(success_a, success_b, n_a - success_a, n_b - success_b) < 5:
        return None
    p_pool = (success_a + success_b) / (n_a + n_b)
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b))
    if not se:
        return None
    z = abs(success_a / n_a - success_b / n_b) / se
    return math.erfc(z / math.sqrt(2))


def confidence_label(evidence_units: float, strong: float, moderate: float) -> str:
    """'high' / 'medium' / 'low' from how much evidence backs a finding (conversions,
    clicks or impressions — the caller picks the unit that matters)."""
    if evidence_units >= strong:
        return "high"
    if evidence_units >= moderate:
        return "medium"
    return "low"


def mean_sd(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    m = sum(values) / len(values)
    if len(values) < 2:
        return m, 0.0
    return m, math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))

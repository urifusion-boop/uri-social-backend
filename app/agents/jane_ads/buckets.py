"""
Jane + Ads — bucket queries, with the sample-size rules enforced in code.

CI-SPEC-01 §2.4 sets what a bucket's size licenses: 10 campaigns to observe
internally, 30 to bias Jane's defaults, 50 to state to a client as fact. §2.4 is also
explicit that these must be enforced at the QUERY LAYER, not by convention — "a claim
below threshold should be impossible to surface, not merely discouraged".

So a thin bucket cannot return a ranking here. Not a ranking with a warning attached,
not a ranking a caller is trusted to suppress: `compare()` returns counts and nothing
else below the floor, because a pattern from six campaigns is noise wearing the
authority of local evidence, and a chart is exactly what makes it look like evidence.

Exploration campaigns (§2.5) are excluded from anything that sets a default while
staying in the measurement, which is the only way to tell a deliberate variation from
a failure.
"""
from __future__ import annotations

from typing import Any, Optional

from .campaign_record import RECORDS

# §2.4. Names, not bare numbers, so a caller reads what a count licenses.
OBSERVE_MIN = 10      # internal observation
BIAS_MIN = 30         # may bias Jane's defaults
CLAIM_MIN = 50        # may be stated to a client as fact

PRIMARY_KEYS = ("business_category", "city", "budget_tier", "platform")


def threshold_state(n: int) -> str:
    """What a sample of this size is allowed to be used for."""
    if n >= CLAIM_MIN:
        return "claimable"
    if n >= BIAS_MIN:
        return "may_bias_defaults"
    if n >= OBSERVE_MIN:
        return "observation_only"
    return "insufficient"


def _median(values: list[float]) -> Optional[float]:
    clean = sorted(v for v in values if isinstance(v, (int, float)))
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        return round(float(clean[mid]), 2)
    return round((clean[mid - 1] + clean[mid]) / 2, 2)


async def load_bucket(db, *, include_exploration: bool = True, **keys) -> list[dict]:
    """Every record matching the given bucket keys.

    Unknown keys are ignored rather than silently widening the bucket to everything,
    which would inflate a count and so inflate what it licenses.
    """
    if db is None:
        return []
    query: dict[str, Any] = {}
    for key, value in keys.items():
        if value in (None, "") or key not in PRIMARY_KEYS:
            continue
        query[f"context.{key}"] = str(value).strip().lower()
    if not include_exploration:
        query["exploration"] = {"$ne": True}
    try:
        return await db[RECORDS].find(query, {"_id": 0}).to_list(length=5000)
    except Exception as e:
        print(f"[Buckets] query failed: {e}", flush=True)
        return []


def _cost_per_conversation(record: dict) -> Optional[float]:
    results = record.get("results") or {}
    cost = results.get("cost_per_conversation_ngn")
    return float(cost) if isinstance(cost, (int, float)) else None


def compare(records: list[dict], dimension: str) -> dict:
    """Break a bucket down by one dimension — but only if it is big enough.

    BELOW THE FLOOR THIS RETURNS NO RANKING AT ALL. §2.4 and acceptance criterion 9
    require that a sparse bucket cannot render rankings or medians: returning them
    with a warning still puts a chart on screen, and a chart is what invites the
    over-reading the threshold exists to prevent.
    """
    n = len(records)
    state = threshold_state(n)
    out: dict[str, Any] = {
        "dimension": dimension,
        "campaigns": n,
        "threshold_state": state,
        "thresholds": {"observe": OBSERVE_MIN, "bias": BIAS_MIN, "claim": CLAIM_MIN},
        "rows": [],
    }
    if state == "insufficient":
        out["message"] = (
            f"{n} campaigns — too few to compare. "
            f"{OBSERVE_MIN} are needed before any ranking is shown."
        )
        return out

    groups: dict[str, list[dict]] = {}
    for record in records:
        value = _dimension_value(record, dimension)
        if value:
            groups.setdefault(value, []).append(record)

    rows = []
    for value, group in groups.items():
        costs = [c for c in (_cost_per_conversation(r) for r in group) if c is not None]
        rows.append({
            "value": value,
            "campaigns": len(group),
            "median_cost_per_conversation_ngn": _median(costs),
            "with_results": len(costs),
            # A single group can be thin inside an otherwise adequate bucket, and
            # ranking on one campaign is the same error at a smaller scale.
            "sufficient": len(group) >= OBSERVE_MIN,
        })
    rows.sort(key=lambda r: (r["median_cost_per_conversation_ngn"] is None,
                             r["median_cost_per_conversation_ngn"] or 0))
    out["rows"] = rows
    return out


def _dimension_value(record: dict, dimension: str) -> str:
    if dimension == "creative_format":
        return (record.get("creative") or {}).get("format_id") or "none"
    if dimension == "asset_source":
        return (record.get("creative") or {}).get("asset_source") or ""
    if dimension == "media_type":
        return (record.get("creative") or {}).get("media_type") or ""
    if dimension == "geo_strategy":
        return (record.get("strategy") or {}).get("geo_strategy") or ""
    if dimension == "purchase_behaviour":
        return (record.get("context") or {}).get("purchase_behaviour") or ""
    return ""


def headline_metrics(records: list[dict]) -> dict:
    """The bucket's own summary — the figures §4.2 puts above the breakdown.

    Repeat rate and budget-on-repeat are the honest ones: they need no integration,
    cannot be gamed, and a client who ran ₦15k then came back at ₦30k has said
    something no survey would get.
    """
    n = len(records)
    costs = [c for c in (_cost_per_conversation(r) for r in records) if c is not None]

    by_brand: dict[str, list[dict]] = {}
    for r in records:
        by_brand.setdefault(r.get("brand_id") or "", []).append(r)
    repeat_brands = [rs for rs in by_brand.values() if len(rs) > 1]

    raised = 0
    for runs in repeat_brands:
        ordered = sorted(runs, key=lambda r: r.get("created_at") or "")
        first = ((ordered[0].get("budget") or {}).get("stated_ngn")) or 0
        last = ((ordered[-1].get("budget") or {}).get("stated_ngn")) or 0
        if last > first:
            raised += 1

    accepted = [r for r in records if not (r.get("modifications") or [])]
    diverged = [r for r in records if (r.get("strategy") or {}).get("recommendation_diverged")]

    return {
        "campaigns": n,
        "threshold_state": threshold_state(n),
        "median_cost_per_conversation_ngn": _median(costs),
        "with_results": len(costs),
        "repeat_rate": round(len(repeat_brands) / len(by_brand), 3) if by_brand else None,
        "raised_budget_on_repeat": raised,
        # How often Jane was right first time — no client edits at all.
        "plan_acceptance_rate": round(len(accepted) / n, 3) if n else None,
        # How often the client took a plan other than the one she recommended.
        "recommendation_divergence_rate": round(len(diverged) / n, 3) if n else None,
        "exploration_share": round(
            sum(1 for r in records if r.get("exploration")) / n, 3) if n else None,
    }

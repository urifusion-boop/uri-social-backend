"""
Explore → Validate → Scale (Meta ads structures memo, Parts 10–13). Pure.

URI's own stages, not Meta campaign types:

  explore   find which reasons to buy show promise — a few genuinely different
            concepts, each given enough spend to be judged
  validate  a concept looks promising: test variations of it before betting on it
  scale     a creative has held its cost over enough results, on several days: put
            money behind it

Every creative gets a verdict against the business's own target cost — strong,
promising, underperforming or inconclusive. Inconclusive is a real answer: ₦500
spent without an order says nothing when an order normally costs ₦2,000, and the
absence of evidence is never reported as evidence of failure.

Where the business records what happened to its leads, creatives are judged on cost
per order rather than cost per conversation: the cheapest enquiries are not always
the cheapest sales.
"""
from __future__ import annotations

import math
from typing import Optional

from .. import constants as C

PROMISING = "promising"
STRONG = "strong"
UNDERPERFORMING = "underperforming"
INCONCLUSIVE = "inconclusive"

EXPLORE, VALIDATE, SCALE = "explore", "validate", "scale"

# A creative is only called underperforming once it has spent this many times the
# target cost: at 2.5× the target, a creative performing on target would usually
# have produced two or three results.
JUDGE_SPEND_MULTIPLE = 2.5
UNDERPERFORM_COST_MULTIPLE = 1.5
MIN_RESULTS_TO_JUDGE = 3
VALIDATED_RESULTS = 10
# Repeatable, not a spike: results on at least this many separate days.
STRONG_MIN_DAYS = 3
MAX_CONCEPTS = 5
# Orders only replace conversations as the yardstick once enough leads have a
# recorded outcome to say something.
MIN_LEADS_WITH_OUTCOME = 5


def _ngn(v: float) -> str:
    return f"₦{v:,.0f}"


def yardstick(conversations: float, leads: Optional[dict], goal: dict) -> dict:
    """What a creative is judged on: orders when the business records them and has
    a target per order, conversations otherwise."""
    leads = leads or {}
    orders_target = goal.get("target_cost_per_order_ngn")
    if orders_target and leads.get("with_outcome", 0) >= MIN_LEADS_WITH_OUTCOME:
        return {"metric": "orders", "results": float((leads.get("by_stage") or {}).get("converted", 0)),
                "target_ngn": float(orders_target)}
    return {"metric": "conversations", "results": float(conversations),
            "target_ngn": float(goal["target_cpl_ngn"]) if goal.get("target_cpl_ngn") else None}


def judge(spend_ngn: float, results: float, target_ngn: Optional[float], metric: str = "conversations",
          days_with_results: Optional[int] = None) -> dict:
    """One creative's verdict against the target cost per result. Strong means it has
    held the target over enough results AND on several separate days."""
    noun = "order" if metric == "orders" else "conversation"
    cost = spend_ngn / results if results else None
    base = {"spend_ngn": round(spend_ngn, 2), "results": results, "metric": metric,
            "cost_per_result_ngn": round(cost, 2) if cost else None, "target_ngn": target_ngn,
            "spend_to_judge_ngn": None}
    if not target_ngn:
        return {**base, "status": INCONCLUSIVE, "confidence": "low",
                "reason": f"No target cost per {noun} is set, so there is nothing to judge it against."}
    judge_at = target_ngn * JUDGE_SPEND_MULTIPLE
    if results == 0:
        if spend_ngn >= judge_at:
            return {**base, "status": UNDERPERFORMING, "confidence": "medium",
                    "reason": f"{_ngn(spend_ngn)} spent with no {noun}s, against a {_ngn(target_ngn)} target."}
        return {**base, "status": INCONCLUSIVE, "confidence": "low",
                "spend_to_judge_ngn": round(judge_at - spend_ngn, 2),
                "reason": f"{_ngn(spend_ngn)} spent and no {noun} yet — unsurprising at a "
                          f"{_ngn(target_ngn)} target. It needs about {_ngn(judge_at)} before it can be judged."}
    confidence = "high" if results >= VALIDATED_RESULTS else "medium" if results >= MIN_RESULTS_TO_JUDGE else "low"
    if cost <= target_ngn and results >= VALIDATED_RESULTS and (days_with_results or 0) >= STRONG_MIN_DAYS:
        return {**base, "status": STRONG, "confidence": "high",
                "reason": f"{_ngn(cost)} per {noun} from {results:.0f} across {days_with_results} days, "
                          f"within the {_ngn(target_ngn)} target — repeatable, not a one-off."}
    if cost <= target_ngn:
        early = "" if results >= MIN_RESULTS_TO_JUDGE else " Early: too few results to rely on yet."
        return {**base, "status": PROMISING, "confidence": confidence,
                "reason": f"{_ngn(cost)} per {noun} from {results:.0f}, within the {_ngn(target_ngn)} target.{early}"}
    if cost > target_ngn * UNDERPERFORM_COST_MULTIPLE and spend_ngn >= judge_at:
        return {**base, "status": UNDERPERFORMING, "confidence": confidence,
                "reason": f"{_ngn(cost)} per {noun} from {results:.0f}, well above the {_ngn(target_ngn)} target."}
    return {**base, "status": INCONCLUSIVE, "confidence": "low",
            "spend_to_judge_ngn": round(max(0.0, judge_at - spend_ngn), 2) or None,
            "reason": f"{_ngn(cost)} per {noun} from {results:.0f} — near the {_ngn(target_ngn)} target, "
                      "not clearly better or worse yet."}


def stage(verdicts: list[dict], has_target: bool) -> dict:
    """Where the campaign is in the loop, and the one next step that moves it on."""
    proven = [v for v in verdicts if v["status"] == STRONG]
    promising = [v for v in verdicts if v["status"] == PROMISING]
    weak = [v for v in verdicts if v["status"] == UNDERPERFORMING]
    if not has_target:
        return {"stage": EXPLORE, "why": "No target cost is set yet.",
                "next": "Set the most you'd pay for a result, so URI can tell promising creatives "
                        "from underperforming ones."}
    if proven:
        n = len(proven)
        return {"stage": SCALE, "proven_ad_ids": [v["ad_id"] for v in proven],
                "why": f"{n} creative{'s have' if n > 1 else ' has'} held the target over "
                       f"{VALIDATED_RESULTS}+ results on {STRONG_MIN_DAYS}+ separate days.",
                "next": "Raise the budget in steps of about 20% a week while the cost holds, and keep "
                        "testing new variations so there is a replacement when this one tires."}
    if promising:
        best = min(promising, key=lambda v: v["cost_per_result_ngn"] or float("inf"))
        concept = (best.get("attributes") or {}).get("concept")
        return {"stage": VALIDATE, "promising_ad_ids": [v["ad_id"] for v in promising],
                "why": "A creative is beating the target, but on too few results to bet on.",
                "next": f"Test 2–3 variations of {'the ' + concept + ' concept' if concept else 'the leading creative'}"
                        " — a new hook, format or offer — and keep it running to see whether it holds."}
    pending = [v for v in verdicts if v["status"] == INCONCLUSIVE and v.get("spend_to_judge_ngn")]
    if weak and not pending:
        return {"stage": EXPLORE, "why": "Every creative judged so far is above the target.",
                "next": "Pause the underperformers and test a different reason to buy, or revisit the offer."}
    more = sum(v["spend_to_judge_ngn"] for v in pending)
    return {"stage": EXPLORE, "why": "Not enough evidence yet to call any creative promising.",
            "next": f"Keep the test running — about {_ngn(more)} more spend lets each creative be judged."
            if more else "Keep the test running until each creative has enough results to judge."}


def recommend_structure(daily_budget_ngn: float, days: int, target_ngn: Optional[float] = None,
                        concepts_available: Optional[int] = None, proven: int = 0) -> dict:
    """How many concepts this budget can genuinely test, and how to lay them out on Meta.

    Each concept needs Meta's daily floor to deliver at all, and enough total spend to
    be judged (JUDGE_SPEND_MULTIPLE × the target). A budget that can't judge even one
    concept in the time says so rather than pretending a test is affordable."""
    days = max(1, int(days))
    floor = C.MIN_DAILY_SPEND_NGN
    by_delivery = max(1, min(MAX_CONCEPTS, int(daily_budget_ngn // floor)))
    total = daily_budget_ngn * days
    out = {"daily_budget_ngn": daily_budget_ngn, "days": days, "target_ngn": target_ngn,
           "max_by_daily_floor": by_delivery, "spend_to_judge_per_concept_ngn": None,
           "days_to_judge": None, "budget_too_small": False}

    if proven >= 2:
        return {**out, "concepts": proven, "structure": "cbo",
                "why": "Several creatives are proven: one campaign budget lets Meta move spend to "
                       "whichever is doing best that day."}

    n = by_delivery
    if target_ngn:
        need = target_ngn * JUDGE_SPEND_MULTIPLE
        out["spend_to_judge_per_concept_ngn"] = round(need, 2)
        by_evidence = int(total // need)
        if by_evidence < 1:
            out["budget_too_small"] = True
            out["days_to_judge"] = math.ceil(need / daily_budget_ngn) if daily_budget_ngn else None
            return {**out, "concepts": 1, "structure": "single",
                    "why": f"{_ngn(total)} over {days} days can't judge even one creative at a "
                           f"{_ngn(target_ngn)} target — that takes about {_ngn(need)}. Run one concept "
                           f"for {out['days_to_judge']} days, or treat early results as a signal only."}
        n = min(n, by_evidence)
    if concepts_available:
        n = min(n, max(1, concepts_available))
    if target_ngn:
        out["days_to_judge"] = math.ceil(out["spend_to_judge_per_concept_ngn"] * n / daily_budget_ngn)

    if n >= 2:
        return {**out, "concepts": n, "structure": "abo",
                "per_concept_daily_ngn": round(daily_budget_ngn / n, 2),
                "why": f"{n} genuinely different concepts, each in its own ad set with an equal "
                       f"{_ngn(daily_budget_ngn / n)} a day, so each gets a fair chance to show results."}
    return {**out, "concepts": 1, "structure": "single",
            "why": "The budget supports one concept at a time: one ad set, so the money isn't split "
                   "too thinly to learn anything."}


def experiment(record: dict, ad_id: str, verdict: dict, attributes: dict, campaign_stage: str) -> dict:
    """What one creative taught us, in a shape that can be compared across campaigns."""
    return {"brand_id": record.get("brand_id"), "campaign_id": record["campaign_id"], "ad_id": ad_id,
            "objective": record.get("objective") or record.get("goal") or "",
            "category": record.get("category") or "",
            "attributes": attributes or {}, "stage": campaign_stage,
            **{k: verdict.get(k) for k in ("status", "confidence", "reason", "metric", "spend_ngn",
                                           "results", "cost_per_result_ngn", "target_ngn")}}

"""
Anomaly detection (PRD §4.3). Pure: snapshots and context in, findings out.

Each rule compares the recent window (the last RECENT_DAYS complete days) with the
baseline before it, and only fires once there is enough delivery for the change to
mean something. A rule that lacks the data says nothing — a quiet detector is
better than one that pauses a campaign over three clicks.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from .stats import derive, ratio, totals, two_proportion_p

RECENT_DAYS = 3
BASELINE_DAYS = 7
STALE_AFTER = timedelta(hours=6)

# Minimum delivery before each rule may speak.
MIN_IMPRESSIONS_NO_RESULTS = 1000
MIN_BASELINE_CONVERSATIONS = 5
MIN_IMPRESSIONS_CTR = 2000
MIN_CLICKS_FUNNEL = 30
MIN_LEADS_WITH_OUTCOME = 8

COST_RISE = 1.4            # recent cost per result ≥ 1.4× baseline
CTR_DROP = 0.7             # recent CTR ≤ 0.7× baseline
FATIGUE_FREQUENCY = 3.0
WEAK_CONVERSION_RATE = 0.02
LOW_QUALIFIED_RATE = 0.25
STRONG_VARIANT_GAP = 0.6   # best ad's cost per result ≤ 0.6× the rest


def windows(rows: list[dict], today: date) -> tuple[list[dict], list[dict], list[str], list[str]]:
    """(recent rows, baseline rows, recent dates, baseline dates). Today is excluded:
    a half-reported day reads as a collapse at every metric."""
    end = today - timedelta(days=1)
    recent_d = [(end - timedelta(days=i)).isoformat() for i in range(RECENT_DAYS)]
    base_end = end - timedelta(days=RECENT_DAYS)
    base_d = [(base_end - timedelta(days=i)).isoformat() for i in range(BASELINE_DAYS)]
    recent = [r for r in rows if r.get("date") in recent_d]
    base = [r for r in rows if r.get("date") in base_d]
    return recent, base, sorted(recent_d), sorted(base_d)


def _finding(issue: str, severity: str, evidence: dict, level: str = "campaign",
             entity_id: str = "", **extra: Any) -> dict:
    return {"issue_type": issue, "severity": severity, "level": level,
            "entity_id": entity_id, "evidence": evidence, **extra}


def _round(t: dict) -> dict:
    keep = ("spend_ngn", "impressions", "clicks", "conversations", "ctr", "cpc_ngn",
            "cost_per_conversation_ngn", "conversion_rate", "frequency", "days")
    out = {}
    for k in keep:
        v = t.get(k)
        out[k] = round(v, 4) if isinstance(v, float) else v
    return out


def detect(rows: list[dict], ctx: dict) -> list[dict]:
    """ctx keys: today (date), status ('ACTIVE'…), retrieved_at (datetime|None),
    fetch_error (str), daily_budget_ngn, target_cpl_ngn, leads (from leads.stats),
    landing (from a page check), followups (from leads.follow_up_gaps)."""
    today: date = ctx.get("today") or datetime.now(timezone.utc).date()
    out: list[dict] = []

    # Data first: nothing below is safe on stale or failing data.
    retrieved = ctx.get("retrieved_at")
    if ctx.get("fetch_error") or not retrieved or \
            datetime.now(timezone.utc) - retrieved > STALE_AFTER:
        out.append(_finding("tracking_or_api", "high", {
            "fetch_error": ctx.get("fetch_error") or "",
            "last_successful_retrieval": retrieved.isoformat() if retrieved else None,
        }, blocks_actions=True))
        return out

    recent_rows, base_rows, recent_d, base_d = windows(rows, today)
    recent, base = totals(recent_rows), totals(base_rows)
    win = {"recent": [recent_d[0], recent_d[-1]], "baseline": [base_d[0], base_d[-1]]}
    active = (ctx.get("status") or "").upper() == "ACTIVE"

    if active and ctx.get("daily_budget_ngn") and recent["impressions"] == 0:
        yesterday = totals([r for r in rows if r.get("date") == recent_d[-1]])
        if yesterday["impressions"] == 0:
            out.append(_finding("delivery_stalled", "high",
                                {"recent": _round(recent), "daily_budget_ngn": ctx["daily_budget_ngn"]},
                                window=win))

    # Spend with nothing to show for it.
    if recent["spend_ngn"] > 0 and recent["conversations"] == 0 \
            and recent["impressions"] >= MIN_IMPRESSIONS_NO_RESULTS:
        expected_cpl = ctx.get("target_cpl_ngn") or base.get("cost_per_conversation_ngn")
        if not expected_cpl or recent["spend_ngn"] >= 2 * expected_cpl:
            out.append(_finding("spend_no_results", "high",
                                {"recent": _round(recent), "baseline": _round(base),
                                 "expected_cost_per_result_ngn": expected_cpl,
                                 "benchmark": "target" if ctx.get("target_cpl_ngn") else
                                 ("baseline" if expected_cpl else "none (provisional)")},
                                window=win))

    # Cost per result rising against its own past.
    base_cpl, rec_cpl = base.get("cost_per_conversation_ngn"), recent.get("cost_per_conversation_ngn")
    if base["conversations"] >= MIN_BASELINE_CONVERSATIONS and rec_cpl and base_cpl \
            and rec_cpl >= COST_RISE * base_cpl and recent["conversations"] >= 3:
        out.append(_finding("cost_rise", "medium",
                            {"recent": _round(recent), "baseline": _round(base),
                             "change": round(rec_cpl / base_cpl - 1, 3)}, window=win))

    # CTR sliding: fatigue or a message that no longer lands.
    if recent["impressions"] >= MIN_IMPRESSIONS_CTR and base["impressions"] >= MIN_IMPRESSIONS_CTR \
            and recent["ctr"] is not None and base["ctr"] and recent["ctr"] <= CTR_DROP * base["ctr"]:
        p = two_proportion_p(recent["clicks"], recent["impressions"], base["clicks"], base["impressions"])
        if p is None or p < 0.05:
            out.append(_finding("ctr_decline", "medium",
                                {"recent": _round(recent), "baseline": _round(base),
                                 "change": round(recent["ctr"] / base["ctr"] - 1, 3),
                                 "frequency": round(recent["frequency"] or 0, 2),
                                 "fatigue_likely": (recent["frequency"] or 0) >= FATIGUE_FREQUENCY,
                                 "p_value": round(p, 4) if p is not None else None},
                                window=win))

    # People click, few start a conversation.
    if recent["clicks"] >= MIN_CLICKS_FUNNEL and (recent["conversion_rate"] or 0) < WEAK_CONVERSION_RATE:
        out.append(_finding("clicks_no_conversions", "medium",
                            {"recent": _round(recent), "landing": ctx.get("landing") or {}},
                            window=win))

    landing = ctx.get("landing") or {}
    if landing.get("checked") and not landing.get("ok"):
        out.append(_finding("landing_page", "high", landing))

    leads = ctx.get("leads") or {}
    with_outcome = leads.get("with_outcome", 0)
    if with_outcome >= MIN_LEADS_WITH_OUTCOME and \
            (leads.get("qualified_rate") or 0) < LOW_QUALIFIED_RATE:
        out.append(_finding("low_lead_quality", "medium", {"leads": leads}))

    # Meta reports no conversations but the inbox has people who came from this ad.
    if recent["clicks"] > 0 and recent["conversations"] == 0 and leads.get("recent_from_ads", 0) >= 3:
        out.append(_finding("tracking_mismatch", "high",
                            {"meta_conversations": 0, "inbox_conversations_from_ads": leads["recent_from_ads"]},
                            blocks_actions=True, window=win))

    followups = ctx.get("followups") or {}
    if followups.get("waiting", 0) >= 1:
        out.append(_finding("follow_up_gap", "high" if followups["waiting"] >= 3 else "medium",
                            followups))

    # Cheap results and the budget is the limit: room to scale (or to finish sooner).
    target = ctx.get("target_cpl_ngn") or base_cpl
    budget = ctx.get("daily_budget_ngn") or 0
    if active and target and rec_cpl and recent["conversations"] >= 10 and budget \
            and rec_cpl <= 0.7 * target and recent["spend_ngn"] >= 0.9 * budget * RECENT_DAYS:
        out.append(_finding("scale_opportunity", "low",
                            {"recent": _round(recent), "benchmark_cost_ngn": round(target, 2),
                             "benchmark": "target" if ctx.get("target_cpl_ngn") else "baseline",
                             "daily_budget_ngn": budget}, window=win))

    out.extend(_variant_findings(recent_rows + base_rows))
    return out


def _variant_findings(rows: list[dict]) -> list[dict]:
    """A clearly stronger ad among several — only when both sides have delivered
    enough and the gap holds up statistically."""
    by_ad: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("ad_id"):
            by_ad.setdefault(r["ad_id"], []).append(r)
    if len(by_ad) < 2:
        return []
    stats = {ad: totals(rs) for ad, rs in by_ad.items()}
    eligible = {a: s for a, s in stats.items()
                if s["conversations"] >= 5 and s["impressions"] >= 1000}
    if len(eligible) < 2:
        return []
    best = min(eligible, key=lambda a: eligible[a]["cost_per_conversation_ngn"] or float("inf"))
    rest = derive({k: sum(s[k] for a, s in eligible.items() if a != best)
                   for k in ("spend_ngn", "impressions", "reach", "clicks", "conversations")})
    b = eligible[best]
    gap = ratio(b["cost_per_conversation_ngn"], rest["cost_per_conversation_ngn"])
    p = two_proportion_p(b["conversations"], b["impressions"], rest["conversations"], rest["impressions"])
    if gap is None or gap > STRONG_VARIANT_GAP or p is None or p >= 0.05:
        return []
    worst = max((a for a in eligible if a != best),
                key=lambda a: eligible[a]["cost_per_conversation_ngn"] or 0)
    return [_finding("strong_variant", "low",
                     {"best_ad": best, "best": _round(b), "rest": _round(rest),
                      "weakest_ad": worst, "weakest": _round(eligible[worst]),
                      "cost_ratio": round(gap, 3), "p_value": round(p, 4)},
                     level="ad", entity_id=best)]


def fingerprint(campaign_id: str, finding: dict) -> str:
    """One open diagnosis per campaign, issue and entity — a rule that keeps firing
    updates its evidence rather than stacking duplicates."""
    return f"{campaign_id}:{finding['issue_type']}:{finding.get('entity_id') or ''}"


def latest_date(rows: list[dict]) -> Optional[str]:
    return max((r["date"] for r in rows if r.get("date")), default=None)


def totals_for_ui(rows: list[dict]) -> dict:
    return _round(totals(rows))

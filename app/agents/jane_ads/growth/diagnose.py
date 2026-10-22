"""
Diagnosis and recommendation (PRD §4.4, §4.5). Pure: a finding becomes a
diagnosis — likely causes as labelled hypotheses with a confidence, the other
explanations still open, and the smallest useful action that tests the leading one.

Executable actions (the executor can run them on Meta):
  pause_ad, pause_campaign, resume_campaign, set_daily_budget
Everything else is advisory — the business does it, then marks it done:
  observe, refresh_creative, review_offer, fix_tracking, fix_landing_page,
  follow_up_leads, review_targeting
"""
from __future__ import annotations

from typing import Optional

from .stats import confidence_label

EXECUTABLE = ("pause_ad", "resume_ad", "pause_campaign", "resume_campaign", "set_daily_budget")
ADVISORY = ("observe", "refresh_creative", "review_offer", "fix_tracking",
            "fix_landing_page", "follow_up_leads", "review_targeting")

ISSUE_LABELS = {
    "tracking_or_api": "Data from Meta is missing or out of date",
    "delivery_stalled": "The campaign isn't delivering",
    "spend_no_results": "Spending with no conversations",
    "cost_rise": "Cost per conversation is rising",
    "ctr_decline": "Fewer people are clicking",
    "clicks_no_conversions": "Clicks, but few conversations",
    "landing_page": "The landing page has a problem",
    "low_lead_quality": "Many leads, few qualified",
    "tracking_mismatch": "Meta and your inbox disagree on results",
    "follow_up_gap": "Leads are waiting for a reply",
    "strong_variant": "One ad is clearly outperforming the others",
    "scale_opportunity": "Results are cheap and the budget is the limit",
    "daily_incomplete": "Meta's day-by-day figures don't add up",
}

# Evaluation windows: long enough for results and attribution to land.
EVAL_DAYS = {"pause_ad": 3, "pause_campaign": 1, "resume_campaign": 3, "set_daily_budget": 4,
             "refresh_creative": 5, "review_offer": 7, "fix_tracking": 2, "fix_landing_page": 3,
             "follow_up_leads": 3, "review_targeting": 5, "observe": 2}


def _h(cause: str, confidence: str, why: str) -> dict:
    return {"cause": cause, "confidence": confidence, "why": why, "is_hypothesis": True}


def _rec(action: str, summary: str, expected: str, risk: str, success_metric: str,
         params: Optional[dict] = None) -> dict:
    return {"action_type": action, "summary": summary, "parameters": params or {},
            "expected_goal": expected, "risk": risk,
            "reversible": action in ("pause_ad", "pause_campaign", "resume_campaign",
                                     "set_daily_budget") or action in ADVISORY,
            "executable": action in EXECUTABLE,
            "success_metric": success_metric, "eval_window_days": EVAL_DAYS.get(action, 3)}


def diagnose(finding: dict, ctx: dict) -> dict:
    """ctx: daily_budget_ngn, adset_id, ad_ids, policy (UserPolicy), history
    (learning.summary for this brand), market (recent market signals)."""
    issue, ev = finding["issue_type"], finding.get("evidence") or {}
    recent = ev.get("recent") or {}
    policy = ctx.get("policy") or {}
    hypotheses: list[dict] = []
    alternatives: list[str] = []
    recs: list[dict] = []

    if issue == "tracking_or_api":
        hypotheses = [_h("Meta's reporting API failed or is delayed", "high",
                         ev.get("fetch_error") or "No successful data pull in the last 6 hours.")]
        alternatives = ["Meta's reporting can lag by several hours on its own."]
        recs = [_rec("fix_tracking", "Check the ad account connection; no changes run until data is fresh.",
                     "Fresh data before any decision", "None — nothing is changed.",
                     "A successful data pull")]

    elif issue == "daily_incomplete":
        hypotheses = [_h("Meta's daily breakdown for this campaign is incomplete", "high",
                         f"Lifetime spend ₦{ev.get('lifetime_spend_ngn') or 0:,.0f}, but the days Meta "
                         f"reports add up to ₦{ev.get('daily_spend_ngn') or 0:,.0f}.")]
        alternatives = ["Meta may still be processing recent days."]
        recs = [_rec("observe", "Totals use Meta's lifetime figures. Trend checks and automatic changes "
                     "wait until the daily figures add up.", "Decisions only on data that adds up",
                     "None — nothing is changed.", "Daily figures matching the lifetime total")]

    elif issue == "delivery_stalled":
        hypotheses = [_h("The ad was rejected, the budget or schedule ran out, or the account has a "
                         "payment/limit issue", "medium", "No impressions yesterday despite an active status.")]
        alternatives = ["Meta is still reviewing the ad.", "The audience is too narrow to deliver."]
        recs = [_rec("review_targeting", "Check the ad's review status and the account's payment and spend "
                     "limit in Ads Manager; widen the audience if it's very narrow.",
                     "Delivery restarts", "Low", "Impressions over the next 2 days")]

    elif issue == "spend_no_results":
        spend = recent.get("spend_ngn") or 0
        conf = confidence_label(recent.get("impressions") or 0, 8000, 3000)
        hypotheses = [
            _h("The creative or offer isn't prompting people to message", conf,
               f"₦{spend:,.0f} spent and {recent.get('impressions', 0):,} people reached with no conversations."),
            _h("Conversation tracking isn't counting messages", "low",
               "Check whether messages actually arrived in your inbox."),
        ]
        alternatives = ["The sample is still small — the first conversations can take a few days.",
                        "The audience may be wrong for the offer."]
        min_spend = policy.get("min_spend_before_pause_ngn", 0)
        if spend >= min_spend and conf != "low":
            recs.append(_rec("pause_campaign", "Pause the campaign until the creative or offer is changed.",
                             "Stop spend that isn't producing conversations",
                             "Low — reversible, but delivery learning restarts when resumed.",
                             "Spend avoided; conversations after the fix"))
        recs.append(_rec("refresh_creative", "Try a new image and a clearer call to message (one change at a time).",
                         "First conversations", "Low", "Conversations per ₦1,000 spent"))

    elif issue == "cost_rise":
        base = ev.get("baseline") or {}
        conf = confidence_label(base.get("conversations") or 0, 30, 10)
        fatigue = (recent.get("frequency") or 0) >= 3
        hypotheses = [
            _h("Creative fatigue — the same people are seeing the ad repeatedly", "medium" if fatigue else "low",
               f"Frequency is {recent.get('frequency') or 0:.1f}."),
            _h("The audience or auction got more competitive", conf,
               f"Cost per conversation rose {ev.get('change', 0) * 100:.0f}% against the previous week."),
        ]
        alternatives = ["Day-to-day noise on a small number of conversations.",
                        "A holiday or seasonal change in demand."]
        recs = [_rec("refresh_creative" if fatigue else "observe",
                     "Refresh the creative to reset fatigue." if fatigue else
                     "Watch two more days before changing anything — the rise may be noise.",
                     "Bring cost per conversation back toward the baseline", "Low",
                     "Cost per conversation vs. the baseline week")]

    elif issue == "ctr_decline":
        hypotheses = [_h("Creative fatigue" if ev.get("fatigue_likely") else "The message no longer matches "
                         "the audience", "medium" if ev.get("p_value") is not None else "low",
                         f"CTR fell {abs(ev.get('change', 0)) * 100:.0f}%; frequency {ev.get('frequency', 0)}.")]
        alternatives = ["Meta moved delivery to a different, less engaged part of the audience."]
        recs = [_rec("refresh_creative", "Test a new hook or image against the current ad.",
                     "Recover click-through rate", "Low", "CTR over the next 5 days")]

    elif issue == "clicks_no_conversions":
        landing = ev.get("landing") or {}
        hypotheses = [_h("Friction after the click — the chat or page doesn't make the next step obvious",
                         "medium", f"{recent.get('clicks', 0):,} clicks but only "
                         f"{recent.get('conversations', 0)} conversations."),
                      _h("Low-intent clicks from a broad audience", "low", "Common with very wide targeting.")]
        alternatives = ["Conversation tracking may be undercounting."]
        recs = [_rec("fix_landing_page" if landing.get("checked") else "review_offer",
                     "Check the destination opens quickly and asks one clear question.",
                     "More conversations per click", "Low", "Conversation rate per click")]

    elif issue == "landing_page":
        hypotheses = [_h("The landing page is down or slow", "high",
                         f"{ev.get('url', 'The page')}: " + (ev.get("error") or
                         f"status {ev.get('status')}, {ev.get('seconds', '?')}s to load") + ".")]
        recs = [_rec("fix_landing_page", "Fix the page, or pause the campaign until it's back.",
                     "Clicks land on a working page", "None", "Page check passes")]
        if ev.get("status") and ev.get("status") >= 400:
            recs.insert(0, _rec("pause_campaign", "Pause while the page is broken — clicks are wasted until it works.",
                                "Stop paying for clicks to a broken page", "Low — reversible.",
                                "Spend avoided"))

    elif issue == "low_lead_quality":
        leads = ev.get("leads") or {}
        hypotheses = [_h("The ad attracts people outside your target customer", "medium",
                         f"Only {(leads.get('qualified_rate') or 0) * 100:.0f}% of "
                         f"{leads.get('with_outcome', 0)} reviewed leads were qualified.")]
        alternatives = ["Leads are qualified but follow-up is slow."]
        recs = [_rec("review_targeting", "Narrow the audience or say who it's for in the ad text.",
                     "Higher share of qualified leads", "Medium — fewer leads overall.",
                     "Cost per qualified lead")]

    elif issue == "tracking_mismatch":
        hypotheses = [_h("Meta isn't attributing conversations the inbox received", "medium",
                         f"Inbox: {ev.get('inbox_conversations_from_ads')} from ads; Meta: 0.")]
        recs = [_rec("fix_tracking", "Check the ad's messaging destination and the Page connection.",
                     "Results reported correctly", "None — automated changes are held meanwhile.",
                     "Meta and inbox counts agree")]

    elif issue == "follow_up_gap":
        hypotheses = [_h("Leads from your ads are waiting on a reply", "high",
                         f"{ev.get('waiting')} waiting, oldest {ev.get('oldest_hours', 0):.0f}h.")]
        recs = [_rec("follow_up_leads", "Reply to the waiting leads in Customer Messages.",
                     "Fewer leads lost to slow replies", "None", "Reply time and conversion rate")]

    elif issue == "strong_variant":
        hypotheses = [_h("One ad's creative resonates better with this audience", "medium",
                         f"Its cost per conversation is {ev.get('cost_ratio', 0) * 100:.0f}% of the rest "
                         f"(p={ev.get('p_value')}).")]
        alternatives = ["Meta may have delivered the ads to different people."]
        recs = [_rec("pause_ad", "Pause the weakest ad so delivery shifts to the strongest.",
                     "Lower cost per conversation", "Low — reversible.",
                     "Campaign cost per conversation", params={"ad_id": ev.get("weakest_ad")})]

    elif issue == "scale_opportunity":
        budget = float(ev.get("daily_budget_ngn") or 0)
        pct = min(20, policy.get("max_increase_pct_7d", 20)) or 0
        hypotheses = [_h("The campaign could take more budget at a similar cost", "medium",
                         f"Cost per conversation ₦{recent.get('cost_per_conversation_ngn') or 0:,.0f} vs "
                         f"₦{ev.get('benchmark_cost_ngn', 0):,.0f} ({ev.get('benchmark')}), spending its full budget.")]
        alternatives = ["Costs usually rise as budget grows — the next conversations may be dearer."]
        if budget and pct:
            to = round(budget * (1 + pct / 100), -2)
            recs = [_rec("set_daily_budget", f"Raise the daily budget from ₦{budget:,.0f} to ₦{to:,.0f} (+{pct}%).",
                         "More conversations sooner at a similar cost",
                         "Medium — the paid budget is used faster, so the campaign finishes earlier; "
                         "cost per conversation may rise.",
                         "Cost per conversation vs. before the change",
                         params={"from_ngn": budget, "to_ngn": to})]

    for r in recs:
        r["history"] = (ctx.get("history") or {}).get(r["action_type"])
    market = [m for m in (ctx.get("market") or [])][:2]
    return {
        "issue_type": issue,
        "title": ISSUE_LABELS.get(issue, issue),
        "severity": finding.get("severity", "medium"),
        "level": finding.get("level", "campaign"),
        "entity_id": finding.get("entity_id", ""),
        "evidence": ev,
        "window": finding.get("window"),
        "hypotheses": hypotheses,
        "alternatives": alternatives,
        "confidence": max((h["confidence"] for h in hypotheses),
                          key=["low", "medium", "high"].index, default="low"),
        "recommendations": recs,
        "market_signals": market,
        "blocks_actions": bool(finding.get("blocks_actions")),
    }

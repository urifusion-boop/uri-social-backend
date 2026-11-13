"""
Jane + Ads — Plan Defence: Jane can explain and defend a plan she already built,
not just build one.

Confirmed gap: Jane produces campaign plans but has no way to answer "why ₦25,000?"
or "why Instagram, not Google?" — any new message is treated as more input toward
building/refining a plan, never as a question ABOUT an existing one.

Most of "compute every number, persist with reasons" already exists — summary.py's
CampaignSummary/build_campaign_summary and decision_engine.py's plan_campaign are
both real, deterministic, and already persisted into jane_ads_pending_plans. This
module is the narrower missing piece: (1) detect that a message is a question or a
challenge rather than new campaign input, (2) answer strictly from the persisted
plan/summary — never reconstruct or invent a number, and (3) re-run the SAME
deterministic decision engine for a hypothetical change and diff the real result.
"""
from __future__ import annotations

import json
from typing import Literal, Optional

import openai
from pydantic import BaseModel

from app.core.config import settings

from .decision_engine import plan_campaign
from .models import CampaignPlan, CampaignRequest, PlanDecision
from .summary import CampaignSummary, build_campaign_summary


class NlUnavailableError(Exception):
    """Same contract as nl.py/jane_consultant.py — an outage never masquerades as a
    real answer or a real diff."""


class FollowupIntent(BaseModel):
    kind: Literal["question", "challenge", "edit_ad", "new_campaign"] = "question"
    # edit_ad only: which ad (1 = the first ad shown, None = not said), which words, and
    # either the exact new wording the client gave or what they want changed about it.
    ad_number: Optional[int] = None
    ad_field: Literal["headline", "caption", "both"] = "both"
    new_text: str = ""
    instruction: str = ""
    corrected_field: str = ""   # only set for "challenge" — which foundation fact changed
    corrected_value: str = ""   # the new value, as the client stated it
    what_if_budget_ngn: Optional[float] = None  # set only when "question" is a budget
                                                 # hypothetical — the model's job here
                                                 # is pure entity extraction/arithmetic
                                                 # on a number the client stated (e.g.
                                                 # "half" of the known current budget),
                                                 # same trust level nl.py already gives
                                                 # it for parsing "₦10k" → 10000. The
                                                 # actual what-if comparison still runs
                                                 # through the real decision engine.


async def classify_followup(message: str, current_budget_ngn: Optional[float] = None) -> FollowupIntent:
    """Classify a message that arrives when a plan ALREADY exists for this thread: a
    QUESTION about the plan ('why ₦25,000?', 'what if I spent half?'), a CHALLENGE to
    a foundation fact the plan was built on ('students aren't my main buyers'), or
    genuinely NEW, unrelated campaign input. Kept as its own small, focused classifier
    rather than overloading jane_consultant.consult()'s already-complex extraction
    prompt (Plan Defence spec §1)."""
    if not settings.jane_ads_openai_key or not (message or "").strip():
        return FollowupIntent(kind="new_campaign")
    budget_line = (f"The plan's current total budget is ₦{current_budget_ngn:,.0f}.\n"
                  if current_budget_ngn else "")
    prompt = (
        "A campaign plan already exists for this conversation. Classify the client's "
        f"latest message:\n\"{message}\"\n\n{budget_line}"
        "- \"question\": asking WHY a decision was made, or a hypothetical — "
        "'what if I spent half?', 'why Instagram not Google?', 'why this budget?', "
        "'how many leads will this get me?'\n"
        "- \"challenge\": correcting a FACT the plan was built on — 'students aren't "
        "my main buyers', 'I'm actually in Yaba not Lekki', 'my budget is actually "
        "₦30,000' — something Jane got wrong about the business itself, not a "
        "question about her reasoning\n"
        "- \"edit_ad\": asking to change the WORDS of one of the plan's ads — its "
        "headline or its caption/text: 'change the second ad's headline to Ride easy', "
        "'make ad 1 shorter', 'make the caption punchier', 'rewrite the headline'\n"
        "- \"new_campaign\": a fresh, unrelated campaign ask that has nothing to do "
        "with the existing plan\n"
        "Return ONLY JSON: {\"kind\": one of the four strings above, "
        "\"ad_number\": if kind is \"edit_ad\", which ad as a number (first/1st/one = 1, "
        "second = 2, ...), or null if they didn't say, "
        "\"ad_field\": if kind is \"edit_ad\", \"headline\", \"caption\" or \"both\", "
        "\"new_text\": if kind is \"edit_ad\" and they gave the exact new wording, that "
        "wording verbatim without quotes, else empty string, "
        "\"instruction\": if kind is \"edit_ad\", what they want changed in a few words, "
        "else empty string, "
        "\"corrected_field\": a short field name being corrected if kind is "
        "\"challenge\" else empty string, \"corrected_value\": the new value as "
        "stated if kind is \"challenge\" else empty string, \"what_if_budget_ngn\": "
        "if (and only if) kind is \"question\" AND it's a hypothetical about a "
        "DIFFERENT total budget, the resulting Naira number (resolve 'half'/'double'/"
        "'₦10k' etc. against the current budget above) — else null}."
    )
    try:
        client = openai.AsyncOpenAI(api_key=settings.jane_ads_openai_key)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            timeout=15,
        )
        d = json.loads(resp.choices[0].message.content or "{}")
    except Exception as e:
        print(f"[PlanDefence] classify error: {e}", flush=True)
        # Own independent AsyncOpenAI client — AIService.py's alerting never saw this.
        if "insufficient_quota" in str(e):
            from app.services.AIService import _alert_openai_out_of_credit
            await _alert_openai_out_of_credit(str(e))
        return FollowupIntent(kind="new_campaign")
    kind = d.get("kind")
    if kind not in ("question", "challenge", "edit_ad", "new_campaign"):
        kind = "new_campaign"
    what_if_budget = d.get("what_if_budget_ngn")
    ad_number = d.get("ad_number")
    ad_field = d.get("ad_field") if d.get("ad_field") in ("headline", "caption", "both") else "both"
    return FollowupIntent(
        kind=kind,
        corrected_field=str(d.get("corrected_field", "")).strip(),
        corrected_value=str(d.get("corrected_value", "")).strip(),
        what_if_budget_ngn=float(what_if_budget) if isinstance(what_if_budget, (int, float)) else None,
        ad_number=int(ad_number) if isinstance(ad_number, (int, float)) and ad_number >= 1 else None,
        ad_field=ad_field,
        new_text=str(d.get("new_text") or "").strip().strip('"\u201c\u201d'),
        instruction=str(d.get("instruction") or "").strip(),
    )


async def rewrite_ad_words(headline: str, primary_text: str, instruction: str, field: str,
                           business: str = "") -> tuple[str, str]:
    """The ad's words rewritten the way the client asked, on the client's own message:
    it keeps every fact already in the ad and adds none (no prices, offers, dates or
    claims that aren't there). Raises NlUnavailableError when the model can't be
    reached, like the rest of this module."""
    if not settings.jane_ads_openai_key:
        raise NlUnavailableError("OPENAI_API_KEY is not configured")
    which = {"headline": "only the headline (leave the caption exactly as it is)",
             "caption": "only the caption (leave the headline exactly as it is)"}.get(
        field, "the headline and the caption")
    prompt = (
        f"An ad{(' for ' + business) if business else ''} currently reads:\n"
        f"HEADLINE: {headline}\nCAPTION: {primary_text}\n\n"
        f"The client asked: \"{instruction}\". Rewrite {which}.\n"
        "Rules: keep the same product and every fact already in it; never add a price, "
        "offer, date, statistic or claim that isn't already there; headline at most 40 "
        "characters, starting with a capital letter; caption 1-2 sentences; no emoji; "
        "plain, natural English.\n"
        "Return ONLY JSON: {\"headline\": \"...\", \"primary_text\": \"...\"}."
    )
    try:
        client = openai.AsyncOpenAI(api_key=settings.jane_ads_openai_key)
        resp = await client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            timeout=20,
        )
        d = json.loads(resp.choices[0].message.content or "{}")
    except Exception as e:
        print(f"[PlanDefence] rewrite error: {e}", flush=True)
        raise NlUnavailableError(str(e))
    new_headline = str(d.get("headline") or "").strip() if field != "caption" else headline
    new_text = str(d.get("primary_text") or "").strip() if field != "headline" else primary_text
    return (new_headline or headline), (new_text or primary_text)


async def explain_plan(
    question: str, plan: CampaignPlan, req: CampaignRequest,
    summary: Optional[CampaignSummary], understood: Optional[dict] = None,
) -> str:
    """Answer a question about an EXISTING plan using only data already derived.
    Every figure the model can reference comes from `plan`/`summary`/`understood` —
    passed in as the ONLY source of numbers, with an explicit instruction never to
    invent one that isn't there (Plan Defence spec §3). Raises NlUnavailableError on
    a model outage — same contract as the rest of this package, never silently
    fabricate an answer instead."""
    if not settings.jane_ads_openai_key:
        raise NlUnavailableError("OPENAI_API_KEY is not configured")
    facts = {
        "goal": plan.goal.value,
        "behaviour": plan.behaviour.value,
        "platforms": [p.model_dump(mode="json") for p in plan.platforms],
        "budget_tier": plan.budget_tier,
        "estimated_conversations": plan.estimated_conversations,
        "explanation": plan.explanation,
        "trace": plan.trace,
        "geo": plan.geo.model_dump(mode="json") if plan.geo else None,
        "understood": understood or {},
    }
    if summary:
        facts["summary"] = summary.model_dump(mode="json")
    prompt = (
        "You are Jane, a media buyer defending a campaign plan you already built for "
        f"a client. They ask: \"{question}\"\n\n"
        "Everything you are allowed to reference — the ACTUAL derivation, already "
        f"computed, never re-derive or invent a new number:\n{json.dumps(facts, indent=2)}\n\n"
        "Answer in 2-4 short sentences, plain language, like texting a client back. "
        "Quote the real numbers/reasons above — NEVER invent a number that isn't in "
        "the data given. Distinguish solid facts (trace/platforms/budget_tier — real "
        "arithmetic) from ESTIMATES (summary.estimates — always labelled as "
        "estimates, never guarantees; be honest about that distinction). If the "
        "question asks about something not covered above, say plainly you don't have "
        "that broken out — don't guess."
    )
    try:
        client = openai.AsyncOpenAI(api_key=settings.jane_ads_openai_key)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            timeout=20,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"[PlanDefence] explain error: {e}", flush=True)
        # Own independent AsyncOpenAI client — AIService.py's alerting never saw this.
        if "insufficient_quota" in str(e):
            from app.services.AIService import _alert_openai_out_of_credit
            await _alert_openai_out_of_credit(str(e))
        raise NlUnavailableError(str(e)) from e


class WhatIfResult(BaseModel):
    changed: str                              # e.g. "budget_ngn: 25,000 -> 10,000"
    original: CampaignSummary
    hypothetical: CampaignSummary
    hypothetical_plan: CampaignPlan
    narrative: str                            # plain-language diff, from real re-derived numbers only


def _diff_narrative(orig: CampaignSummary, hyp: CampaignSummary,
                    orig_budget: float, hyp_budget: float) -> str:
    """Pure — every clause here is a straight field comparison between two REAL,
    independently-derived summaries, never a model guess at what would change."""
    bits = [f"At ₦{hyp_budget:,.0f} instead of ₦{orig_budget:,.0f}:"]
    if hyp.platforms.value != orig.platforms.value:
        bits.append(f"platforms would change from {orig.platforms.value} to "
                    f"{hyp.platforms.value} ({hyp.platforms.reason})")
    if hyp.duration.value != orig.duration.value:
        bits.append(f"the run would be {hyp.duration.value} instead of {orig.duration.value}")
    if hyp.estimates.estimated_leads != orig.estimates.estimated_leads:
        bits.append(
            f"estimated leads would go from {orig.estimates.estimated_leads or 'n/a'} "
            f"to {hyp.estimates.estimated_leads or 'n/a'}"
        )
    if len(bits) == 1:
        bits.append("everything else about the plan stays the same.")
    return " ".join(bits)


def what_if(
    plan: CampaignPlan, req: CampaignRequest, budget_ngn: float,
    summary: Optional[CampaignSummary] = None,
) -> WhatIfResult:
    """Re-run the REAL deterministic decision engine (decision_engine.plan_campaign,
    unchanged) with one changed input — currently budget_ngn, the highest-value case
    per the spec ('what if I spent ₦10,000?') — and diff the two summaries. Never a
    model-estimated difference (Plan Defence spec §4). Purely a read-side comparison;
    never touches the stored plan. Raises ValueError with Jane's own honest reason
    when the hypothetical budget doesn't clear a platform floor at all — that IS the
    answer, not something to paper over.

    plan_campaign() itself never sets estimated_conversations — router.py's
    _build_campaign_plan derives it separately from a real per-business
    price-per-result and threads it into build_campaign_summary(). Live-tested
    (2026-07): without redoing that here, every what-if silently lost the leads
    estimate ('n/a') — exactly the number the spec calls out as the highest-value
    part of a budget what-if. The original summary's cost_per_result_ngn IS that
    real price (see summary.py — always either the true price or back-derived from
    it), so it's reused here rather than re-deriving it from scratch."""
    hypothetical_req = req.model_copy(update={"budget_ngn": budget_ngn})
    result = plan_campaign(hypothetical_req, plan.per_business_cap_ngn, plan.account_cap_ngn)
    if result.decision != PlanDecision.PLAN or result.plan is None:
        reason = result.advice.reason if result.advice else "That budget doesn't clear a platform minimum."
        raise ValueError(reason)
    hypothetical_plan = result.plan
    orig_summary = summary or build_campaign_summary(plan, req)
    price_per_result_ngn = orig_summary.estimates.cost_per_result_ngn
    if price_per_result_ngn:
        hypothetical_plan.estimated_conversations = max(1, round(budget_ngn / price_per_result_ngn))
    hyp_summary = build_campaign_summary(hypothetical_plan, hypothetical_req,
                                         price_per_result_ngn=price_per_result_ngn)
    narrative = _diff_narrative(orig_summary, hyp_summary, req.budget_ngn, budget_ngn)
    return WhatIfResult(
        changed=f"budget_ngn: {req.budget_ngn:,.0f} -> {budget_ngn:,.0f}",
        original=orig_summary, hypothetical=hyp_summary,
        hypothetical_plan=hypothetical_plan, narrative=narrative,
    )

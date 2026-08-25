"""
Jane + Ads — Multi-Plan Audience Variants (spec v1.0.0).

Sits between strategy extraction (jane_consultant.py) and the creative brief
(creative.py). Today Jane extracts one strategy and silently picks one audience.
Most businesses have more than one viable audience, and the client knows their
customers better than Jane's reasoning does — so instead of picking for them,
Jane presents up to five ranked, genuinely-different audience strategies (a
different buyer, trigger, place, and usually message) with an argued
recommendation, and the client picks one or more.

A "plan" here is NOT a CampaignPlan — nothing has a platform/budget/geo-pin
decision yet, only the audience hypothesis. decision_engine.plan_campaign still
runs exactly as before, once PER selected variant (see router.py wiring).

How many variants a client can select is COMPUTED from the real budget floors
(constants.py), never generated — the same discipline this codebase already
applies everywhere else a number reaches the client (decision_engine, summary.py,
plan_defence.py).
"""
from __future__ import annotations

import json
from typing import Optional

import openai

from app.core.config import settings

from . import constants as C
from .jane_consultant import ConsultantBrief
from .models import PlanVariant, PlanVariantSet, StrategyCitation
from .retrieval import RetrievalResult


class PlanVariantsUnavailableError(Exception):
    """Same contract as NlUnavailableError/jane_consultant's — an outage never
    masquerades as 'only one plan exists', since that's a real, meaningful
    finding the client should be able to trust."""


def max_selectable_plans(budget_ngn: float) -> tuple[int, str]:
    """How many audience variants this budget can actually support without
    starving any of them (spec §6.1) — pure arithmetic off the SAME useful-minimum
    floor decision_engine already uses, never a separately invented number.

    Tier 1 (below §6.1's ~₦15,000): one only — splitting starves both.
    Tier 2 (~₦15,000–50,000): one or two, IF each half still clears Meta's real
    useful minimum (reuses the identical test decision_engine._variant_plan already
    applies for A/B splits within one platform — "never split below the useful
    minimum, a starved variant can't learn").
    Tier 3 (~₦50,000–250,000): two or three.
    Tier 4 (above ~₦250,000): multiple, with proper structure (kept at 5 here —
    the hard cap on how many variants Jane ever generates, spec §2)."""
    meta_floor = C.USEFUL_MIN_NGN["meta"]
    if budget_ngn < C.PLAN_VARIANT_TIER_2_NGN:
        return 1, (
            f"At ₦{budget_ngn:,.0f} I'd pick just one — split it and neither gets "
            f"enough to work properly. Around ₦{C.PLAN_VARIANT_TIER_2_NGN:,.0f} "
            "running two starts to make sense."
        )
    if budget_ngn < C.PLAN_VARIANT_TIER_3_NGN:
        if budget_ngn / 2 < meta_floor:
            return 1, (
                f"₦{budget_ngn:,.0f} split two ways wouldn't clear the useful "
                f"minimum for either — running one gets the full amount working."
            )
        return 2, "This budget can properly fund two audiences at once."
    if budget_ngn < C.PLAN_VARIANT_TIER_4_NGN:
        return 3, "This budget can properly fund up to three audiences at once."
    return 5, "This budget can support running several audiences with proper structure."


def _variant_fields_block() -> str:
    return (
        "Return JSON with a top-level \"variants\" array (1 to 5 objects — generate "
        "ONLY as many genuinely distinct audiences as actually exist for this "
        "business; padding to 5 makes you look like you're inventing options) and "
        "a top-level \"recommendation_reason\" string. Each variant object:\n"
        "- who_its_for: how the customer would recognise themselves, in plain words "
        "a real person would use — e.g. \"people fitting out a new place\". NEVER a "
        "demographic/segment label like \"homeowners 30-55\" — that belongs in "
        "audience_segment instead.\n"
        "- audience_segment: the underlying targeting label (age/interest/demographic "
        "shorthand) — this is internal targeting language, never shown to the "
        "customer.\n"
        "- geo_pockets: array of named areas/pockets for this specific audience "
        "(may differ per variant even within the same city).\n"
        "- trigger: a HYPOTHESIS about the buying moment — not a description of the "
        "audience. e.g. \"solar gets bought at the moment someone is setting up a "
        "new place\", not \"people who are setting up a new place\".\n"
        "- why_this_could_work: plain-language reasoning tying the trigger to money "
        "— why this audience is worth pursuing.\n"
        "- trade_off: MANDATORY, one honest downside of this specific variant. "
        "Never omit this — a set where every option sounds perfect isn't credible.\n"
        "- needs_video: boolean, whether this variant's creative would need video "
        "rather than photos (e.g. building trust in a person, or showing motion).\n"
        "- rank: integer, 1 = best, no gaps or ties.\n"
        "- recommended: boolean, true on EXACTLY ONE variant (rank 1).\n"
        "Rank on: fit to the stated goal, reachability at the stated budget, deal "
        "economics (value per customer weighted against how many the business can "
        "actually serve — capacity before demand), any evidence of what's worked "
        "before (be honest when this is thin), and creative feasibility (a variant "
        "needing video from a business with none ranks below one that works with "
        "what they have).\n"
        "recommendation_reason: argue for the top-ranked variant using ITS OWN "
        "trade_off and why_this_could_work, in the voice of a media buyer talking "
        "to the client directly — e.g. \"I'd go with the second one. Bigger jobs, "
        "and you said the new builds are where your good work comes from.\" Never "
        "just assert it's best without a reason tied to their actual business.\n"
        "Never invent a specific number (no \"est. 58 conversions\", no \"₦380 per "
        "lead\") anywhere — express differences in KIND (\"more enquiries, smaller "
        "jobs\" / \"fewer enquiries, much bigger jobs\"), never fabricated precision."
    )



def _corpus_block(corpus: Optional["RetrievalResult"]) -> str:
    """ASC-SPEC-01 v2 §9.1 — the corpus INFORMS plans, it does not generate them.
    Five tactics presented as five strategies is the wrong output; the plan-variants
    contract needs genuinely distinct audience strategies. So retrieved records enter
    as evidence Jane may draw on for `why_this_could_work`, never as the plans
    themselves.

    §9.3: the trade-off is Jane's own reasoning from the plan's economics and is
    explicitly NOT a corpus field, so nothing here may supply it.

    §8.2: a record that applies only with modification must carry that modification
    with it — the unmodified version is frequently wrong for this market.
    """
    if not corpus or not corpus.records:
        return ""
    lines = []
    for r in corpus.records:
        line = f"- {r.claim.rstrip('.')}. Why: {r.mechanism}"
        if r.modification_required:
            line += f" (applies here only with this change: {r.modification_required})"
        lines.append(line)
    return (
        "## HOUSE RULES — these override your defaults\n"
        "The following are our own validated findings at Nigerian SME budgets. They "
        "are not background reading: apply them to the plans you produce.\n\n"
        + "\n".join(lines)
        + "\n\n"
        "How to apply them:\n"
        "- Where a finding constrains HOW you describe an audience or an offer, follow "
        "it. If one says to describe audiences by life stage and observable behaviour "
        "rather than demographic labels, then 'tech enthusiasts' is a failing answer "
        "and 'finance teams who just moved onto a new accounting system' is a passing "
        "one.\n"
        "- In 'why_this_could_work', give the actual reason this buyer spends money, "
        "drawing on the finding that applies. Do not restate the audience back as its "
        "own justification.\n"
        "- Do NOT turn a finding into a plan. A tactic is not an audience strategy — "
        "five tactics dressed as five plans is a failed answer.\n"
        "- Do NOT let them touch 'trade_off'. That is your own reasoning from this "
        "plan's economics — deal size, cycle length, reachability.\n\n"
    )


async def generate_plan_variants(
    parsed: ConsultantBrief,
    business_name: str = "",
    description: str = "",
    corpus: Optional["RetrievalResult"] = None,
) -> PlanVariantSet:
    """The one LLM call that turns an already-extracted strategy (jane_consultant's
    ConsultantBrief — goal, budget, geo strategy, intermediary/creative-fit notes)
    into up to five ranked, genuinely distinct audience strategies. Reuses the
    consultant's own extraction rather than re-deriving the business from scratch —
    this is the next reasoning step on the SAME strategic judgment, not a fresh one.

    max_selectable/selection_rule_reason are NOT part of this call — they're pure
    arithmetic (max_selectable_plans) attached by the caller, per spec §11
    ('budget gating computed, not generated')."""
    if not settings.jane_ads_openai_key:
        raise PlanVariantsUnavailableError("OPENAI_API_KEY is not configured")

    known_bits = [
        f"business: {business_name or parsed.business_name or 'unknown'}",
        f"category: {parsed.category}",
        f"goal: {parsed.goal or 'unknown'}",
        f"budget: ₦{parsed.budget_ngn:,.0f}" if parsed.budget_ngn else "budget: unknown",
        f"city: {parsed.city}" if parsed.city else "",
        f"has video: {parsed.has_video}",
    ]
    if parsed.geo_explanation:
        known_bits.append(f"Jane's geography read so far: {parsed.geo_explanation}")
    if parsed.intermediary_note:
        known_bits.append(f"intermediary note: {parsed.intermediary_note}")
    if description:
        known_bits.append(f"additional context: {description}")
    known_line = "; ".join(b for b in known_bits if b)

    prompt = (
        "You are an expert media buyer. A client's strategy has already been "
        f"established: {known_line}.\n\n"
        "Most businesses have more than one viable audience — the client knows "
        "their customers better than any amount of reasoning from you does. Your "
        "job now is NOT to silently pick one: present the genuinely different ways "
        "to find customers for this specific business, ranked, with an argued "
        "recommendation, so the client can choose.\n\n"
        "A plan is a DIFFERENT STRATEGIC APPROACH — a different buyer, a different "
        "trigger, a different place, and usually a different message. The test: "
        "would this change the creative? If two options would use the same ad, "
        "they're one plan with a targeting tweak, not two plans. 'Age 25-40 vs age "
        "30-50' is not two plans. 'Developers buying multiple units vs homeowners "
        "fitting out one' is.\n\n"
        f"{_variant_fields_block()}\n"
        f"{_corpus_block(corpus)}"
        "Return ONLY the JSON."
    )
    try:
        client = openai.AsyncOpenAI(api_key=settings.jane_ads_openai_key)
        resp = await client.chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            timeout=35,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception as e:
        print(f"[PlanVariants] generation error: {e}", flush=True)
        raise PlanVariantsUnavailableError(str(e)) from e

    raw_variants = data.get("variants") or []
    variants: list[PlanVariant] = []
    for i, v in enumerate(raw_variants[:5], start=1):
        if not isinstance(v, dict):
            continue
        variants.append(PlanVariant(
            rank=int(v.get("rank") or i),
            recommended=bool(v.get("recommended")),
            who_its_for=str(v.get("who_its_for", "")).strip(),
            audience_segment=str(v.get("audience_segment", "")).strip(),
            geo_pockets=[str(g).strip() for g in (v.get("geo_pockets") or []) if str(g).strip()],
            trigger=str(v.get("trigger", "")).strip(),
            why_this_could_work=str(v.get("why_this_could_work", "")).strip(),
            trade_off=str(v.get("trade_off", "")).strip(),
            needs_video=bool(v.get("needs_video")),
        ))
    variants.sort(key=lambda v: v.rank)

    # Never trust the model to have set exactly one recommended flag — code-enforce
    # it, same discipline as jane_consultant's _enforce_hard_requirements.
    if variants:
        recommended_count = sum(1 for v in variants if v.recommended)
        if recommended_count != 1:
            for v in variants:
                v.recommended = False
            variants[0].recommended = True

    budget = parsed.budget_ngn or 0.0
    max_selectable, selection_rule_reason = max_selectable_plans(budget)
    if variants:
        alone = round(budget, 2)
        shared = round(budget / 2, 2) if max_selectable > 1 else None
        for v in variants:
            v.budget_alone_ngn = alone
            v.budget_shared_ngn = shared

    return PlanVariantSet(
        variants=variants,
        recommendation_reason=str(data.get("recommendation_reason", "")).strip(),
        max_selectable=max_selectable,
        selection_rule_reason=selection_rule_reason,
        corpus_coverage=(corpus.coverage if corpus else "none"),
        corpus_citations=[
            StrategyCitation(
                record_id=r.strategy_id, version=r.version,
                stage="plan_generation", score=corpus.scores.get(r.strategy_id, 0.0),
            )
            for r in (corpus.records if corpus else [])
        ],
    )

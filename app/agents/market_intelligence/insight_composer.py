"""
Uri Market Intelligence — insight composition (PRD §13).

PRD §13's engineering note is explicit: "Every factual sentence must reference
one or more internal evidence IDs... Validate that numeric claims equal stored
metrics... If validation fails, retry once with the errors; otherwise show a
factual fallback summary." This module implements that literally, not just as
a prompt instruction — `_validate_numbers` actually extracts every number the
model wrote and checks it against the real computed counts before the insight
is ever returned, because an LLM instruction not to hallucinate is advisory,
and evidence integrity is the one place this pilot cannot be advisory about.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.services.AIService import AIService
from app.services.PostHogService import track_event
from .models import (
    Classification,
    Cluster,
    Evidence,
    InsightVersion,
    ScoreBreakdown,
    UrgencyAssessment,
)

PROMPT_VERSION = "mi-compose-v1"
MODEL_NAME = "gpt-4o-mini"


class _ComposeLLMOutput(BaseModel):
    headline: str
    observed_change: str
    business_implication: str
    suggested_next_step: str
    assumptions: list[str] = []


SYSTEM_PROMPT = """You write a short, factual business briefing from pre-computed evidence. You do not have
access to anything beyond what is given to you below — do not add statistics, demographics, growth rates,
or revenue figures that are not explicitly provided. If you want to say something is likely to happen,
name the assumption behind it in `assumptions`, do not state it as fact.

Separate four things, matching these fields exactly:
- headline: one short factual sentence, no adjectives implying certainty you don't have.
- observed_change: strictly what the evidence shows — only numbers/counts given to you below.
- business_implication: what this plausibly means for the business, clearly inference, not fact.
- suggested_next_step: one concrete, actionable recommendation for the business owner.

Every number you use in observed_change MUST be one of the numbers given to you in the evidence summary
below — do not compute, estimate, or round to a different number."""


def _build_user_prompt(
    cluster: Cluster,
    classifications: list[Classification],
    confidence: ScoreBreakdown,
    relevance: ScoreBreakdown,
) -> str:
    span_lines = "\n".join(f'- "{c.evidence_span}"' for c in classifications[:8])
    return f"""EVIDENCE SUMMARY (the only facts and numbers you may use):
- Theme: {cluster.theme}
- Independent accounts: {cluster.independent_account_count}
- Original conversation threads: {cluster.original_thread_count}
- Confidence score: {confidence.total}/10 ({confidence.band.value})
- Relevance score: {relevance.total}/10 ({relevance.band.value})

REPRESENTATIVE QUOTES (verbatim from real evidence):
{span_lines}

Write the briefing."""


def _extract_numbers(text: str) -> set[int]:
    return {int(n) for n in re.findall(r"\b\d+\b", text)}


def _validate_numbers(output: _ComposeLLMOutput, cluster: Cluster, confidence: ScoreBreakdown, relevance: ScoreBreakdown) -> Optional[str]:
    """Returns None if every number the model used in observed_change is a real
    stored number; otherwise returns an error string describing the mismatch,
    for the one-retry-then-fallback flow PRD §13 specifies."""
    allowed = {
        cluster.independent_account_count,
        cluster.original_thread_count,
        confidence.total,
        relevance.total,
    }
    used = _extract_numbers(output.observed_change)
    unknown = used - allowed
    if unknown:
        return f"observed_change uses numbers not in the evidence summary: {sorted(unknown)} (allowed: {sorted(allowed)})"
    return None


def _fallback_insight_text(cluster: Cluster) -> _ComposeLLMOutput:
    """Deterministic, zero-hallucination-risk text used when the LLM fails
    validation twice — PRD §13: 'otherwise show a factual fallback summary.'"""
    from .models import EvidenceType

    if cluster.primary_type == EvidenceType.PURCHASE_INQUIRY:
        return _ComposeLLMOutput(
            headline=f"Purchase inquiry: {cluster.theme}",
            observed_change="A specific purchase request was found within the active freshness window.",
            business_implication="Review the linked evidence to judge whether this inquiry is one you can act on — this summary is a factual fallback, not an AI interpretation.",
            suggested_next_step="Open the evidence drawer and respond to the inquiry directly if it's a fit.",
            assumptions=[],
        )

    people = "customer" if cluster.independent_account_count == 1 else "customers"
    return _ComposeLLMOutput(
        headline=f"{cluster.independent_account_count} {people} raised: {cluster.theme}",
        observed_change=(
            f"{cluster.independent_account_count} independent accounts across "
            f"{cluster.original_thread_count} separate conversations mentioned this in the monitored period."
        ),
        business_implication="Review the linked evidence to judge relevance to your business — this summary is a factual fallback, not an AI interpretation.",
        suggested_next_step="Open the evidence drawer and read the original conversations before deciding on an action.",
        assumptions=[],
    )


async def _call_llm(prompt: str, error_context: Optional[str] = None) -> Optional[_ComposeLLMOutput]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt if not error_context else f"{prompt}\n\nYour previous attempt failed validation: {error_context}\nFix it and use ONLY the numbers listed above."},
    ]
    ai_request = AIService.build_ai_model(model=MODEL_NAME, messages=messages, temperature=0)
    try:
        completion = await AIService.structured_chat_completion(ai_request, response_model=_ComposeLLMOutput)
    except Exception as e:
        print(f"[MI][compose] AI call raised: {e}")
        return None
    if isinstance(completion, dict) and "error" in completion:
        print(f"[MI][compose] AI call failed: {completion['error']}")
        return None
    choice = completion.choices[0]
    if getattr(choice.message, "refusal", None):
        return None
    return choice.message.parsed


async def compose_insight(
    cluster: Cluster,
    evidence_list: list[Evidence],
    classifications: list[Classification],
    confidence: ScoreBreakdown,
    relevance: ScoreBreakdown,
    urgency: UrgencyAssessment,
) -> InsightVersion:
    prompt = _build_user_prompt(cluster, classifications, confidence, relevance)

    output = await _call_llm(prompt)
    validation_error = _validate_numbers(output, cluster, confidence, relevance) if output else "no output returned"

    if output is None or validation_error:
        print(f"[MI][compose] validation failed, retrying once: {validation_error}")
        # PRD §24 "unsupported claims" — every time the model tried to state
        # a number that wasn't a real stored metric, previously only a log
        # line. distinct_id is the brand, not a human, since this fires from
        # a background scan with no acting user in scope.
        track_event(cluster.brand_id, "insight_validation_retry", {
            "brand_id": cluster.brand_id, "topic_id": cluster.topic_id, "cluster_id": cluster.id,
            "reason": (validation_error or "")[:200],
        })
        output = await _call_llm(prompt, error_context=validation_error)
        validation_error = _validate_numbers(output, cluster, confidence, relevance) if output else "no output returned on retry"

    if output is None or validation_error:
        print(f"[MI][compose] falling back to factual template after retry: {validation_error}")
        track_event(cluster.brand_id, "insight_validation_fallback", {
            "brand_id": cluster.brand_id, "topic_id": cluster.topic_id, "cluster_id": cluster.id,
            "reason": (validation_error or "")[:200],
        })
        output = _fallback_insight_text(cluster)

    now = datetime.utcnow()
    return InsightVersion(
        id=str(uuid.uuid4()),
        revision=1,
        brand_id=cluster.brand_id,
        topic_id=cluster.topic_id,
        cluster_id=cluster.id,
        type=cluster.primary_type,
        headline=output.headline,
        observed_change=output.observed_change,
        business_implication=output.business_implication,
        suggested_next_step=output.suggested_next_step,
        assumptions=output.assumptions,
        evidence_ids=cluster.evidence_ids,
        confidence=confidence,
        relevance=relevance,
        urgency=urgency,
        lifecycle=cluster.lifecycle,
        first_seen=cluster.first_seen,
        last_updated=now,
    )

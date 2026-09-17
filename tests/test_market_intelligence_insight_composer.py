"""
Uri Market Intelligence — insight composition tests (PRD §13's engineering
note: "Every factual sentence must reference internal evidence IDs...
Validate that numeric claims equal stored metrics... If validation fails,
retry once with the errors; otherwise show a factual fallback summary").

insight_composer.py implements this literally (_validate_numbers extracts
every number the model wrote and checks it against real computed counts),
but had zero test coverage before this file — this is the one mechanism
that guarantees Uri never hallucinates a number in a published insight, so
it's the highest-value gap to close first.
"""
import asyncio
from datetime import datetime

import pytest

from app.agents.market_intelligence.insight_composer import (
    _ComposeLLMOutput,
    _extract_numbers,
    _fallback_insight_text,
    _validate_numbers,
    compose_insight,
)
from app.agents.market_intelligence.models import (
    Cluster,
    ConfidenceBand,
    EvidenceType,
    Lifecycle,
    ScoreBreakdown,
    UrgencyAssessment,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _score(total=7, band=ConfidenceBand.MEDIUM):
    return ScoreBreakdown(components=[], total=total, band=band)


def _cluster(primary_type=EvidenceType.CUSTOMER_CONCERN, accounts=6, threads=3):
    now = datetime.utcnow()
    return Cluster(
        id="cl1",
        brand_id="b1",
        topic_id="t1",
        primary_type=primary_type,
        theme="Delivery delays to Lekki",
        evidence_ids=["ev1", "ev2", "ev3"],
        independent_account_count=accounts,
        original_thread_count=threads,
        first_seen=now,
        last_updated=now,
        lifecycle=Lifecycle.EMERGING,
    )


def _output(observed_change="6 independent accounts across 3 threads mentioned this."):
    return _ComposeLLMOutput(
        headline="Customers raised delivery delays",
        observed_change=observed_change,
        business_implication="This may be costing repeat orders.",
        suggested_next_step="Message affected customers with a delivery-time update.",
        assumptions=[],
    )


# ── _extract_numbers ─────────────────────────────────────────────────────────

def test_extract_numbers_finds_all_integers():
    assert _extract_numbers("6 accounts across 3 threads, confidence 7/10") == {6, 3, 7, 10}


def test_extract_numbers_empty_for_no_digits():
    assert _extract_numbers("No numbers here at all") == set()


# ── _validate_numbers ────────────────────────────────────────────────────────

def test_validate_numbers_passes_when_every_number_is_real():
    cluster = _cluster(accounts=6, threads=3)
    confidence = _score(total=7)
    relevance = _score(total=8)
    output = _output("6 independent accounts across 3 threads mentioned this. Confidence 7, relevance 8.")
    assert _validate_numbers(output, cluster, confidence, relevance) is None


def test_validate_numbers_catches_a_fabricated_number():
    cluster = _cluster(accounts=6, threads=3)
    confidence = _score(total=7)
    relevance = _score(total=8)
    output = _output("Over 50 accounts mentioned this, a huge spike.")
    error = _validate_numbers(output, cluster, confidence, relevance)
    assert error is not None
    assert "50" in error


def test_validate_numbers_rejects_plausible_but_uncomputed_number():
    """A number that isn't fabricated out of thin air but also isn't one of
    the four real stored values (e.g. the model adding 6+3=9 itself) must
    still fail — PRD §13 requires the number to BE a stored metric, not be
    derivable from stored metrics."""
    cluster = _cluster(accounts=6, threads=3)
    confidence = _score(total=7)
    relevance = _score(total=8)
    output = _output("9 total mentions were found.")
    assert _validate_numbers(output, cluster, confidence, relevance) is not None


# ── _fallback_insight_text ───────────────────────────────────────────────────

def test_fallback_text_for_purchase_inquiry_has_no_invented_numbers():
    cluster = _cluster(primary_type=EvidenceType.PURCHASE_INQUIRY, accounts=1, threads=1)
    result = _fallback_insight_text(cluster)
    assert _extract_numbers(result.observed_change) == set()
    assert "fallback" not in result.headline.lower()


def test_fallback_text_for_general_cluster_uses_only_real_counts():
    cluster = _cluster(primary_type=EvidenceType.CUSTOMER_CONCERN, accounts=6, threads=3)
    result = _fallback_insight_text(cluster)
    assert _extract_numbers(result.observed_change) == {6, 3}
    assert "factual fallback" in result.business_implication


def test_fallback_text_singular_customer_wording():
    cluster = _cluster(primary_type=EvidenceType.CUSTOMER_CONCERN, accounts=1, threads=1)
    result = _fallback_insight_text(cluster)
    assert "1 customer raised" in result.headline
    assert "customers" not in result.headline


# ── compose_insight (end-to-end, _call_llm mocked) ──────────────────────────

def test_compose_insight_uses_first_output_when_it_passes_validation(monkeypatch):
    from app.agents.market_intelligence import insight_composer

    calls = []

    async def fake_call_llm(prompt, error_context=None):
        calls.append(error_context)
        return _output("6 independent accounts across 3 threads mentioned this.")

    monkeypatch.setattr(insight_composer, "_call_llm", fake_call_llm)

    cluster = _cluster(accounts=6, threads=3)
    result = _run(compose_insight(cluster, [], [], _score(), _score(), UrgencyAssessment(is_urgent=False, reason="none")))

    assert len(calls) == 1  # no retry needed
    assert result.observed_change == "6 independent accounts across 3 threads mentioned this."
    assert result.evidence_ids == cluster.evidence_ids


def test_compose_insight_retries_once_then_succeeds(monkeypatch):
    from app.agents.market_intelligence import insight_composer

    calls = []

    async def fake_call_llm(prompt, error_context=None):
        calls.append(error_context)
        if error_context is None:
            return _output("Over 9000 accounts mentioned this!")  # fabricated
        return _output("6 independent accounts across 3 threads mentioned this.")  # corrected

    monkeypatch.setattr(insight_composer, "_call_llm", fake_call_llm)

    cluster = _cluster(accounts=6, threads=3)
    result = _run(compose_insight(cluster, [], [], _score(), _score(), UrgencyAssessment(is_urgent=False, reason="none")))

    assert len(calls) == 2
    assert calls[1] is not None  # retry was told what failed
    assert result.observed_change == "6 independent accounts across 3 threads mentioned this."


def test_compose_insight_falls_back_after_two_failed_validations(monkeypatch):
    from app.agents.market_intelligence import insight_composer

    calls = []

    async def fake_call_llm(prompt, error_context=None):
        calls.append(error_context)
        return _output("999 accounts mentioned this!")  # always fabricated

    monkeypatch.setattr(insight_composer, "_call_llm", fake_call_llm)

    cluster = _cluster(accounts=6, threads=3, primary_type=EvidenceType.CUSTOMER_CONCERN)
    result = _run(compose_insight(cluster, [], [], _score(), _score(), UrgencyAssessment(is_urgent=False, reason="none")))

    assert len(calls) == 2  # never called a third time
    assert "999" not in result.observed_change
    assert _extract_numbers(result.observed_change) == {6, 3}
    assert "factual fallback" in result.business_implication


def test_compose_insight_falls_back_when_llm_call_raises(monkeypatch):
    from app.agents.market_intelligence import insight_composer

    async def fake_call_llm(prompt, error_context=None):
        return None  # simulates AIService raising / returning nothing usable

    monkeypatch.setattr(insight_composer, "_call_llm", fake_call_llm)

    cluster = _cluster(accounts=6, threads=3)
    result = _run(compose_insight(cluster, [], [], _score(), _score(), UrgencyAssessment(is_urgent=False, reason="none")))

    assert "factual fallback" in result.business_implication


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))

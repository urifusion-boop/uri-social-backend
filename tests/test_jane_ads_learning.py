"""
Tests for the corpus feedback loop (ASC-SPEC-01 v2 §15, ASC-ENG-01 §1.2, §2).

The inversion is the point: a locally-confirmed C-grade must outrank an untested
A-grade. These pin the gates that decide when that promotion is earned, because
confirmed_locally grants grade A — a record promoted on thin evidence is noise
wearing the authority of local evidence.
"""
import asyncio

import pytest

from app.agents.jane_ads.entities import (
    ConversationOutcome,
    EvidenceGrade,
    LocalTestStatus,
    OutcomeSetBy,
    Strategy,
)
from app.agents.jane_ads.learning import (
    DEMOTION_RATE,
    MIN_DEPLOYMENTS_FOR_PROMOTION,
    MIN_OUTCOMES_RECORDED,
    assess,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def rec(**local) -> Strategy:
    s = Strategy(
        strategy_id="SEED-100", category="conversion_mechanics", claim="c",
        mechanism="m", evidence_grade=EvidenceGrade.C, market_origin="nigeria",
        transfer_verdict="applies_as_is", budget_floor_ngn_daily=2000,
    )
    for k, v in local.items():
        setattr(s.local, k, v)
    return s


class TestPromotionGates:
    def test_thin_deployments_are_not_promotable(self):
        """A record promoted on three campaigns is noise wearing the authority of
        local evidence — worse than an honest imported claim."""
        v = assess(rec(deployments=3, outcomes_recorded=3, positive_outcomes=3))
        assert not v.actionable
        assert "3/12 deployments" in v.reason

    def test_deployments_without_outcomes_do_not_count(self):
        """20 deployments and still unpromotable — ENG §2 MIN_OUTCOMES_RECORDED."""
        v = assess(rec(deployments=20, outcomes_recorded=2, positive_outcomes=2))
        assert not v.actionable
        assert "recorded outcome" in v.reason

    def test_promotion_when_both_gates_clear(self):
        v = assess(rec(deployments=12, outcomes_recorded=10, positive_outcomes=7))
        assert v.eligible_for is LocalTestStatus.CONFIRMED_LOCALLY

    def test_demotion_on_a_poor_rate(self):
        v = assess(rec(deployments=14, outcomes_recorded=12, positive_outcomes=2))
        assert v.eligible_for is LocalTestStatus.UNDERPERFORMED_LOCALLY

    def test_middle_band_is_left_alone(self):
        """Between demotion and promotion, the honest answer is 'keep testing'."""
        v = assess(rec(deployments=14, outcomes_recorded=12, positive_outcomes=5))
        assert not v.actionable

    def test_confirmed_record_degrading_is_retired(self):
        v = assess(rec(test_status=LocalTestStatus.CONFIRMED_LOCALLY,
                       deployments=25, outcomes_recorded=20, positive_outcomes=5))
        assert v.eligible_for is LocalTestStatus.RETIRED


class TestTheInversion:
    def test_confirmation_replaces_the_grade(self):
        r = rec()
        assert r.effective_grade is EvidenceGrade.C
        r.local.test_status = LocalTestStatus.CONFIRMED_LOCALLY
        assert r.effective_grade is EvidenceGrade.A

    def test_outcome_rate_ignores_unmarked_deployments(self):
        r = rec(deployments=20, outcomes_recorded=8, positive_outcomes=6)
        assert r.local.outcome_rate == 0.75


class TestOutcomeCapture:
    def test_jane_inferred_is_never_confirmed_evidence(self):
        """Pre-fill saves the operator labour; it is not evidence (§14.3)."""
        from app.agents.jane_ads.entities import Conversation
        c = Conversation(conversation_id="c", business_id="b", ad_id="a",
                         campaign_id="cm", platform="meta", charged_ngn=400)
        c.outcome = ConversationOutcome.WON
        c.outcome_set_by = OutcomeSetBy.JANE_INFERRED
        assert c.outcome_is_confirmed is False
        c.outcome_set_by = OutcomeSetBy.OPERATOR
        assert c.outcome_is_confirmed is True

    def test_unset_outcome_is_not_confirmed(self):
        from app.agents.jane_ads.entities import Conversation
        c = Conversation(conversation_id="c", business_id="b", ad_id="a",
                         campaign_id="cm", platform="meta", charged_ngn=400)
        assert c.outcome_is_confirmed is False


class TestPromotionRequiresAHuman:
    def test_apply_verdict_refuses_without_a_name(self):
        from app.agents.jane_ads.learning import apply_verdict
        v = assess(rec(deployments=12, outcomes_recorded=10, positive_outcomes=7))
        with pytest.raises(ValueError, match="never automatic"):
            _run(apply_verdict(None, None, v, confirmed_by=""))

    def test_apply_verdict_refuses_a_non_actionable_verdict(self):
        from app.agents.jane_ads.learning import apply_verdict
        v = assess(rec(deployments=1))
        with pytest.raises(ValueError, match="nothing to apply"):
            _run(apply_verdict(None, None, v, confirmed_by="collins"))


class TestBrandIdentityPrecedence:
    """A stale identity in conversation history outranked the brand profile, so a
    Social Media & Marketing Technology brand was planned for as "Living the Truth /
    Faith-based" — and every downstream plan, audience and ad served the wrong
    business. The profile is authoritative for WHO the business is."""

    def test_profile_beats_stale_history(self):
        src = open("app/agents/jane_ads/router.py").read()
        i = src.index("known_business_name = (")
        block = src[i:i + 420]
        assert 'brand_profile.get("brand_name", "")' in block
        assert block.index('brand_profile.get("brand_name"') < block.index("remembered_business_name")
        assert block.index('brand_profile.get("industry"') < block.index("remembered_category")

    def test_explicit_request_value_still_wins(self):
        """An identity stated on the request is the most specific signal of all."""
        src = open("app/agents/jane_ads/router.py").read()
        i = src.index("known_business_name = (")
        block = src[i:i + 420]
        assert block.index("body.business_name") < block.index('brand_profile.get("brand_name"')

    def test_history_is_still_the_last_resort(self):
        """It should fill the gap when a profile has no value, not be removed."""
        src = open("app/agents/jane_ads/router.py").read()
        i = src.index("known_business_name = (")
        assert "remembered_business_name(history)" in src[i:i + 420]


class TestOfferingReachesTheConsultant:
    """The profile holds product_description and key_products_services and neither
    reached Jane, so she reasoned from a name and an industry label. A category is
    not an offer, and the offer is what an audience responds to."""

    def test_consult_accepts_an_offering(self):
        import inspect
        from app.agents.jane_ads.jane_consultant import consult
        assert "offering" in inspect.signature(consult).parameters

    def test_router_builds_it_from_the_profile(self):
        src = open("app/agents/jane_ads/router.py").read()
        assert "known_offering" in src
        assert "product_description" in src and "key_products_services" in src
        assert "offering=known_offering" in src

    def test_offering_is_surfaced_in_the_prompt(self):
        import inspect
        from app.agents.jane_ads import jane_consultant
        assert "what they sell:" in inspect.getsource(jane_consultant.consult)

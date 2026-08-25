"""
Retrieval tests — the ENG §8 acceptance fixtures (ASC-SPEC-01 v2 §7–§8).

Fixture 14 is the design's core claim: if a locally-confirmed C-grade does not
outrank an untested A-grade, the inversion does not work and spec §8.1 is
decorative. It is tested here first.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.agents.jane_ads.entities import (
    ConsumedBy,
    ConversionLocation,
    EvidenceGrade,
    ExecutableVia,
    LocalTestStatus,
    MarketOrigin,
    PooledAccountSafety,
    Requirement,
    Strategy,
    StrategyCategory,
    StrategyPlatform,
    StrategyStatus,
    TransferVerdict,
)
from app.agents.jane_ads.retrieval import (
    BudgetContext,
    RetrievalResult,
    BusinessProfile,
    ExclusionReason,
    RetrievalRequest,
    gap_record,
    recency_weight,
    retrieve,
    score,
    top_exclusions,
)
from app.agents.jane_ads.store import InMemoryCoverageGapStore

NOW = datetime(2026, 8, 24, tzinfo=timezone.utc)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def rec(sid="SEED-001", **kw) -> Strategy:
    """An approved, fully-cleared record. Tests break one thing at a time."""
    base = dict(
        strategy_id=sid,
        status=StrategyStatus.APPROVED,
        category=StrategyCategory.CONVERSION_MECHANICS,
        claim="A claim.",
        mechanism="A mechanism.",
        evidence_grade=EvidenceGrade.B,
        market_origin=MarketOrigin.US,
        transfer_verdict=TransferVerdict.APPLIES_AS_IS,
        budget_floor_ngn_daily=2000,
        platforms=[StrategyPlatform.META],
        conversion_location=[ConversionLocation.MESSAGING],
        pooled_account_safe=PooledAccountSafety.YES,
        consumed_by=[ConsumedBy.PLAN_GENERATION],
        staleness_review_due=NOW + timedelta(days=120),
    )
    base.update(kw)
    return Strategy(**base)


def request(**kw) -> RetrievalRequest:
    budget = kw.pop("budget", BudgetContext(daily_spend_ngn=3322, budget_tier=2))
    profile = kw.pop("profile", BusinessProfile())
    return RetrievalRequest(
        stage=kw.pop("stage", ConsumedBy.PLAN_GENERATION),
        platforms=kw.pop("platforms", [StrategyPlatform.META]),
        budget=budget,
        profile=profile,
    )


def only_reason(records, req) -> ExclusionReason:
    res = retrieve(records, req, now=NOW)
    assert res.excluded, "expected an exclusion"
    return res.excluded[0].reason


class TestFixture14TheInversion:
    """The design's core claim. If this fails, §8.1 is decorative."""

    def test_locally_confirmed_c_outranks_untested_a(self):
        c_local = rec("C-LOCAL", evidence_grade=EvidenceGrade.C,
                      market_origin=MarketOrigin.NIGERIA)
        c_local.local.test_status = LocalTestStatus.CONFIRMED_LOCALLY
        a_untested = rec("A-UNTESTED", evidence_grade=EvidenceGrade.A)

        res = retrieve([a_untested, c_local], request(), now=NOW)
        assert [r.strategy_id for r in res.records] == ["C-LOCAL", "A-UNTESTED"]
        assert res.scores["C-LOCAL"] == 1.2
        assert res.scores["A-UNTESTED"] == 1.0

    def test_confirmation_replaces_grade_rather_than_adding(self):
        r = rec(evidence_grade=EvidenceGrade.C)
        assert score(r, NOW) == 0.5
        r.local.test_status = LocalTestStatus.CONFIRMED_LOCALLY
        assert score(r, NOW) == 1.0


class TestExclusions:
    def test_fixture_1_budget_floor_exceeds_daily_spend(self):
        assert only_reason([rec(budget_floor_ngn_daily=5000)], request()) \
            is ExclusionReason.BUDGET_FLOOR_EXCEEDS_DAILY_SPEND

    def test_fixture_2_sustained_capacity_unknown(self):
        r = rec(requires_sustained_days=7)
        req = request(budget=BudgetContext(daily_spend_ngn=3322, budget_tier=2,
                                           sustained_known=False))
        assert only_reason([r], req) is ExclusionReason.SUSTAINED_CAPACITY_UNKNOWN

    def test_sustained_capacity_insufficient(self):
        r = rec(requires_sustained_days=7, budget_floor_ngn_daily=3000)
        req = request(budget=BudgetContext(daily_spend_ngn=3322, budget_tier=2,
                                           sustained_daily_ngn=933, sustained_known=True))
        assert only_reason([r], req) is ExclusionReason.SUSTAINED_CAPACITY_INSUFFICIENT

    def test_fixture_3_does_not_transfer_never_returns(self):
        r = rec(transfer_verdict=TransferVerdict.DOES_NOT_TRANSFER,
                budget_floor_ngn_daily=None)
        res = retrieve([r], request(), now=NOW)
        assert res.records == []
        assert res.excluded[0].reason is ExclusionReason.DOES_NOT_TRANSFER

    def test_fixture_4_pooled_account_unknown_fails_closed(self):
        assert only_reason([rec(pooled_account_safe=PooledAccountSafety.UNKNOWN)], request()) \
            is ExclusionReason.POOLED_ACCOUNT_UNKNOWN

    def test_pooled_account_unsafe(self):
        assert only_reason([rec(pooled_account_safe=PooledAccountSafety.NO)], request()) \
            is ExclusionReason.POOLED_ACCOUNT_UNSAFE

    def test_requires_isolation_without_isolated_account(self):
        assert only_reason([rec(pooled_account_safe=PooledAccountSafety.REQUIRES_ISOLATION)],
                           request()) is ExclusionReason.REQUIRES_ISOLATION_UNAVAILABLE

    def test_fixture_5_implies_product_change_never_reaches_a_plan(self):
        assert only_reason([rec(implies_product_change=True)], request()) \
            is ExclusionReason.IMPLIES_PRODUCT_CHANGE

    def test_ui_only_excluded_from_plan_stages(self):
        assert only_reason([rec(executable_via=ExecutableVia.UI_ONLY)], request()) \
            is ExclusionReason.NOT_API_EXECUTABLE

    def test_ui_only_allowed_at_diagnostics(self):
        """Team knowledge is legitimately useful to an operator (§7.1.10)."""
        r = rec(executable_via=ExecutableVia.UI_ONLY, consumed_by=[ConsumedBy.DIAGNOSTICS])
        res = retrieve([r], request(stage=ConsumedBy.DIAGNOSTICS), now=NOW)
        assert len(res.records) == 1

    def test_stage_mismatch(self):
        assert only_reason([rec(consumed_by=[ConsumedBy.CREATIVE_BRIEF])], request()) \
            is ExclusionReason.STAGE_MISMATCH

    def test_cross_platform_matches_any_request(self):
        """A tactic that applies everywhere must not be excluded from a Meta-only
        request. Treating cross_platform as a literal value dropped 20 of 55."""
        r = rec(platforms=[StrategyPlatform.CROSS_PLATFORM])
        assert len(retrieve([r], request(platforms=[StrategyPlatform.META]), now=NOW).records) == 1
        assert len(retrieve([r], request(platforms=[StrategyPlatform.TIKTOK]), now=NOW).records) == 1

    def test_platform_mismatch(self):
        assert only_reason([rec(platforms=[StrategyPlatform.TIKTOK])], request()) \
            is ExclusionReason.PLATFORM_MISMATCH

    def test_conversion_location_mismatch(self):
        """Jane is messaging-first; a web-conversion record filters out automatically."""
        assert only_reason([rec(conversion_location=[ConversionLocation.WEBSITE])], request()) \
            is ExclusionReason.CONVERSION_LOCATION_MISMATCH

    def test_conversion_location_any_always_matches(self):
        r = rec(conversion_location=[ConversionLocation.ANY])
        assert len(retrieve([r], request(), now=NOW).records) == 1

    def test_infrastructure_missing_names_the_requirement(self):
        r = rec(requires=[Requirement.CUSTOMER_LIST])
        res = retrieve([r], request(), now=NOW)
        assert res.excluded[0].reason is ExclusionReason.INFRASTRUCTURE_MISSING
        assert res.excluded[0].detail == "customer_list"

    def test_not_approved_is_excluded(self):
        assert only_reason([rec(status=StrategyStatus.DRAFT)], request()) \
            is ExclusionReason.NOT_APPROVED

    def test_underperformed_and_retired_are_hard_exclusions(self):
        for st, reason in ((LocalTestStatus.UNDERPERFORMED_LOCALLY,
                            ExclusionReason.UNDERPERFORMED_LOCALLY),
                           (LocalTestStatus.RETIRED, ExclusionReason.RETIRED)):
            r = rec()
            r.local.test_status = st
            assert only_reason([r], request()) is reason

    def test_first_failing_check_wins(self):
        """ENG §5.1 — a draft record that is ALSO unaffordable reports not_approved,
        because the cheap check short-circuits first."""
        r = rec(status=StrategyStatus.DRAFT, budget_floor_ngn_daily=99999)
        assert only_reason([r], request()) is ExclusionReason.NOT_APPROVED


class TestConfidenceGate:
    def test_fixture_8_all_below_threshold_returns_nothing(self):
        """Spec §8.3 — omission, not a weak suggestion."""
        stale = rec(evidence_grade=EvidenceGrade.C,
                    transfer_verdict=TransferVerdict.APPLIES_WITH_MODIFICATION,
                    modification_required="Halve the floor.",
                    staleness_review_due=NOW - timedelta(days=10))
        res = retrieve([stale], request(), now=NOW)
        assert res.records == []
        assert res.coverage == "none"
        assert res.excluded[-1].reason is ExclusionReason.BELOW_CONFIDENCE_THRESHOLD

    def test_c_grade_with_modification_fresh_passes(self):
        """ENG §2 worked example: 0.5 × 0.85 = 0.425 → passes at 0.40."""
        r = rec(evidence_grade=EvidenceGrade.C,
                transfer_verdict=TransferVerdict.APPLIES_WITH_MODIFICATION,
                modification_required="Halve the floor.")
        assert score(r, NOW) == 0.425
        assert len(retrieve([r], request(), now=NOW).records) == 1

    def test_coverage_partial_below_three(self):
        res = retrieve([rec("A"), rec("B")], request(), now=NOW)
        assert res.coverage == "partial"

    def test_coverage_full_at_three_or_more(self):
        res = retrieve([rec("A"), rec("B"), rec("C")], request(), now=NOW)
        assert res.coverage == "full"

    def test_max_records_per_stage_caps_output(self):
        res = retrieve([rec(f"S{i}") for i in range(9)], request(), now=NOW)
        assert len(res.records) == 5


class TestRecencyAndScoring:
    def test_recency_decays(self):
        assert recency_weight(NOW + timedelta(days=120), NOW) == 1.0
        assert recency_weight(NOW + timedelta(days=10), NOW) == 0.6
        assert recency_weight(NOW - timedelta(days=1), NOW) == 0.3

    def test_nigeria_origin_earns_the_bonus(self):
        assert score(rec(market_origin=MarketOrigin.NIGERIA), NOW) == 0.96
        assert score(rec(market_origin=MarketOrigin.US), NOW) == 0.8

    def test_desk_research_earns_no_bonus(self):
        """§3.3 — only evidence observed in a Nigerian account counts as local."""
        assert score(rec(market_origin=MarketOrigin.NIGERIA_DESK_RESEARCH), NOW) == 0.8

    def test_exclusion_runs_before_scoring(self):
        """ENG §4 — an excluded record is never scored."""
        r = rec(pooled_account_safe=PooledAccountSafety.UNKNOWN)
        assert r.strategy_id not in retrieve([r], request(), now=NOW).scores


class TestCoverageGaps:
    def test_fixture_9_empty_retrieval_produces_a_gap_row(self):
        res = retrieve([rec(status=StrategyStatus.DRAFT)], request(), now=NOW)
        assert res.coverage == "none"
        store = InMemoryCoverageGapStore()
        _run(store.log_gap(gap_record(request(), NOW)))
        assert len(_run(store.list_gaps())) == 1

    def test_gap_row_carries_the_seeding_roadmap_context(self):
        g = gap_record(request(), NOW)
        assert g["stage"] == "plan_generation"
        assert g["budget_tier"] == 2
        assert g["conversion_location"] == "messaging"

    def test_operator_view_ranks_exclusions(self):
        """ENG §5.2 — if pooled_account_unknown dominates, Phase 0 is incomplete."""
        recs = [rec(f"U{i}", pooled_account_safe=PooledAccountSafety.UNKNOWN) for i in range(4)]
        recs.append(rec("B1", budget_floor_ngn_daily=99999))
        res = retrieve(recs, request(), now=NOW)
        assert top_exclusions(res.excluded)[0][0] == "pooled_account_unknown"


class TestFixture13PlatformRequired:
    def test_retrieval_before_platform_decision_is_an_error(self):
        with pytest.raises(ValueError, match="platforms is required"):
            RetrievalRequest(stage=ConsumedBy.PLAN_GENERATION, platforms=[],
                             budget=BudgetContext(daily_spend_ngn=3322, budget_tier=2))


class TestPlanGenerationIntegration:
    """ASC-SPEC-01 v2 §9 — the corpus informs plans, it does not generate them."""

    def test_corpus_block_is_empty_without_records(self):
        """No corpus, no prompt injection — Jane plans exactly as before."""
        from app.agents.jane_ads.plan_variants import _corpus_block
        assert _corpus_block(None) == ""
        assert _corpus_block(RetrievalResult([], "none", [], {})) == ""

    def test_corpus_block_carries_claim_and_mechanism(self):
        from app.agents.jane_ads.plan_variants import _corpus_block
        block = _corpus_block(RetrievalResult([rec("S1")], "partial", [], {"S1": 0.8}))
        assert "A claim" in block and "A mechanism" in block

    def test_modification_travels_with_the_claim(self):
        """§8.2 — returning the claim without its modification is a correctness bug."""
        from app.agents.jane_ads.plan_variants import _corpus_block
        r = rec("S1", transfer_verdict=TransferVerdict.APPLIES_WITH_MODIFICATION,
                modification_required="Halve the budget floor.")
        assert "Halve the budget floor." in _corpus_block(
            RetrievalResult([r], "partial", [], {"S1": 0.68}))

    def test_block_forbids_tactics_becoming_plans_and_owning_the_tradeoff(self):
        """§9.1 five tactics presented as five strategies is the wrong output;
        §9.3 the trade-off is Jane's own reasoning, never a corpus field."""
        from app.agents.jane_ads.plan_variants import _corpus_block
        block = _corpus_block(RetrievalResult([rec("S1")], "partial", [], {"S1": 0.8}))
        assert "not an audience strategy" in block
        assert "trade_off" in block

    def test_citations_pin_the_version(self):
        """§9.5 / ENG §3 — citing the id alone makes a plan unexplainable
        within one edit cycle."""
        from app.agents.jane_ads.models import StrategyCitation
        c = StrategyCitation(record_id="SEED-013", version=3,
                             stage="plan_generation", score=0.8)
        assert (c.record_id, c.version) == ("SEED-013", 3)

    def test_budget_tier_boundaries(self):
        from app.agents.jane_ads.router import _budget_tier
        assert [_budget_tier(b) for b in (14_999, 15_000, 49_999, 50_000,
                                          249_999, 250_000)] == [1, 2, 2, 3, 3, 4]

    def test_empty_coverage_is_visible_not_silent(self):
        """§8.4 / ENG §10 — silent fallback to model priors is the failure mode
        that makes the system look like it works while doing nothing."""
        from app.agents.jane_ads.models import PlanVariantSet
        assert PlanVariantSet(variants=[]).corpus_coverage == "none"


class TestSustainedCapacity:
    """Spec §5.2 — what they can KEEP spending, distinct from what this campaign can."""

    def _svc(self):
        from app.agents.jane_ads.store import InMemoryWalletStore
        from app.agents.jane_ads.wallet import WalletService
        return WalletService(InMemoryWalletStore())

    def test_single_topup_is_not_trusted(self):
        """Below the minimum event count the rate is not stable, so it fails closed
        and every multi-day tactic is excluded."""
        svc = self._svc()
        _run(svc.top_up("b1", 50_000, reference="r1"))
        assert _run(svc.sustained_daily_ngn("b1")) == (None, False)

    def test_two_topups_give_a_trusted_rate(self):
        svc = self._svc()
        _run(svc.top_up("b1", 45_000, reference="r1"))
        _run(svc.top_up("b1", 45_000, reference="r2"))
        rate, known = _run(svc.sustained_daily_ngn("b1"))
        assert known is True
        assert rate == 1000.0          # 90,000 over the 90-day window

    def test_no_wallet_is_unknown_not_zero(self):
        assert _run(self._svc().sustained_daily_ngn("nobody")) == (None, False)


class TestWhoItsForShape:
    """The field instruction forbids the label form and the corpus block repeats it
    (SEED-023). The model produced it anyway on two consecutive live runs, so the
    constraint is enforced here rather than asked for a third time."""

    def test_category_plus_need_is_rejected(self):
        from app.agents.jane_ads.plan_variants import label_shaped
        for bad in ("businesses needing efficient tech solutions",
                    "start-ups in need of scalable tech infrastructures",
                    "tech enthusiasts seeking cutting-edge software solutions",
                    "companies looking for reliable services"):
            assert label_shaped(bad), bad

    def test_situations_pass(self):
        from app.agents.jane_ads.plan_variants import label_shaped
        for good in ("people fitting out a new place",
                     "individuals setting up home offices",
                     "finance teams who just moved onto a new accounting system",
                     "restaurants that just opened a second branch"):
            assert not label_shaped(good), good

    def test_empty_is_not_flagged(self):
        from app.agents.jane_ads.plan_variants import label_shaped
        assert not label_shaped("")
        assert not label_shaped(None)


class TestCreativeBriefStage:
    """Spec §10 — corpus shapes angle/format/register; Zone A holds without exception."""

    def _rules(self):
        from app.agents.jane_ads.creative import _corpus_rules
        return _corpus_rules(RetrievalResult([rec("S1")], "partial", [], {"S1": 0.8}))

    def test_rules_are_directive_not_advisory(self):
        assert "override your defaults" in self._rules()

    def test_zone_a_prohibition_is_stated(self):
        r = self._rules()
        assert "DELIVERY CONTEXT" in r
        assert "Never introduce a location" in r

    def test_percentage_prohibition_is_stated(self):
        """§16.1 — no performance figure a record carries may reach copy."""
        assert "percentage" in self._rules()

    def test_empty_corpus_adds_nothing(self):
        from app.agents.jane_ads.creative import _corpus_rules
        assert _corpus_rules(None) == ""
        assert _corpus_rules(RetrievalResult([], "none", [], {})) == ""

    def test_directive_carries_stage_specific_forbid(self):
        from app.agents.jane_ads.retrieval import corpus_directive
        d = corpus_directive(RetrievalResult([rec("S1")], "partial", [], {"S1": 0.8}),
                             applies_to="the angle", forbid="the trade-off")
        assert "They apply to: the angle" in d
        assert "They must NOT influence: the trade-off" in d


class TestDiagnosticsStage:
    """Fires on underperformance, not at build time. The only stage where ui_only
    records are legitimately allowed through (§7.1.10) — the operator can act on
    team knowledge Jane cannot execute."""

    def test_ui_only_reaches_diagnostics(self):
        r = rec(executable_via=ExecutableVia.UI_ONLY, consumed_by=[ConsumedBy.DIAGNOSTICS])
        got = retrieve([r], request(stage=ConsumedBy.DIAGNOSTICS), now=NOW)
        assert len(got.records) == 1

    def test_ui_only_still_blocked_at_build_stages(self):
        r = rec(executable_via=ExecutableVia.UI_ONLY, consumed_by=[ConsumedBy.PLAN_GENERATION])
        assert only_reason([r], request()) is ExclusionReason.NOT_API_EXECUTABLE

    def test_limit_one_returns_the_best_scoring(self):
        hi = rec("HI", market_origin=MarketOrigin.NIGERIA, consumed_by=[ConsumedBy.DIAGNOSTICS])
        lo = rec("LO", evidence_grade=EvidenceGrade.C, consumed_by=[ConsumedBy.DIAGNOSTICS])
        got = retrieve([lo, hi], request(stage=ConsumedBy.DIAGNOSTICS), now=NOW, limit=1)
        assert [x.strategy_id for x in got.records] == ["HI"]


class TestCampaignStructureStage:
    """§12 — tier rules take precedence over corpus records, so these are stored as
    review material rather than fed into the deterministic build."""

    def test_tier_gate_excludes_parallel_adset_records_below_tier_3(self):
        r = rec(requires=[Requirement.PARALLEL_ADSET_BUDGET],
                consumed_by=[ConsumedBy.CAMPAIGN_STRUCTURE])
        req = request(stage=ConsumedBy.CAMPAIGN_STRUCTURE,
                      budget=BudgetContext(daily_spend_ngn=3720, budget_tier=2))
        assert only_reason([r], req) is ExclusionReason.INFRASTRUCTURE_MISSING

    def test_same_record_passes_at_tier_3(self):
        r = rec(requires=[Requirement.PARALLEL_ADSET_BUDGET],
                consumed_by=[ConsumedBy.CAMPAIGN_STRUCTURE])
        req = request(stage=ConsumedBy.CAMPAIGN_STRUCTURE,
                      budget=BudgetContext(daily_spend_ngn=12000, budget_tier=3))
        assert len(retrieve([r], req, now=NOW).records) == 1


class TestMotorTruthiness:
    """Motor's Database raises NotImplementedError on bool(), and in a conditional
    expression that fires while evaluating the argument — outside the callee's own
    try/except. `if db` shipped a 500 on the build endpoint that every local test
    missed, because the tests called the helper directly and never went through the
    guard. Compare with None."""

    def test_bool_on_a_motor_database_raises(self):
        class FakeMotorDB:
            def __bool__(self):
                raise NotImplementedError(
                    "Database objects do not implement truth value testing or bool()."
                )
        db = FakeMotorDB()
        with pytest.raises(NotImplementedError):
            bool(db)
        assert (db is not None) is True

    def test_creative_guard_uses_is_not_none(self):
        import inspect
        from app.agents.jane_ads import creative
        src = inspect.getsource(creative.generate_ad_creative)
        assert "if db is not None else None" in src
        assert "has_video=False) if db else" not in src


class TestCreativeCorpusReachesShippedCopy:
    """generate_ad_creative writes copy twice: write_ad_copy, then
    write_ad_copy_for_image after the image exists — and the second OVERWRITES the
    first. Passing the corpus only to the first meant it shaped copy that was then
    discarded, which is why the first live test showed no change in the ad."""

    def test_vision_rewrite_accepts_a_corpus(self):
        import inspect
        from app.agents.jane_ads import creative
        assert "corpus" in inspect.signature(creative.write_ad_copy_for_image).parameters

    def test_vision_rewrite_is_given_the_corpus(self):
        import inspect
        from app.agents.jane_ads import creative
        src = inspect.getsource(creative.generate_ad_creative)
        assert "corpus=corpus," in src

    def test_both_copy_prompts_carry_the_rules(self):
        import inspect
        from app.agents.jane_ads import creative
        for fn in (creative.write_ad_copy, creative.write_ad_copy_for_image):
            assert "_corpus_rules(corpus)" in inspect.getsource(fn), fn.__name__

    def test_creative_carries_citations(self):
        from app.agents.jane_ads.models import AdCreative
        a = AdCreative()
        assert a.corpus_coverage == "none"
        assert a.corpus_citations == []


class TestAdImageCta:
    """Every Jane ad routes to WhatsApp, so the image must not carry the brand's
    generic website CTA. Live-observed: a click-to-WhatsApp ad whose creative read
    "Visit our website" while the copy said "message me to order"."""

    def test_image_cta_is_overridden_to_whatsapp(self):
        import inspect
        from app.agents.jane_ads import creative
        src = inspect.getsource(creative.generate_ad_creative)
        assert '"override_cta": "Message us on WhatsApp"' in src
        assert "generate_ad_image(content_for_image, image_brand_context" in src


class TestCreativeCallSites:
    """A kwarg landed on the wrong function after a merge: budget_ngn went to
    creative_from_upload, which has no such parameter (a latent TypeError on the
    "Upload my own" path), while generate_ad_creative — the one that actually reads
    the corpus — got none, so every generated ad shipped corpus_coverage="none".
    Verified live: the 13:31 build had coverage none and zero citations."""

    def test_only_generate_accepts_budget(self):
        import inspect
        from app.agents.jane_ads import creative as c
        assert "budget_ngn" in inspect.signature(c.generate_ad_creative).parameters
        for fn in (c.creative_from_upload, c.creative_from_recomposite):
            assert "budget_ngn" not in inspect.signature(fn).parameters, fn.__name__

    def test_router_passes_budget_only_where_supported(self):
        src = open("app/agents/jane_ads/router.py").read()
        import re
        for m in re.finditer(r"(creative_from_upload|creative_from_recomposite)\((.*?)\n        \)",
                             src, re.S):
            assert "budget_ngn=" not in m.group(2), f"{m.group(1)} would TypeError"

    def test_generate_call_site_supplies_budget(self):
        src = open("app/agents/jane_ads/router.py").read()
        i = src.index("creative = await generate_ad_creative(")
        assert "budget_ngn=float(parsed.budget_ngn or 0)" in src[i:i+900]

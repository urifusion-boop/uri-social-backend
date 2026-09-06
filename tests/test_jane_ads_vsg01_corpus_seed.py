"""
vsg01_corpus_seed.py (VSG-01 v3 §6, step 9) — the 12 ad formats as real
corpus records, verified through the actual InMemoryStrategyStore and
retrieval.retrieve() (not a stand-in): draft on ingest, human approval
required before anything retrieves, and correct precondition gating per
business profile (product photo, real customer photo, isolated account).
"""
import asyncio

from app.agents.jane_ads.vsg01_corpus_seed import build_vsg01_strategies, seed_vsg01_corpus
from app.agents.jane_ads.store import InMemoryStrategyStore
from app.agents.jane_ads.entities import (
    ConsumedBy, StrategyCategory, StrategyPlatform, StrategyStatus,
    EvidenceGrade, MarketOrigin, PooledAccountSafety,
)
from app.agents.jane_ads.retrieval import RetrievalRequest, BudgetContext, BusinessProfile, retrieve
from app.agents.jane_ads.ad_formats import (
    receipt, us_vs_them, borrowed_interface, day1_day30, review_card,
    problem_solution, testimonial_offer, text_on_a_face, news_headline,
    censored_item, starter_pack, humour_cartoon,
)

ALL_FORMAT_MODULES = [
    receipt, us_vs_them, borrowed_interface, day1_day30, review_card,
    problem_solution, testimonial_offer, text_on_a_face, news_headline,
    censored_item, starter_pack, humour_cartoon,
]


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestBuildVsg01Strategies:
    def test_produces_exactly_twelve_valid_records(self):
        """Pydantic validation passing at construction IS a real test here
        — any record missing modification_required (mandatory whenever
        transfer_verdict is applies_with_modification) or a negative
        budget floor would raise before this assertion ever runs."""
        strategies = build_vsg01_strategies()
        assert len(strategies) == 12
        assert {s.strategy_id for s in strategies} == {m.FORMAT.format_id for m in ALL_FORMAT_MODULES}

    def test_all_records_are_draft_never_pre_approved(self):
        """corpus.py's own established invariant: a record must never
        arrive pre-approved. These are built the same way."""
        for s in build_vsg01_strategies():
            assert s.status == StrategyStatus.DRAFT

    def test_all_records_are_creative_formats_category(self):
        for s in build_vsg01_strategies():
            assert s.category == StrategyCategory.CREATIVE_FORMATS

    def test_evidence_grade_is_conservative_c_not_a_fabricated_higher_grade(self):
        """Not D either — D scores 0.0 and could never clear retrieval's
        confidence threshold, which would import these into permanent
        unreachability."""
        for s in build_vsg01_strategies():
            assert s.evidence_grade == EvidenceGrade.C

    def test_market_origin_is_desk_research_not_a_false_claim_of_live_evidence(self):
        for s in build_vsg01_strategies():
            assert s.market_origin == MarketOrigin.NIGERIA_DESK_RESEARCH

    def test_requires_mirrors_each_formats_own_ad_format_def(self):
        by_id = {s.strategy_id: s for s in build_vsg01_strategies()}
        for module in ALL_FORMAT_MODULES:
            fmt = module.FORMAT
            record = by_id[fmt.format_id]
            assert [r.value for r in record.requires] == fmt.requires

    def test_isolation_required_formats_get_the_real_retrieval_precondition(self):
        """The three §6-named formats get PooledAccountSafety.
        REQUIRES_ISOLATION — the actual retrieval.py precondition, not
        just a documentation note."""
        by_id = {s.strategy_id: s for s in build_vsg01_strategies()}
        isolation_formats = [m.FORMAT for m in ALL_FORMAT_MODULES if m.FORMAT.requires_isolation]
        assert {f.format_id for f in isolation_formats} == {"SEED-077", "SEED-078", "SEED-083"}
        for fmt in isolation_formats:
            assert by_id[fmt.format_id].pooled_account_safe == PooledAccountSafety.REQUIRES_ISOLATION
        for module in ALL_FORMAT_MODULES:
            if not module.FORMAT.requires_isolation:
                assert by_id[module.FORMAT.format_id].pooled_account_safe == PooledAccountSafety.YES


class TestSeedAndRetrieveEndToEnd:
    """The real loop: seed into a real store, confirm nothing retrieves
    before approval, approve, then confirm retrieval is correctly gated
    per business profile — against the actual InMemoryStrategyStore and
    retrieval.retrieve(), no stand-ins."""

    def test_seeding_ingests_all_twelve_as_draft(self):
        store = InMemoryStrategyStore()
        count = _run(seed_vsg01_corpus(store))
        assert count == 12
        assert _run(store.count(status=StrategyStatus.DRAFT)) == 12
        assert _run(store.count(status=StrategyStatus.APPROVED)) == 0

    def test_nothing_retrieves_before_human_approval(self):
        store = InMemoryStrategyStore()
        _run(seed_vsg01_corpus(store))
        candidates = build_vsg01_strategies()
        req = RetrievalRequest(
            stage=ConsumedBy.VCE, platforms=[StrategyPlatform.META],
            budget=BudgetContext(daily_spend_ngn=5000, budget_tier=1),
            profile=BusinessProfile(has_product_photo=True, has_real_customer_photo=True, isolated_ad_account=True),
        )
        result = retrieve(candidates, req)
        assert result.records == []
        assert all(str(e).startswith("not_approved") for e in result.excluded)

    def _seed_and_approve(self):
        store = InMemoryStrategyStore()
        _run(seed_vsg01_corpus(store))
        strategies = build_vsg01_strategies()
        for s in strategies:
            _run(store.approve(s.strategy_id, s.version, approved_by="test-operator"))
        return _run(store.fetch_approved())

    def test_business_with_no_photos_and_no_isolation_gets_only_unrestricted_formats(self):
        approved = self._seed_and_approve()
        req = RetrievalRequest(
            stage=ConsumedBy.VCE, platforms=[StrategyPlatform.META],
            budget=BudgetContext(daily_spend_ngn=5000, budget_tier=1),
            profile=BusinessProfile(has_product_photo=False, has_real_customer_photo=False, isolated_ad_account=False),
        )
        result = retrieve(approved, req)
        retrieved_ids = {r.strategy_id for r in result.records}
        # None of these require a photo or isolation.
        assert retrieved_ids <= {"SEED-075", "SEED-080", "SEED-081", "SEED-087", "SEED-089"}
        assert len(result.records) > 0

    def test_isolation_gated_formats_excluded_without_an_isolated_account(self):
        approved = self._seed_and_approve()
        req = RetrievalRequest(
            stage=ConsumedBy.VCE, platforms=[StrategyPlatform.META],
            budget=BudgetContext(daily_spend_ngn=5000, budget_tier=1),
            profile=BusinessProfile(has_product_photo=True, has_real_customer_photo=True, isolated_ad_account=False),
        )
        result = retrieve(approved, req)
        retrieved_ids = {r.strategy_id for r in result.records}
        assert "SEED-077" not in retrieved_ids  # News Headline
        assert "SEED-078" not in retrieved_ids  # Day 1 -> Day 30
        assert "SEED-083" not in retrieved_ids  # The Censored Item
        reasons = [str(e) for e in result.excluded]
        assert any("requires_isolation_unavailable" in r for r in reasons)

    def test_isolation_gated_formats_included_with_an_isolated_account(self):
        approved = self._seed_and_approve()
        req = RetrievalRequest(
            stage=ConsumedBy.VCE, platforms=[StrategyPlatform.META],
            budget=BudgetContext(daily_spend_ngn=5000, budget_tier=1),
            profile=BusinessProfile(has_product_photo=True, has_real_customer_photo=True, isolated_ad_account=True),
        )
        result = retrieve(approved, req)
        retrieved_ids = {r.strategy_id for r in result.records}
        assert retrieved_ids & {"SEED-077", "SEED-078", "SEED-083"}

    def test_product_photo_requirement_excludes_businesses_without_one(self):
        approved = self._seed_and_approve()
        req = RetrievalRequest(
            stage=ConsumedBy.VCE, platforms=[StrategyPlatform.META],
            budget=BudgetContext(daily_spend_ngn=5000, budget_tier=1),
            profile=BusinessProfile(has_product_photo=False, has_real_customer_photo=True, isolated_ad_account=True),
        )
        result = retrieve(approved, req)
        retrieved_ids = {r.strategy_id for r in result.records}
        # SEED-093 (Review Card) requires product_photo — must not appear.
        assert "SEED-093" not in retrieved_ids

    def test_real_customer_photo_requirement_excludes_businesses_without_one(self):
        approved = self._seed_and_approve()
        req = RetrievalRequest(
            stage=ConsumedBy.VCE, platforms=[StrategyPlatform.META],
            budget=BudgetContext(daily_spend_ngn=5000, budget_tier=1),
            profile=BusinessProfile(has_product_photo=True, has_real_customer_photo=False, isolated_ad_account=True),
        )
        result = retrieve(approved, req)
        retrieved_ids = {r.strategy_id for r in result.records}
        # SEED-082 (Text on a Face) requires real_customer_photo — must not appear.
        assert "SEED-082" not in retrieved_ids

    def test_wrong_platform_excludes_every_record(self):
        """These 12 are Meta-only (platforms=[META]) — a TikTok-only
        request must retrieve none of them."""
        approved = self._seed_and_approve()
        req = RetrievalRequest(
            stage=ConsumedBy.VCE, platforms=[StrategyPlatform.TIKTOK],
            budget=BudgetContext(daily_spend_ngn=5000, budget_tier=1),
            profile=BusinessProfile(has_product_photo=True, has_real_customer_photo=True, isolated_ad_account=True),
        )
        result = retrieve(approved, req)
        assert result.records == []

    def test_wrong_stage_excludes_every_record(self):
        """category CREATIVE_FORMATS derives to [CREATIVE_BRIEF, VCE] —
        a DIAGNOSTICS-stage request must retrieve none of them."""
        approved = self._seed_and_approve()
        req = RetrievalRequest(
            stage=ConsumedBy.DIAGNOSTICS, platforms=[StrategyPlatform.META],
            budget=BudgetContext(daily_spend_ngn=5000, budget_tier=1),
            profile=BusinessProfile(has_product_photo=True, has_real_customer_photo=True, isolated_ad_account=True),
        )
        result = retrieve(approved, req)
        assert result.records == []

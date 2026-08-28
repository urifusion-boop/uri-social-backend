"""
Unit tests for the ad strategy corpus schema and ingestion
(ASC-SPEC-01 v2, ASC-ENG-01 v1).

Covers the ENG §8 fixtures reachable without the retrieval layer: 6 (mandatory
modification pairing), 10 (version-pinned citation), 11 (nigeria vs desk
research), 12 (ingestion cannot approve). Fixtures needing exclusion/scoring
land with that layer.
"""
import asyncio

import pytest
from pydantic import ValidationError

from app.agents.jane_ads.backfill import derive_consumed_by
from app.agents.jane_ads.corpus import import_rows, row_to_strategy
from app.agents.jane_ads.entities import (
    ConsumedBy,
    EvidenceGrade,
    LocalTestStatus,
    MarketOrigin,
    PooledAccountSafety,
    Strategy,
    StrategyCategory,
    StrategyStatus,
    TransferVerdict,
)
from app.agents.jane_ads.store import (
    ImmutableRecordError,
    IngestionCannotApprove,
    InMemoryStrategyStore,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _row(**overrides):
    row = {
        "ID": "SEED-999",
        "Category": "Conversion Mechanics",
        "Claim": "State a price range in click-to-WhatsApp ad copy.",
        "Business Type": "Product retail",
        "Budget Floor (₦/day)": 3000,
        "Platform": "Meta (FB/IG)",
        "Funnel Stage": "Conversion",
        "Product Price Band (₦)": "3,000 - 100,000",
        "Sales Cycle": "Same day",
        "Mechanism — why it works": "Price disclosure filters unqualified enquiries.",
        "Evidence Grade": "B - Practitioner anecdote",
        "Market Origin": "Nigeria",
        "Transfer Verdict": "Applies as-is",
        "Status": "Draft",
    }
    row.update(overrides)
    return row


class TestVocabularyMapping:
    def test_workbook_labels_map_to_canonical_values(self):
        s = row_to_strategy(_row())
        assert s.category is StrategyCategory.CONVERSION_MECHANICS
        assert s.evidence_grade is EvidenceGrade.B
        assert s.transfer_verdict is TransferVerdict.APPLIES_AS_IS
        assert s.status is StrategyStatus.DRAFT

    def test_unknown_dropdown_value_fails_loudly(self):
        with pytest.raises(ValueError, match="Lists tab"):
            row_to_strategy(_row(Category="Growth Hacking"))

    def test_nigeria_and_desk_research_stay_distinct(self):
        """Fixture 11 / spec §3.3 — collapsing them destroys the only honest
        measure of local knowledge, and only `nigeria` earns the origin bonus."""
        assert row_to_strategy(_row(**{"Market Origin": "Nigeria"})).market_origin \
            is MarketOrigin.NIGERIA
        assert row_to_strategy(_row(**{"Market Origin": "Nigeria (desk research)"})).market_origin \
            is MarketOrigin.NIGERIA_DESK_RESEARCH
        assert MarketOrigin.NIGERIA is not MarketOrigin.NIGERIA_DESK_RESEARCH


class TestFailClosedDefaults:
    def test_pooled_account_safety_defaults_to_unknown(self):
        """Spec §3.2 — unknown blocks retrieval. Most records carry it at cold start
        and that is correct behaviour, not a broken system."""
        assert row_to_strategy(_row()).pooled_account_safe is PooledAccountSafety.UNKNOWN

    def test_conversion_location_is_not_guessed(self):
        """§4.3 says a human pass is required; a heuristic tagged Meta account-settings
        records as `calls`/`app`/`website` when the answer was `any`."""
        assert row_to_strategy(_row()).conversion_location == []

    def test_requires_is_not_guessed(self):
        """The inverted `website_or_pixel` tags came from exactly this kind of
        inference — a wrong precondition excludes a record from the very accounts
        it was written for."""
        assert row_to_strategy(_row()).requires == []

    def test_sustained_days_defaults_to_one(self):
        assert row_to_strategy(_row()).requires_sustained_days == 1


class TestConsumedByBackfill:
    def test_derived_from_category(self):
        assert derive_consumed_by(StrategyCategory.COPY_ANGLES) == [ConsumedBy.CREATIVE_BRIEF]

    def test_every_category_maps(self):
        for c in StrategyCategory:
            assert derive_consumed_by(c), f"{c} has no consumed_by mapping"

    def test_import_populates_consumed_by(self):
        assert ConsumedBy.PLAN_GENERATION in row_to_strategy(_row()).consumed_by


class TestMandatoryFields:
    def test_blank_mechanism_is_rejected(self):
        with pytest.raises(ValidationError):
            row_to_strategy(_row(**{"Mechanism — why it works": "   "}))

    def test_modification_required_when_verdict_says_so(self):
        """Fixture 6 / spec §8.2 — returning the claim without its modification is a
        correctness bug; the unmodified version is often wrong for this market."""
        with pytest.raises(ValidationError, match="modification_required"):
            row_to_strategy(_row(**{"Transfer Verdict": "Applies with modification",
                                    "Modification Required": None}))

    def test_modification_supplied_passes(self):
        s = row_to_strategy(_row(**{"Transfer Verdict": "Applies with modification",
                                    "Modification Required": "Halve the budget floor."}))
        assert s.modification_required


class TestBudgetFloor:
    def test_missing_floor_rejected_for_a_live_tactic(self):
        with pytest.raises(ValidationError):
            row_to_strategy(_row(**{"Budget Floor (₦/day)": None}))

    def test_missing_floor_allowed_when_it_does_not_transfer(self):
        s = row_to_strategy(_row(**{"Budget Floor (₦/day)": None,
                                    "Transfer Verdict": "Does not transfer"}))
        assert s.budget_floor_ngn_daily is None

    def test_zero_floor_is_valid_for_an_organic_tactic(self):
        assert row_to_strategy(_row(**{"Budget Floor (₦/day)": 0})).budget_floor_ngn_daily == 0.0

    def test_negative_floor_is_rejected(self):
        with pytest.raises(ValidationError):
            row_to_strategy(_row(**{"Budget Floor (₦/day)": -500}))


class TestIngestionCannotApprove:
    """Fixture 12 / spec §1.1 — only a human moves draft -> approved, and the refusal
    must live at the data layer, not merely in a service."""

    def test_store_refuses_an_approved_record(self):
        store = InMemoryStrategyStore()
        rec = row_to_strategy(_row())
        rec.status = StrategyStatus.APPROVED
        with pytest.raises(IngestionCannotApprove):
            _run(store.ingest(rec))

    def test_sheet_marked_approved_is_reported_not_imported(self):
        store = InMemoryStrategyStore()
        rep = _run(import_rows([_row(Status="Approved")], store))
        assert rep.imported == 0
        assert len(rep.errors) == 1
        assert _run(store.count(status=StrategyStatus.APPROVED)) == 0

    def test_import_leaves_nothing_approved(self):
        store = InMemoryStrategyStore()
        _run(import_rows([_row(), _row(ID="SEED-998")], store))
        assert _run(store.fetch_approved()) == []

    def test_human_approval_path_works(self):
        store = InMemoryStrategyStore()
        _run(import_rows([_row()], store))
        rec = _run(store.approve("SEED-999", 1, approved_by="collins"))
        assert rec.status is StrategyStatus.APPROVED
        assert len(_run(store.fetch_approved())) == 1


class TestVersioning:
    def test_approved_record_cannot_be_overwritten(self):
        """Spec §3.3 — immutable once approved; an edit makes a new version."""
        store = InMemoryStrategyStore()
        _run(import_rows([_row()], store))
        _run(store.approve("SEED-999", 1, "collins"))
        with pytest.raises(ImmutableRecordError):
            _run(store.ingest(row_to_strategy(_row(Claim="Edited claim."))))

    def test_new_version_coexists_with_the_old(self):
        """Fixture 10 — a plan citing v1 must still resolve to v1 after v2 exists."""
        store = InMemoryStrategyStore()
        _run(import_rows([_row()], store))
        _run(store.approve("SEED-999", 1, "collins"))
        v2 = row_to_strategy(_row(Claim="Revised claim."))
        v2.version = 2
        _run(store.ingest(v2))
        assert _run(store.get("SEED-999", 1)).claim.startswith("State a price")
        assert _run(store.get("SEED-999", 2)).claim == "Revised claim."

    def test_only_one_live_version_per_record(self):
        store = InMemoryStrategyStore()
        _run(import_rows([_row()], store))
        _run(store.approve("SEED-999", 1, "collins"))
        v2 = row_to_strategy(_row(Claim="Revised claim."))
        v2.version = 2
        _run(store.ingest(v2))
        _run(store.approve("SEED-999", 2, "collins"))
        assert len(_run(store.fetch_approved())) == 1
        assert _run(store.get("SEED-999")).version == 2


class TestLocalEvidenceInversion:
    """Spec §8.1 — local confirmation REPLACES the grade. v1's additive modifier
    produced a tie, not the inversion; fixture 14 is the design's core claim."""

    def test_confirmed_locally_promotes_effective_grade_to_a(self):
        s = row_to_strategy(_row(**{"Evidence Grade": "C - Guru assertion"}))
        assert s.effective_grade is EvidenceGrade.C
        s.local.test_status = LocalTestStatus.CONFIRMED_LOCALLY
        assert s.effective_grade is EvidenceGrade.A

    def test_approval_and_local_evidence_are_orthogonal(self):
        """ENG §1 names conflating these as the most likely modelling error."""
        s = row_to_strategy(_row())
        assert s.status is StrategyStatus.DRAFT
        assert s.local.test_status is LocalTestStatus.NOT_YET_TESTED

    def test_outcome_rate_ignores_deployments_without_outcomes(self):
        s = row_to_strategy(_row())
        s.local.deployments, s.local.outcomes_recorded, s.local.positive_outcomes = 20, 8, 6
        assert s.local.outcome_rate == 0.75

    def test_outcome_rate_is_none_without_recorded_outcomes(self):
        s = row_to_strategy(_row())
        s.local.deployments = 20
        assert s.local.outcome_rate is None


class TestImportAccounting:
    def test_example_rows_are_skipped(self):
        store = InMemoryStrategyStore()
        rep = _run(import_rows([_row(ID="EX-01", Status="EXAMPLE"), _row()], store))
        assert (rep.imported, rep.skipped_examples) == (1, 1)

    def test_rejected_records_are_retained_for_anti_duplication(self):
        """Spec §4.2 / §17.3 — they import and are kept, they simply never retrieve."""
        store = InMemoryStrategyStore()
        _run(import_rows([_row(Status="Rejected",
                               **{"Transfer Verdict": "Does not transfer",
                                  "Budget Floor (₦/day)": None})], store))
        assert _run(store.get("SEED-999")).status is StrategyStatus.REJECTED

    def test_every_row_is_accounted_for(self):
        store = InMemoryStrategyStore()
        rows = [_row(), _row(ID="EX-02", Status="EXAMPLE"), _row(ID="SEED-997", Claim=None)]
        rep = _run(import_rows(rows, store))
        assert rep.total_seen == len(rows)


class TestStaleIndexRegression:
    """v1 shipped a plain unique index on strategy_id. Left in place it makes a
    second version of any record unwritable (E11000), silently defeating the
    versioning model — ensure_indexes must DROP it, not just add the right one."""

    def test_stale_v1_index_names_are_declared_for_removal(self):
        from app.agents.jane_ads.store import MongoStrategyStore
        assert "strategy_id_1" in MongoStrategyStore.STALE_V1_INDEXES
        assert "category_1_budget_floor_ngn_per_day_1" in MongoStrategyStore.STALE_V1_INDEXES

    def test_in_memory_store_allows_two_versions(self):
        store = InMemoryStrategyStore()
        _run(import_rows([_row()], store))
        _run(store.approve("SEED-999", 1, "collins"))
        v2 = row_to_strategy(_row(Claim="Second version."))
        v2.version = 2
        _run(store.ingest(v2))
        assert _run(store.get("SEED-999", 1)) is not None
        assert _run(store.get("SEED-999", 2)).claim == "Second version."


class TestNairaShorthand:
    """A plain float() rejects "20k" and returns None, which reads downstream as
    "no budget stated" — so Jane asks for the budget immediately after being told
    it. Observed live: "we are using for leads, and budget is 20k"."""

    def test_k_and_m_shorthand(self):
        from app.agents.jane_ads.nl import parse_ngn
        assert parse_ngn("20k") == 20_000
        assert parse_ngn("20K") == 20_000
        assert parse_ngn("1.5m") == 1_500_000

    def test_naira_signs_and_separators(self):
        from app.agents.jane_ads.nl import parse_ngn
        assert parse_ngn("₦20,000") == 20_000
        assert parse_ngn("N20k") == 20_000
        assert parse_ngn("20,000") == 20_000

    def test_plain_numbers_unchanged(self):
        from app.agents.jane_ads.nl import parse_ngn
        assert parse_ngn(20000) == 20_000
        assert parse_ngn("20000") == 20_000

    def test_junk_is_none_not_a_guess(self):
        from app.agents.jane_ads.nl import parse_ngn
        for v in ("abc", "", None, "twenty thousand"):
            assert parse_ngn(v) is None


class TestCorpusUploadEndpoint:
    """Standalone admin page for seeding — the people who maintain the workbook are
    not the people using Jane, so it lives on its own route rather than inside the
    app. Writes to production corpus data, so it is gated on the same admin
    allowlist as the billing report."""

    def _routes(self):
        from app.agents.jane_ads.router import router
        return {getattr(r, "path", "") for r in router.routes}

    def test_both_routes_exist(self):
        assert "/jane-ads/corpus/upload" in self._routes()

    def test_upload_requires_an_admin(self):
        """Same allowlist as the billing report, inlined so the refusal can be worded
        for this page rather than saying "not authorized for the billing report"."""
        import inspect
        from app.agents.jane_ads.router import corpus_upload
        src = inspect.getsource(corpus_upload)
        assert "_is_ads_admin(token)" in src
        assert "JANE_ADS_ADMIN_EMAILS" in src
        assert "status_code=403" in src

    def test_only_workbooks_are_accepted(self):
        import inspect
        from app.agents.jane_ads.router import corpus_upload
        src = inspect.getsource(corpus_upload)
        assert '(".xlsx", ".xlsm")' in src

    def test_dry_run_never_touches_the_real_store(self):
        import inspect
        from app.agents.jane_ads.router import corpus_upload
        src = inspect.getsource(corpus_upload)
        assert "InMemoryStrategyStore()" in src
        assert "if not dry_run:" in src

    def test_temp_file_is_always_cleaned_up(self):
        import inspect
        from app.agents.jane_ads.router import corpus_upload
        src = inspect.getsource(corpus_upload)
        assert "finally:" in src and "os.unlink(tmp_path)" in src

    def test_page_is_self_contained(self):
        """No bundle, no build step — it has to survive a backend-only deploy."""
        from app.agents.jane_ads.router import _CORPUS_UPLOAD_HTML as h
        assert "<script>" in h and "<style>" in h
        assert "src=" not in h and "cdn" not in h.lower()

    def test_page_posts_to_the_upload_endpoint(self):
        from app.agents.jane_ads.router import _CORPUS_UPLOAD_HTML as h
        assert "/jane-ads/corpus/upload?dry_run=" in h
        assert "Bearer " in h

    def test_page_signs_in_with_email_and_password(self):
        """Non-technical users maintain the workbook — asking them to dig an access
        token out of devtools was never going to work."""
        from app.agents.jane_ads.router import _CORPUS_UPLOAD_HTML as h
        assert 'id="em"' in h and 'id="pw"' in h
        assert '"/auth/login"' in h
        assert 'type="password"' in h

    def test_password_is_never_stored(self):
        """Email is remembered for convenience; the password must not be."""
        from app.agents.jane_ads.router import _CORPUS_UPLOAD_HTML as h
        assert "corpus_email" in h
        assert "corpus_password" not in h
        assert 'localStorage.setItem("corpus_email"' in h

    def test_permission_error_reads_sensibly_here(self):
        """The shared gate says "not authorized for the billing report", which is
        nonsense on an upload page."""
        import inspect
        from app.agents.jane_ads.router import corpus_upload
        src = inspect.getsource(corpus_upload)
        assert "can't upload the corpus" in src
        assert "Corpus upload is not configured." in src
        # the shared helper's wording must not be what the user sees
        assert "Not authorized for the billing report" not in src

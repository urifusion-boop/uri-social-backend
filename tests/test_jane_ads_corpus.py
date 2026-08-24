"""
Unit tests for the ad strategy corpus (entities.Strategy, corpus.py ingestion).

These prove the workbook's hard rule — a row is only a record if every mandatory
field is filled — is enforced by the model, so no ingestion path can smuggle a
note into the corpus. InMemoryStrategyStore stands in for Mongo; no DB required.
"""
import asyncio

import pytest
from pydantic import ValidationError

from app.agents.jane_ads.corpus import ImportReport, import_rows, row_to_strategy
from app.agents.jane_ads.entities import (
    EvidenceGrade,
    Strategy,
    StrategyCategory,
    StrategyStatus,
    TransferVerdict,
)
from app.agents.jane_ads.store import InMemoryStrategyStore


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _row(**overrides):
    """A complete, valid Records row. Overrides punch holes in it per test."""
    row = {
        "ID": "SEED-999",
        "Category": "Conversion Mechanics",
        "Claim": "State a price range in click-to-WhatsApp ad copy.",
        "Business Type": "Product retail",
        "Budget Floor (₦/day)": 3000,
        "Platform": "WhatsApp",
        "Funnel Stage": "Conversion",
        "Product Price Band (₦)": "3,000 - 100,000",
        "Sales Cycle": "Same day",
        "Mechanism — why it works": "Price disclosure filters out unqualified enquiries.",
        "Evidence Grade": "B - Practitioner anecdote",
        "Market Origin": "Nigeria",
        "Transfer Verdict": "Applies as-is",
        "Status": "Draft",
    }
    row.update(overrides)
    return row


class TestMandatoryFields:
    def test_complete_row_becomes_a_record(self):
        s = row_to_strategy(_row())
        assert s.strategy_id == "SEED-999"
        assert s.category is StrategyCategory.CONVERSION_MECHANICS
        assert s.budget_floor_ngn_per_day == 3000.0

    def test_blank_mechanism_is_rejected(self):
        """"This just works" is noise — a record without a mechanism is a note."""
        with pytest.raises(ValidationError):
            row_to_strategy(_row(**{"Mechanism — why it works": "   "}))

    def test_missing_claim_is_rejected(self):
        with pytest.raises(ValidationError):
            row_to_strategy(_row(Claim=None))

    def test_unknown_dropdown_value_is_rejected(self):
        """A category the Lists tab doesn't define must fail loudly, not coerce."""
        with pytest.raises(ValidationError):
            row_to_strategy(_row(Category="Growth Hacking"))


class TestBudgetFloor:
    def test_missing_floor_rejected_for_a_live_tactic(self):
        with pytest.raises(ValidationError):
            row_to_strategy(_row(**{"Budget Floor (₦/day)": None}))

    def test_missing_floor_allowed_when_it_does_not_transfer(self):
        """A rejected claim has no budget at which it works, and the workbook says
        such records still earn their place (SEED-003 is exactly this)."""
        s = row_to_strategy(
            _row(**{"Budget Floor (₦/day)": None, "Transfer Verdict": "Does not transfer"})
        )
        assert s.transfer_verdict is TransferVerdict.DOES_NOT_TRANSFER
        assert s.budget_floor_ngn_per_day is None

    def test_zero_floor_is_valid_for_an_organic_tactic(self):
        """₦0/day is a real floor, not a missing one (SEED-044, WhatsApp Status)."""
        s = row_to_strategy(_row(**{"Budget Floor (₦/day)": 0}))
        assert s.budget_floor_ngn_per_day == 0.0

    def test_negative_floor_is_rejected(self):
        with pytest.raises(ValidationError):
            row_to_strategy(_row(**{"Budget Floor (₦/day)": -500}))

    def test_naira_formatted_text_is_parsed(self):
        assert row_to_strategy(_row(**{"Budget Floor (₦/day)": "₦3,000"})).budget_floor_ngn_per_day == 3000.0


class TestImport:
    def test_example_rows_are_not_ingested(self):
        store = InMemoryStrategyStore()
        rep = _run(import_rows([_row(ID="EX-01", Status="EXAMPLE"), _row()], store))
        assert rep.imported == 1
        assert rep.skipped_examples == 1
        assert _run(store.count()) == 1

    def test_bad_row_is_reported_not_raised(self):
        """One malformed row must not abort the import — the seeder needs the list."""
        store = InMemoryStrategyStore()
        rep = _run(import_rows([_row(), _row(ID="SEED-998", Claim=None)], store))
        assert rep.imported == 1
        assert len(rep.errors) == 1
        assert rep.errors[0].strategy_id == "SEED-998"
        assert rep.errors[0].reason  # a usable reason, not just "1 validation error"

    def test_every_row_is_accounted_for(self):
        store = InMemoryStrategyStore()
        rows = [_row(), _row(ID="EX-02", Status="EXAMPLE"), _row(ID="SEED-997", Claim=None)]
        rep = _run(import_rows(rows, store))
        assert rep.total_seen == len(rows)

    def test_reimport_updates_rather_than_duplicates(self):
        store = InMemoryStrategyStore()
        _run(import_rows([_row()], store))
        _run(import_rows([_row(Claim="Revised claim.")], store))
        assert _run(store.count()) == 1
        assert _run(store.get_strategy("SEED-999")).claim == "Revised claim."


class TestRetrieval:
    def _seeded(self):
        store = InMemoryStrategyStore()
        _run(import_rows([
            _row(ID="S1", Status="Approved",
                 **{"Budget Floor (₦/day)": 1000, "Evidence Grade": "C - Guru assertion"}),
            _row(ID="S2", Status="Approved",
                 **{"Budget Floor (₦/day)": 2000,
                    "Evidence Grade": "A - Verified case study with numbers"}),
            _row(ID="S3", Status="Approved",
                 **{"Budget Floor (₦/day)": 50000, "Category": "Copy Angles"}),
            _row(ID="S4", Status="Approved",
                 **{"Budget Floor (₦/day)": None, "Transfer Verdict": "Does not transfer"}),
        ], store))
        return store

    def test_filters_by_category(self):
        got = _run(self._seeded().find_strategies(category=StrategyCategory.COPY_ANGLES))
        assert [s.strategy_id for s in got] == ["S3"]

    def test_affordability_filter_excludes_pricier_tactics(self):
        got = _run(self._seeded().find_strategies(max_budget_floor_ngn=2000))
        assert {s.strategy_id for s in got} == {"S1", "S2"}

    def test_affordability_filter_excludes_floorless_rejections(self):
        """A "does not transfer" record carries no budget precondition, so it must
        never surface as an affordable suggestion."""
        got = _run(self._seeded().find_strategies(max_budget_floor_ngn=100000))
        assert "S4" not in {s.strategy_id for s in got}

    def test_rejections_are_still_retrievable(self):
        """They earn their place by stopping Jane rediscovering a dead tactic."""
        assert _run(self._seeded().get_strategy("S4")) is not None

    def test_better_evidence_ranks_first(self):
        got = _run(self._seeded().find_strategies(category=StrategyCategory.CONVERSION_MECHANICS))
        assert got[0].evidence_grade is EvidenceGrade.A_VERIFIED


class TestStatusGate:
    """Draft -> In review -> Approved. Ingestion keeps everything so the review
    workflow has something to act on; retrieval defaults to Approved only, so an
    unreviewed draft can never shape a live campaign plan."""

    def _mixed(self):
        store = InMemoryStrategyStore()
        _run(import_rows([
            _row(ID="D1", Status="Draft"),
            _row(ID="R1", Status="In review"),
            _row(ID="A1", Status="Approved"),
            _row(ID="X1", Status="Rejected"),
        ], store))
        return store

    def test_drafts_are_ingested(self):
        """They must be stored — the review workflow needs them."""
        assert _run(self._mixed().count()) == 4
        assert _run(self._mixed().get_strategy("D1")) is not None

    def test_retrieval_returns_only_approved(self):
        got = _run(self._mixed().find_strategies())
        assert [s.strategy_id for s in got] == ["A1"]

    def test_rejected_records_are_not_retrievable(self):
        got = _run(self._mixed().find_strategies())
        assert "X1" not in {s.strategy_id for s in got}

    def test_statuses_none_searches_whole_corpus(self):
        """Review tooling needs to see everything — never used for user-facing planning."""
        got = _run(self._mixed().find_strategies(statuses=None))
        assert len(got) == 4

    def test_explicit_status_set_is_honoured(self):
        got = _run(self._mixed().find_strategies(statuses={StrategyStatus.DRAFT}))
        assert [s.strategy_id for s in got] == ["D1"]

    def test_record_without_a_status_is_not_retrievable(self):
        """No status means it never went through review either."""
        store = InMemoryStrategyStore()
        _run(import_rows([_row(ID="N1", Status=None)], store))
        assert _run(store.count()) == 1
        assert _run(store.find_strategies()) == []

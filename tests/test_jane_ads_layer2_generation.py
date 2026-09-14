"""
layer2_generation.py — VSG-01-PROMPTS v3 §3/§8/§12: the ratio clause
resolved per generation from actual pixel dimensions (not assumed square),
and the optional §8 seasonal-context clause. No test file existed for this
module before (generate_scene() itself needs a real or mocked network call
to exercise end-to-end); this covers the pure, easily-isolated pieces —
_resolve_ratio_clause's distance-to-nearest-ratio logic and
seasonal_context_clause's validate-then-render behaviour — directly.
"""
import pytest

from app.agents.jane_ads.layer2_generation import (
    RATIO_CLAUSE_1_1,
    RATIO_CLAUSE_4_5,
    RATIO_CLAUSE_9_16,
    _resolve_ratio_clause,
    seasonal_context_clause,
)
from app.agents.jane_ads.visual_slots import InvalidSlotValue


class TestResolveRatioClause:
    def test_square_size_resolves_to_1_1(self):
        assert _resolve_ratio_clause("1080x1080") == RATIO_CLAUSE_1_1

    def test_exact_4_5_size_resolves_to_4_5(self):
        assert _resolve_ratio_clause("1080x1350") == RATIO_CLAUSE_4_5

    def test_exact_9_16_size_resolves_to_9_16(self):
        assert _resolve_ratio_clause("1080x1920") == RATIO_CLAUSE_9_16

    def test_is_case_insensitive_on_the_separator(self):
        assert _resolve_ratio_clause("1080X1920") == RATIO_CLAUSE_9_16

    def test_a_wide_zone_size_falls_back_to_nearest_which_is_1_1(self):
        """Problem/Solution's own half-canvas zone calls (e.g. "1080x540",
        a 2:1 landscape strip) aren't one of §3's three named ratios —
        confirms the nearest-distance fallback doesn't raise and lands on
        1:1, the closest of the three to a wide strip."""
        assert _resolve_ratio_clause("1080x540") == RATIO_CLAUSE_1_1

    def test_unparseable_size_falls_back_to_1_1_rather_than_raising(self):
        assert _resolve_ratio_clause("not-a-size") == RATIO_CLAUSE_1_1

    def test_zero_height_falls_back_to_1_1_rather_than_raising(self):
        assert _resolve_ratio_clause("1080x0") == RATIO_CLAUSE_1_1

    def test_the_three_clauses_are_distinct_strings(self):
        assert len({RATIO_CLAUSE_1_1, RATIO_CLAUSE_4_5, RATIO_CLAUSE_9_16}) == 3


class TestSeasonalContextClause:
    def test_none_produces_no_clause(self):
        """§8: the slot is optional — no context supplied means nothing is
        appended, not an empty-but-present sentence."""
        assert seasonal_context_clause(None) == ""

    def test_empty_string_also_produces_no_clause(self):
        assert seasonal_context_clause("") == ""

    def test_a_valid_context_produces_a_subtle_influence_sentence(self):
        """§8: 'should influence environmental cues, styling and relevance
        without overwhelming the core visual hierarchy' and 'Do not add
        seasonal decorations merely because the slot is populated' — both
        constraints should show up in the actual generated wording, not
        just the spec."""
        clause = seasonal_context_clause("Detty December")
        assert "Detty December" in clause
        assert "subtly" in clause or "subtle" in clause
        assert "Do not add seasonal decorations" in clause

    def test_a_value_outside_the_closed_vocabulary_is_rejected(self):
        """Same fail-closed contract as every other slot in this library —
        this function must not silently accept free text just because it's
        optional when absent."""
        with pytest.raises(InvalidSlotValue):
            seasonal_context_clause("Black Friday")

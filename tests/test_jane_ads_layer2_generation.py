"""
layer2_generation.py — VSG-01-PROMPTS v3 §3/§12: the ratio clause resolved
per generation from actual pixel dimensions, not assumed square. No test
file existed for this module before (generate_scene() itself needs a real
or mocked network call to exercise end-to-end); this covers the pure,
easily-isolated piece — _resolve_ratio_clause's distance-to-nearest-ratio
logic — directly.
"""
from app.agents.jane_ads.layer2_generation import (
    RATIO_CLAUSE_1_1,
    RATIO_CLAUSE_4_5,
    RATIO_CLAUSE_9_16,
    _resolve_ratio_clause,
)


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

"""
vsg01_orchestrator.py — VSG-01-PROMPTS v3 §8's seasonal-context slot,
resolved from the real calendar date and wired into the three actual
generation call sites (Problem/Solution, Work In Progress, Starter Pack).

No test file exists yet for vsg01_orchestrator.py as a whole (a real gap,
out of scope to close here) — this covers just the one pure, deterministic
function added for this work: _resolve_current_seasonal_context(), which
takes an explicit `today` so the calendar-window logic is testable without
depending on the real clock.
"""
from datetime import date

from app.agents.jane_ads.vsg01_orchestrator import _resolve_current_seasonal_context


class TestResolveCurrentSeasonalContext:
    def test_december_is_detty_december(self):
        assert _resolve_current_seasonal_context(date(2026, 12, 15)) == "Detty December"

    def test_december_31_is_still_detty_december_not_salary_week(self):
        """Precedence: December is claimed entirely by Detty December —
        confirmed here since Dec 31 would otherwise also match the
        salary-week day window."""
        assert _resolve_current_seasonal_context(date(2026, 12, 31)) == "Detty December"

    def test_september_is_back_to_school_season(self):
        assert _resolve_current_seasonal_context(date(2026, 9, 10)) == "back-to-school season"

    def test_september_1_is_still_back_to_school_not_salary_week(self):
        """Same precedence point as December 31, for the other month
        entirely claimed ahead of the salary-week window."""
        assert _resolve_current_seasonal_context(date(2026, 9, 1)) == "back-to-school season"

    def test_late_month_resolves_to_salary_week(self):
        assert _resolve_current_seasonal_context(date(2026, 6, 29)) == "salary week"

    def test_early_month_resolves_to_salary_week(self):
        assert _resolve_current_seasonal_context(date(2026, 6, 2)) == "salary week"

    def test_mid_month_outside_september_and_december_is_none(self):
        """§8's own caution — 'Do not add seasonal decorations merely
        because the slot is populated' — means most of the year should
        genuinely resolve to no seasonal framing, not something invented
        to always have a value."""
        assert _resolve_current_seasonal_context(date(2026, 6, 15)) is None

    def test_defaults_to_the_real_today_when_not_given(self):
        """Doesn't raise, and returns either None or a real §8 value —
        confirms the default path (used by every actual orchestrator call
        site) is wired, without pinning the test to today's actual date."""
        from app.agents.jane_ads.visual_slots import SEASONAL_CONTEXTS

        result = _resolve_current_seasonal_context()
        assert result is None or result in SEASONAL_CONTEXTS

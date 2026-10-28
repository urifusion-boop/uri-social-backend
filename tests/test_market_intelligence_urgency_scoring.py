"""
Uri Market Intelligence — urgency scoring (classification/scoring.py).

Confirmed live: a real account showed "48/48 urgent" on its active
insights. Root cause was two separate hardcodings, not a real signal:
- is_inquiry_eligible requires urgency_for_inquiry(...).is_urgent, and
  urgency_for_inquiry used the SAME freshness window as eligibility — so
  every eligible purchase inquiry was, by construction, always urgent.
- urgency_for_concern (reused by concern/praise/reputation_risk/unmet_need/
  competitor_movement) unconditionally returned is_urgent=False — a
  hardcoded constant, so no insight of those types could ever be urgent.
"""
from datetime import datetime, timedelta

from app.agents.market_intelligence.classification.scoring import (
    INQUIRY_FRESHNESS_HOURS,
    INQUIRY_URGENT_HOURS,
    is_inquiry_eligible,
    urgency_for_concern,
    urgency_for_inquiry,
)
from app.agents.market_intelligence.models import Evidence


def _evidence(published_hours_ago: float, author="@a1", **overrides):
    now = datetime(2026, 10, 28, 12, 0, 0)
    base = dict(
        id=f"e-{author}-{published_hours_ago}", brand_id="b1", user_id="u1", topic_id="t1",
        provider="fake", platform="fake", source_id=f"s-{author}-{published_hours_ago}",
        text="some real text", author_handle=author,
        published_at=now - timedelta(hours=published_hours_ago),
        collected_at=now,
    )
    base.update(overrides)
    return Evidence(**base)


NOW = datetime(2026, 10, 28, 12, 0, 0)


class TestUrgencyForInquiry:
    def test_fresh_inquiry_within_urgent_cutoff_is_urgent(self):
        result = urgency_for_inquiry(_evidence(2), now=NOW)
        assert result.is_urgent is True

    def test_eligible_but_past_the_urgent_cutoff_is_not_urgent(self):
        """The actual conflation fix: an inquiry can still be inside the
        48h eligibility window without being flagged urgent."""
        hours = INQUIRY_URGENT_HOURS + 1
        assert hours < INQUIRY_FRESHNESS_HOURS
        result = urgency_for_inquiry(_evidence(hours), now=NOW)
        assert result.is_urgent is False
        assert "urgent cutoff" in result.reason

    def test_past_the_freshness_window_is_not_urgent_either(self):
        result = urgency_for_inquiry(_evidence(INQUIRY_FRESHNESS_HOURS + 1), now=NOW)
        assert result.is_urgent is False

    def test_undated_is_not_urgent(self):
        result = urgency_for_inquiry(_evidence(2, published_at=None), now=NOW)
        assert result.is_urgent is False

    def test_eligibility_and_urgency_are_no_longer_the_same_boolean(self):
        """The literal live bug: before this fix, is_inquiry_eligible==True
        implied is_urgent==True, every time. A 40h-old inquiry (eligible,
        not urgent) proves they're independent now."""
        evidence = _evidence(40)
        eligible, _ = is_inquiry_eligible(evidence, now=NOW)
        urgency = urgency_for_inquiry(evidence, now=NOW)
        assert eligible is True
        assert urgency.is_urgent is False


class TestUrgencyForConcern:
    def test_mostly_recent_accounts_is_urgent(self):
        evidence = [_evidence(2, author=f"@a{i}") for i in range(5)]
        result = urgency_for_concern(evidence, now=NOW)
        assert result.is_urgent is True

    def test_mostly_stale_accounts_is_not_urgent(self):
        evidence = [_evidence(96, author=f"@a{i}") for i in range(5)]
        result = urgency_for_concern(evidence, now=NOW)
        assert result.is_urgent is False

    def test_exactly_half_recent_meets_the_bar(self):
        evidence = [_evidence(2, author="@a1"), _evidence(96, author="@a2")]
        result = urgency_for_concern(evidence, now=NOW)
        assert result.is_urgent is True

    def test_no_independent_accounts_is_not_urgent(self):
        result = urgency_for_concern([_evidence(2, author=None)], now=NOW)
        assert result.is_urgent is False

    def test_this_is_no_longer_a_hardcoded_constant(self):
        """The literal live bug: urgency_for_concern used to return
        is_urgent=False unconditionally, for every cluster, no matter what
        the evidence looked like. A clearly-accelerating cluster must now
        be able to come back urgent."""
        accelerating = urgency_for_concern([_evidence(1, author=f"@a{i}") for i in range(6)], now=NOW)
        quiet = urgency_for_concern([_evidence(200, author=f"@a{i}") for i in range(6)], now=NOW)
        assert accelerating.is_urgent is True
        assert quiet.is_urgent is False

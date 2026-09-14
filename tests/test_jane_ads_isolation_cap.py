"""
isolation_cap.py (VSG-01 v3 §6/§2.8, SEED-079) — the book-wide usage cap
for requires_isolation formats, tested against the real async
IsolationCapService/InMemoryIsolationUsageStore logic (no mocking — this
is pure in-memory state, the same store implementation this module ships
for production unit testing).
"""
import asyncio
from datetime import datetime, timedelta, timezone

from app.agents.jane_ads.isolation_cap import (
    IsolationCapService,
    InMemoryIsolationUsageStore,
)
from app.agents.jane_ads.ad_formats import news_headline, day1_day30, censored_item, problem_solution


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestNonIsolationFormatsAlwaysPass:
    def test_problem_solution_is_never_capped(self):
        service = IsolationCapService(InMemoryIsolationUsageStore(), cap=0)  # cap=0 would deny anything capped
        decision = _run(service.check(problem_solution.FORMAT))
        assert decision.allowed is True
        assert "not an isolation-capped format" in decision.reason

    def test_record_is_a_no_op_for_non_capped_formats(self):
        store = InMemoryIsolationUsageStore()
        service = IsolationCapService(store)
        _run(service.record(problem_solution.FORMAT, business_id="biz-1"))
        assert store.records == []


class TestIsolationCapEnforcement:
    def test_all_three_named_formats_are_capped(self):
        """§6 names SEED-077, SEED-078, SEED-083 explicitly."""
        for format_def in (news_headline.FORMAT, day1_day30.FORMAT, censored_item.FORMAT):
            assert format_def.requires_isolation is True

    def test_allows_usage_below_the_cap(self):
        store = InMemoryIsolationUsageStore()
        service = IsolationCapService(store, cap=3)
        _run(service.record(news_headline.FORMAT, "biz-1"))
        _run(service.record(news_headline.FORMAT, "biz-2"))
        decision = _run(service.check(news_headline.FORMAT))
        assert decision.allowed is True
        assert decision.current_usage == 2

    def test_denies_usage_at_the_cap(self):
        store = InMemoryIsolationUsageStore()
        service = IsolationCapService(store, cap=3)
        for i in range(3):
            _run(service.record(news_headline.FORMAT, f"biz-{i}"))
        decision = _run(service.check(news_headline.FORMAT))
        assert decision.allowed is False
        assert decision.current_usage == 3
        assert "SEED-077" in decision.reason

    def test_cap_is_per_format_not_shared_across_formats(self):
        """'Across the book' means across every business using ONE
        format, not a single shared bucket for all isolation-capped
        formats — Day 1 -> Day 30 being at its cap must not block News
        Headline."""
        store = InMemoryIsolationUsageStore()
        service = IsolationCapService(store, cap=1)
        _run(service.record(day1_day30.FORMAT, "biz-1"))
        day1_decision = _run(service.check(day1_day30.FORMAT))
        news_decision = _run(service.check(news_headline.FORMAT))
        assert day1_decision.allowed is False
        assert news_decision.allowed is True

    def test_usage_outside_the_time_window_does_not_count(self):
        store = InMemoryIsolationUsageStore()
        service = IsolationCapService(store, cap=3, window=timedelta(days=30))
        store.records.append({
            "format_id": news_headline.FORMAT.format_id,
            "business_id": "biz-old",
            "occurred_at": datetime.now(timezone.utc) - timedelta(days=45),
        })
        decision = _run(service.check(news_headline.FORMAT))
        assert decision.current_usage == 0
        assert decision.allowed is True

    def test_usage_inside_the_time_window_counts(self):
        store = InMemoryIsolationUsageStore()
        service = IsolationCapService(store, cap=3, window=timedelta(days=30))
        store.records.append({
            "format_id": news_headline.FORMAT.format_id,
            "business_id": "biz-recent",
            "occurred_at": datetime.now(timezone.utc) - timedelta(days=1),
        })
        decision = _run(service.check(news_headline.FORMAT))
        assert decision.current_usage == 1


class TestInMemoryIsolationUsageStore:
    def test_count_usage_filters_by_format_id(self):
        store = InMemoryIsolationUsageStore()
        now = datetime.now(timezone.utc)
        _run(store.record_usage("SEED-077", "biz-1", now))
        _run(store.record_usage("SEED-078", "biz-1", now))
        assert _run(store.count_usage("SEED-077", now - timedelta(days=1))) == 1
        assert _run(store.count_usage("SEED-078", now - timedelta(days=1))) == 1

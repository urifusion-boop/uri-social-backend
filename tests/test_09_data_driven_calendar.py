"""
Tests: Data-Driven Content Calendar (Phase 1)
──────────────────────────────────────────────
Unit tests  — no server needed, test service logic directly
API tests   — hit staging endpoints (requires running server + valid auth)

Run unit tests only:
    pytest tests/test_09_data_driven_calendar.py -k "unit" -v

Run API tests only (needs staging):
    pytest tests/test_09_data_driven_calendar.py -k "api" -v

Run everything:
    pytest tests/test_09_data_driven_calendar.py -v
"""

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS — no DB, no server, pure logic
# ══════════════════════════════════════════════════════════════════════════════

class TestIdeaScoringServiceUnit:
    """Test IdeaScoringService logic in isolation."""

    MOCK_PERFORMANCE = {
        "avg_engagement_by_format": {"image": 5.2, "text": 2.1, "long_form": 3.0},
        "avg_engagement_by_topic":  {"finance": 6.1, "education": 4.0, "marketing": 3.5},
        "best_posting_hour": 18,
        "top_formats": ["image", "long_form", "text"],
        "top_topics":  ["finance", "education", "marketing"],
        "post_count": 15,
        "has_data": True,
    }

    MOCK_TRENDS = [
        {"keyword": "personal finance", "trend_score": 85.0, "growth_rate": 120.0, "source": "google_trends", "type": "rising"},
        {"keyword": "investment tips",  "trend_score": 70.0, "growth_rate": 80.0,  "source": "google_trends", "type": "rising"},
        {"keyword": "savings",          "trend_score": 60.0, "growth_rate": 40.0,  "source": "google_trends", "type": "top"},
        {"keyword": "fintech",          "trend_score": 55.0, "growth_rate": 35.0,  "source": "google_trends", "type": "top"},
        {"keyword": "money management", "trend_score": 50.0, "growth_rate": 20.0,  "source": "fallback",      "type": "seed"},
    ]

    def test_unit_generate_ideas_returns_list(self):
        """generate_ideas() returns a non-empty list."""
        from app.services.IdeaScoringService import IdeaScoringService
        ideas = IdeaScoringService.generate_ideas(self.MOCK_TRENDS, "finance", self.MOCK_PERFORMANCE)
        assert isinstance(ideas, list)
        assert len(ideas) > 0

    def test_unit_generate_ideas_have_required_fields(self):
        """Every generated idea has the required fields."""
        from app.services.IdeaScoringService import IdeaScoringService
        ideas = IdeaScoringService.generate_ideas(self.MOCK_TRENDS, "finance", self.MOCK_PERFORMANCE)
        required = {"idea_id", "title", "keyword", "format", "trend_score", "performance_score", "format_score", "final_score"}
        for idea in ideas:
            assert required.issubset(idea.keys()), f"Missing fields in idea: {idea}"

    def test_unit_generate_ideas_uses_trend_keywords(self):
        """Ideas are based on the provided trend keywords."""
        from app.services.IdeaScoringService import IdeaScoringService
        ideas = IdeaScoringService.generate_ideas(self.MOCK_TRENDS, "finance", self.MOCK_PERFORMANCE)
        keywords_in_ideas = {i["keyword"].lower() for i in ideas}
        trend_keywords = {t["keyword"].lower() for t in self.MOCK_TRENDS}
        assert keywords_in_ideas.issubset(trend_keywords), "Idea keywords should come from trend data"

    def test_unit_score_ideas_applies_prd_formula(self):
        """score_ideas() returns scores between 0–100 and final_score matches the PRD formula."""
        from app.services.IdeaScoringService import IdeaScoringService
        ideas = IdeaScoringService.generate_ideas(self.MOCK_TRENDS, "finance", self.MOCK_PERFORMANCE)
        scored = IdeaScoringService.score_ideas(ideas, self.MOCK_PERFORMANCE)

        assert len(scored) > 0
        for idea in scored:
            assert 0 <= idea["trend_score"] <= 100,       f"trend_score out of range: {idea}"
            assert 0 <= idea["performance_score"] <= 100, f"performance_score out of range: {idea}"
            assert 0 <= idea["format_score"] <= 100,      f"format_score out of range: {idea}"
            assert 0 <= idea["final_score"] <= 100,       f"final_score out of range: {idea}"

            # Verify PRD formula: 0.4*trend + 0.4*perf + 0.2*format
            expected = round(
                (0.4 * idea["trend_score"]) +
                (0.4 * idea["performance_score"]) +
                (0.2 * idea["format_score"]),
                1
            )
            assert idea["final_score"] == expected, (
                f"Formula mismatch for '{idea['title']}': "
                f"expected {expected}, got {idea['final_score']}"
            )

    def test_unit_score_ideas_sorted_descending(self):
        """Scored ideas are sorted by final_score descending."""
        from app.services.IdeaScoringService import IdeaScoringService
        ideas  = IdeaScoringService.generate_ideas(self.MOCK_TRENDS, "finance", self.MOCK_PERFORMANCE)
        scored = IdeaScoringService.score_ideas(ideas, self.MOCK_PERFORMANCE)
        scores = [i["final_score"] for i in scored]
        assert scores == sorted(scores, reverse=True), "Ideas should be sorted highest score first"

    def test_unit_score_ideas_rising_keyword_boost(self):
        """Rising keywords score higher than seed keywords for the same template."""
        from app.services.IdeaScoringService import IdeaScoringService
        rising_trends = [{"keyword": "fintech", "trend_score": 50.0, "growth_rate": 200.0, "source": "google_trends", "type": "rising"}]
        seed_trends   = [{"keyword": "fintech", "trend_score": 50.0, "growth_rate": 0.0,   "source": "fallback",      "type": "seed"}]
        empty_perf = {"avg_engagement_by_format": {}, "avg_engagement_by_topic": {}, "top_formats": [], "top_topics": [], "has_data": False}

        rising_ideas = IdeaScoringService.generate_ideas(rising_trends, "finance", empty_perf)
        seed_ideas   = IdeaScoringService.generate_ideas(seed_trends,   "finance", empty_perf)

        rising_scored = IdeaScoringService.score_ideas(rising_ideas, empty_perf)
        seed_scored   = IdeaScoringService.score_ideas(seed_ideas,   empty_perf)

        assert rising_scored[0]["trend_score"] > seed_scored[0]["trend_score"], (
            "Rising keyword should score higher than seed keyword"
        )

    def test_unit_score_ideas_no_data_fallback(self):
        """score_ideas() works gracefully when there is no performance data."""
        from app.services.IdeaScoringService import IdeaScoringService
        empty_perf = {"avg_engagement_by_format": {}, "avg_engagement_by_topic": {}, "top_formats": [], "top_topics": [], "has_data": False}
        ideas  = IdeaScoringService.generate_ideas(self.MOCK_TRENDS[:3], "finance", empty_perf)
        scored = IdeaScoringService.score_ideas(ideas, empty_perf)
        assert len(scored) > 0

    def test_unit_select_for_calendar_returns_7(self):
        """select_for_calendar() returns exactly 7 ideas."""
        from app.services.IdeaScoringService import IdeaScoringService
        ideas  = IdeaScoringService.generate_ideas(self.MOCK_TRENDS, "finance", self.MOCK_PERFORMANCE)
        scored = IdeaScoringService.score_ideas(ideas, self.MOCK_PERFORMANCE)
        top7   = IdeaScoringService.select_for_calendar(scored, n=7)
        assert len(top7) == 7, f"Expected 7 ideas, got {len(top7)}"

    def test_unit_select_for_calendar_no_duplicates(self):
        """select_for_calendar() returns unique ideas (no repeated idea_id)."""
        from app.services.IdeaScoringService import IdeaScoringService
        ideas  = IdeaScoringService.generate_ideas(self.MOCK_TRENDS, "finance", self.MOCK_PERFORMANCE)
        scored = IdeaScoringService.score_ideas(ideas, self.MOCK_PERFORMANCE)
        top7   = IdeaScoringService.select_for_calendar(scored, n=7)
        ids    = [i["idea_id"] for i in top7]
        assert len(ids) == len(set(ids)), "Duplicate idea_ids found in calendar selection"

    def test_unit_select_for_calendar_keyword_diversity(self):
        """No keyword appears more than 2 times in the 7-day calendar."""
        from app.services.IdeaScoringService import IdeaScoringService
        ideas  = IdeaScoringService.generate_ideas(self.MOCK_TRENDS, "finance", self.MOCK_PERFORMANCE)
        scored = IdeaScoringService.score_ideas(ideas, self.MOCK_PERFORMANCE)
        top7   = IdeaScoringService.select_for_calendar(scored, n=7)
        from collections import Counter
        kw_counts = Counter(i["keyword"].lower() for i in top7)
        for kw, count in kw_counts.items():
            assert count <= 2, f"Keyword '{kw}' appears {count} times — exceeds diversity limit of 2"

    def test_unit_select_for_calendar_has_content_types(self):
        """Each selected idea has a content_type assigned."""
        from app.services.IdeaScoringService import IdeaScoringService
        ideas  = IdeaScoringService.generate_ideas(self.MOCK_TRENDS, "finance", self.MOCK_PERFORMANCE)
        scored = IdeaScoringService.score_ideas(ideas, self.MOCK_PERFORMANCE)
        top7   = IdeaScoringService.select_for_calendar(scored, n=7)
        valid_types = {"educational", "trend_based", "promotional"}
        for idea in top7:
            assert idea.get("content_type") in valid_types, (
                f"Invalid content_type '{idea.get('content_type')}' in idea '{idea['title']}'"
            )

    def test_unit_ideas_have_explanation_reason(self):
        """Scored ideas include a human-readable reason string."""
        from app.services.IdeaScoringService import IdeaScoringService
        ideas  = IdeaScoringService.generate_ideas(self.MOCK_TRENDS, "finance", self.MOCK_PERFORMANCE)
        scored = IdeaScoringService.score_ideas(ideas, self.MOCK_PERFORMANCE)
        for idea in scored[:5]:
            assert isinstance(idea.get("reason"), str) and len(idea["reason"]) > 0, (
                f"Missing reason for idea: {idea['title']}"
            )


class TestTrendDataServiceUnit:
    """Test TrendDataService fallback logic (no real API call)."""

    def test_unit_fallback_keywords_returns_list(self):
        """_fallback_keywords() returns a non-empty list for known industries."""
        from app.services.TrendDataService import TrendDataService
        for industry in ["finance", "fashion", "real estate", "technology", "unknown_industry"]:
            result = TrendDataService._fallback_keywords(industry)
            assert isinstance(result, list) and len(result) > 0, f"No fallback for industry: {industry}"

    def test_unit_fallback_keywords_have_required_fields(self):
        """Fallback keywords have keyword, trend_score, growth_rate, source, type."""
        from app.services.TrendDataService import TrendDataService
        keywords = TrendDataService._fallback_keywords("finance")
        required = {"keyword", "trend_score", "growth_rate", "source", "type"}
        for kw in keywords:
            assert required.issubset(kw.keys()), f"Missing fields: {kw}"

    def test_unit_fallback_keyword_scores_are_numeric(self):
        """Trend and growth rate scores are floats."""
        from app.services.TrendDataService import TrendDataService
        keywords = TrendDataService._fallback_keywords("marketing")
        for kw in keywords:
            assert isinstance(kw["trend_score"], float), f"trend_score not float: {kw}"
            assert isinstance(kw["growth_rate"], float), f"growth_rate not float: {kw}"


class TestPerformanceAnalyticsServiceUnit:
    """Test PerformanceAnalyticsService helper methods."""

    def test_unit_classify_format_image(self):
        """has_image=True → format is 'image'."""
        from app.services.PerformanceAnalyticsService import PerformanceAnalyticsService
        assert PerformanceAnalyticsService._classify_format({"has_image": True, "content": "short"}) == "image"

    def test_unit_classify_format_long_form(self):
        """Long text with no image → 'long_form'."""
        from app.services.PerformanceAnalyticsService import PerformanceAnalyticsService
        draft = {"has_image": False, "content": "x" * 600}
        assert PerformanceAnalyticsService._classify_format(draft) == "long_form"

    def test_unit_classify_format_text(self):
        """Short text with no image → 'text'."""
        from app.services.PerformanceAnalyticsService import PerformanceAnalyticsService
        draft = {"has_image": False, "content": "short post"}
        assert PerformanceAnalyticsService._classify_format(draft) == "text"

    def test_unit_extract_topics_finance(self):
        """Finance keywords in content → 'finance' topic extracted."""
        from app.services.PerformanceAnalyticsService import PerformanceAnalyticsService
        topics = PerformanceAnalyticsService._extract_topics("Here are some investment tips and savings strategies")
        assert "finance" in topics

    def test_unit_extract_topics_education(self):
        """Education keywords → 'education' topic extracted."""
        from app.services.PerformanceAnalyticsService import PerformanceAnalyticsService
        topics = PerformanceAnalyticsService._extract_topics("A beginner guide on how to grow your brand")
        assert "education" in topics

    def test_unit_extract_topics_max_3(self):
        """At most 3 topics extracted per post."""
        from app.services.PerformanceAnalyticsService import PerformanceAnalyticsService
        content = "investment tips guide how to learn marketing brand sales business"
        topics = PerformanceAnalyticsService._extract_topics(content)
        assert len(topics) <= 3

    def test_unit_empty_returns_correct_shape(self):
        """_empty() returns the expected structure."""
        from app.services.PerformanceAnalyticsService import PerformanceAnalyticsService
        result = PerformanceAnalyticsService._empty()
        assert result["has_data"] is False
        assert result["post_count"] == 0
        assert isinstance(result["top_formats"], list)
        assert isinstance(result["top_topics"], list)


# ══════════════════════════════════════════════════════════════════════════════
# API TESTS — require running server (staging or local)
# ══════════════════════════════════════════════════════════════════════════════

class TestCalendarAPIEndpoints:
    """Integration tests against the live API endpoints."""

    def test_api_performance_endpoint_requires_auth(self, client):
        """GET /content-calendar/performance requires authentication."""
        r = client.get("/social-media/content-calendar/performance")
        assert r.status_code in (401, 403), f"Expected auth error, got {r.status_code}: {r.text}"

    def test_api_trends_endpoint_requires_auth(self, client):
        """GET /content-calendar/trends requires authentication."""
        r = client.get("/social-media/content-calendar/trends")
        assert r.status_code in (401, 403), f"Expected auth error, got {r.status_code}: {r.text}"

    def test_api_performance_endpoint_returns_data(self, client, auth_headers):
        """Authenticated user gets performance data."""
        r = client.get("/social-media/content-calendar/performance", headers=auth_headers)
        assert r.status_code == 200, f"Unexpected status: {r.status_code} — {r.text}"
        body = r.json()
        assert body.get("status") is True
        # responseData IS the performance dict directly
        perf = body["responseData"]
        assert "has_data" in perf,    f"Missing 'has_data' in response: {perf}"
        assert "post_count" in perf,  f"Missing 'post_count' in response: {perf}"
        assert "top_formats" in perf, f"Missing 'top_formats' in response: {perf}"
        assert "top_topics" in perf,  f"Missing 'top_topics' in response: {perf}"

    def test_api_trends_endpoint_returns_keywords(self, client, auth_headers):
        """Authenticated user gets trending keywords."""
        r = client.get("/social-media/content-calendar/trends", headers=auth_headers)
        assert r.status_code == 200, f"Unexpected status: {r.status_code} — {r.text}"
        body = r.json()
        assert body.get("status") is True
        # responseData IS the trends dict directly
        trends = body["responseData"]
        assert "industry" in trends,  f"Missing 'industry' in response: {trends}"
        assert "keywords" in trends,  f"Missing 'keywords' in response: {trends}"
        assert isinstance(trends["keywords"], list), "keywords should be a list"
        assert trends["count"] == len(trends["keywords"]), "count mismatch"

    def test_api_generate_calendar_uses_data_driven_pipeline(self, client, auth_headers):
        """Generated calendar includes scoring fields and generation_method."""
        r = client.post(
            "/social-media/content-calendar/plan/generate",
            json={"platforms": ["instagram"], "force_regenerate": True},
            headers=auth_headers,
            timeout=90,
        )
        assert r.status_code == 200, f"Unexpected status: {r.status_code} — {r.text}"
        body = r.json()
        assert body.get("status") is True

        # responseData IS the plan dict directly (key name is "calendar_plan")
        plan = body["responseData"]
        assert "generation_method" in plan, f"Plan missing generation_method: {plan.keys()}"
        assert plan["generation_method"] in ("data_driven", "trend_driven", "ai"), (
            f"Unexpected generation_method: {plan['generation_method']}"
        )

        days = plan.get("days", [])
        assert len(days) == 7, f"Expected 7 days, got {len(days)}"

        for day in days:
            assert "title" in day and day["title"],     f"Day missing title: {day}"
            assert "content_type" in day,               f"Day missing content_type: {day}"
            assert "final_score" in day,                f"Day missing final_score: {day}"
            assert "trend_score" in day,                f"Day missing trend_score: {day}"
            assert "performance_score" in day,          f"Day missing performance_score: {day}"
            assert "reason" in day and day["reason"],   f"Day missing reason (explanation layer): {day}"

    def test_api_calendar_days_have_correct_dates(self, client, auth_headers):
        """Each calendar day falls within the correct week."""
        from datetime import datetime
        r = client.post(
            "/social-media/content-calendar/plan/generate",
            json={"platforms": ["instagram"], "force_regenerate": True},
            headers=auth_headers,
            timeout=90,
        )
        assert r.status_code == 200, f"Unexpected status: {r.status_code} — {r.text}"
        plan = r.json()["responseData"]
        days = plan.get("days", [])
        dates = [d["date"] for d in days]
        parsed = [datetime.strptime(d, "%Y-%m-%d") for d in dates]

        # All 7 dates should be within one week and in order
        assert parsed == sorted(parsed), "Calendar days are not in date order"
        assert (parsed[-1] - parsed[0]).days == 6, "Calendar does not span exactly 7 days"

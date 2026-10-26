"""
Live-reported bug: every listening opportunity card suggested Instagram +
carousel, with no variation — traced to the AI prompt never being given
what's actually connected, what the owner stated in their Playbook, or
what's genuinely performed best for this business; it had nothing to react
to, so it defaulted to its own training bias. These tests cover the real
fix: personalize._platform_format_facts grounds the prompt in real data
(in priority order — connections > stated preference > real performance
history > a sensible varied guess, matching SYSTEM's own instructions),
and PerformanceAnalyticsService now actually computes per-platform
engagement (the field was already fetched from each draft and silently
unused before this). Uses an in-memory MongoDB; no network, no AI.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.market_intelligence.listening import personalize
from app.services.PerformanceAnalyticsService import PerformanceAnalyticsService


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["platform_grounding_test"]


def _patch_connections(connected):
    return patch(
        "app.agents.social_media_manager.services.social_account_service.SocialAccountService"
        ".get_user_connections",
        new=AsyncMock(return_value={"status": True, "responseData": {"connected_platforms": list(connected)}}),
    )


PROFILE = {"user_id": "u1", "brand_id": "b1", "facts": {}}


class TestPlatformFormatFacts:
    def test_connected_platforms_always_listed_and_ad_accounts_filtered(self):
        db = fresh_db()
        with _patch_connections(["instagram", "facebook_ads", "tiktok", "google_ads"]):
            out = run(personalize._platform_format_facts(db, PROFILE))
        assert "connected_platforms: instagram, tiktok" in out
        assert "facebook_ads" not in out
        assert "google_ads" not in out

    def test_no_connections_is_reported_honestly_not_silently_omitted(self):
        db = fresh_db()
        with _patch_connections([]):
            out = run(personalize._platform_format_facts(db, PROFILE))
        assert "connected_platforms: none connected yet" in out

    def test_stated_playbook_preference_is_surfaced_when_present(self):
        db = fresh_db()
        profile = {**PROFILE, "facts": {"target_platforms": ["tiktok", "facebook"], "preferred_formats": ["video"]}}
        with _patch_connections(["instagram", "tiktok", "facebook"]):
            out = run(personalize._platform_format_facts(db, profile))
        assert "stated_platform_preference (from the owner's own Playbook): tiktok, facebook" in out
        assert "stated_format_preference (from the owner's own Playbook): video" in out

    def test_no_stated_preference_means_no_fabricated_preference_line(self):
        db = fresh_db()
        with _patch_connections(["instagram"]):
            out = run(personalize._platform_format_facts(db, PROFILE))
        assert "stated_platform_preference" not in out
        assert "stated_format_preference" not in out

    def test_real_performance_history_is_surfaced_when_it_exists(self):
        db = fresh_db()
        with _patch_connections(["instagram", "facebook", "tiktok"]), patch(
            "app.services.PerformanceAnalyticsService.PerformanceAnalyticsService.get_user_performance",
            new=AsyncMock(return_value={
                "has_data": True, "top_platforms": ["facebook", "tiktok"], "top_formats": ["carousel", "video"],
            }),
        ):
            out = run(personalize._platform_format_facts(db, PROFILE))
        assert "best_performing_platforms" in out and "facebook, tiktok" in out
        assert "best_performing_formats" in out and "carousel, video" in out

    def test_no_performance_history_yet_means_no_fabricated_best_performer_line(self):
        db = fresh_db()
        with _patch_connections(["instagram"]), patch(
            "app.services.PerformanceAnalyticsService.PerformanceAnalyticsService.get_user_performance",
            new=AsyncMock(return_value={"has_data": False, "top_platforms": [], "top_formats": []}),
        ):
            out = run(personalize._platform_format_facts(db, PROFILE))
        assert "best_performing_platforms" not in out
        assert "best_performing_formats" not in out

    def test_a_lookup_failure_degrades_gracefully_not_an_exception(self):
        db = fresh_db()
        with patch(
            "app.agents.social_media_manager.services.social_account_service.SocialAccountService"
            ".get_user_connections",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            out = run(personalize._platform_format_facts(db, PROFILE))
        assert "connected_platforms: none connected yet" in out


class TestFallbackActionIsAlsoGrounded:
    """_fallback_action is the deterministic, zero-fabrication-risk safety
    net used when the AI's output fails validation twice — it used to
    hardcode "instagram" here too, the exact same ungrounded-default bug,
    just on a path that never calls the AI at all (so fixing it needs no
    AI mocking — a real connected-platforms list is enough)."""

    def test_picks_the_first_real_connected_platform_not_instagram(self):
        action = personalize._fallback_action(
            {"topic_name": "latasha wig", "topic": "latasha wig"}, connected=["tiktok", "facebook"]
        )
        assert action["platforms"] == ["tiktok"]

    def test_falls_back_to_instagram_only_when_nothing_is_connected(self):
        action = personalize._fallback_action({"topic_name": "x", "topic": "x"}, connected=[])
        assert action["platforms"] == ["instagram"]

    def test_omitting_connected_entirely_keeps_the_old_default_behavior(self):
        """Regression guard for the pre-existing direct-call test in
        test_listening_engine.py, which calls _fallback_action with no
        `connected` argument at all."""
        action = personalize._fallback_action({"topic_name": "x", "topic": "x"})
        assert action["platforms"] == ["instagram"]


class TestRetryPathIsAlsoGrounded:
    def test_judge_one_retry_includes_platform_facts_in_its_prompt(self):
        from types import SimpleNamespace
        from app.services import AIService as ai_module

        captured = {}

        def fake_build(messages, **kw):
            captured["messages"] = messages
            return SimpleNamespace(messages=messages)

        async def fake_structured(req, response_model):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(parsed=None))])

        db = fresh_db()
        with _patch_connections(["tiktok"]), patch.object(
            ai_module.AIService, "build_ai_model", staticmethod(fake_build)
        ), patch.object(ai_module.AIService, "structured_chat_completion", new=AsyncMock(side_effect=fake_structured)):
            run(personalize._judge_one_retry(
                db, PROFILE, {"topic_name": "x", "topic": "x", "type": "rising"}, "fabricated number", "gpt-4o-mini"
            ))
        prompt_text = " ".join(m["content"] for m in captured["messages"] if "content" in m)
        assert "PLATFORM & FORMAT FACTS" in prompt_text
        assert "connected_platforms: tiktok" in prompt_text


class TestPerPlatformPerformance:
    """PerformanceAnalyticsService already fetched `platform` on every draft
    but never aggregated it — the exact same data used for avg_engagement_by_format
    now also produces avg_engagement_by_platform/top_platforms."""

    async def _seed(self, db, brand_id="b1"):
        await db["content_drafts"].insert_many([
            {"id": "d1", "brand_id": brand_id, "status": "published", "platform": "instagram",
             "content": "short", "has_image": True, "published_date": "2026-10-01T18:00:00Z"},
            {"id": "d2", "brand_id": brand_id, "status": "published", "platform": "facebook",
             "content": "short", "has_image": True, "published_date": "2026-10-02T18:00:00Z"},
            {"id": "d3", "brand_id": brand_id, "status": "published", "platform": "facebook",
             "content": "short", "has_image": True, "published_date": "2026-10-03T18:00:00Z"},
        ])
        await db["content_analytics"].insert_many([
            # instagram: low engagement
            {"draft_id": "d1", "likes": 1, "comments": 0, "shares": 0, "impressions": 1000},
            # facebook: consistently high engagement across both its posts
            {"draft_id": "d2", "likes": 80, "comments": 20, "shares": 10, "impressions": 1000},
            {"draft_id": "d3", "likes": 90, "comments": 15, "shares": 5, "impressions": 1000},
        ])

    def test_top_platforms_reflects_real_engagement_not_posting_frequency(self):
        """instagram has 1 post, facebook has 2 — but facebook must win on
        actual engagement rate, not on post count."""
        db = fresh_db()
        run(self._seed(db))
        perf = run(PerformanceAnalyticsService.get_user_performance("u1", db, brand_id="b1"))
        assert perf["has_data"] is True
        assert perf["top_platforms"][0] == "facebook"
        assert perf["avg_engagement_by_platform"]["facebook"] > perf["avg_engagement_by_platform"]["instagram"]

    def test_no_published_drafts_returns_the_empty_shape_with_platform_keys(self):
        db = fresh_db()
        perf = run(PerformanceAnalyticsService.get_user_performance("u-none", db, brand_id="b-none"))
        assert perf["has_data"] is False
        assert perf["avg_engagement_by_platform"] == {}
        assert perf["top_platforms"] == []

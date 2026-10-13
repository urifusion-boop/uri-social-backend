"""
End-to-end checks, against an in-memory MongoDB, of the Playbook features
that run without a request: Auto-tab auto-publishing, the scheduled alerts,
and how "post ready" / "post published" notifications are routed.

Needs mongomock-motor (test-only; skipped when it isn't installed).
"""
import asyncio
from datetime import datetime, timedelta

import pytest

pytest.importorskip("mongomock_motor")

from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from app.agents.social_media_manager.services.auto_content_service import AutoContentService  # noqa: E402
from app.services import brand_alert_jobs as jobs  # noqa: E402
from app.services import notification_channels  # noqa: E402

USER, BRAND = "u1", "brand_1"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def db():
    return AsyncMongoMockClient()["uri_test"]


@pytest.fixture()
def sent(monkeypatch):
    """Every external send is captured instead of going out."""
    log = []

    def fake(name):
        async def _send(*args, **kwargs):
            log.append((name, args[1] if len(args) > 1 else ""))
            return True, None
        return _send

    for name in ("send_email", "send_whatsapp", "send_sms", "send_slack", "send_telegram"):
        monkeypatch.setattr(notification_channels, name, fake(name))
    return log


def profile(**extra):
    return {"user_id": USER, "brand_id": BRAND, "brand_name": "Bukka Express", "industry": "Restaurant",
            "region": "Nigeria", **extra}


async def seed(db, prof, drafts=(), analytics=()):
    await db["brand_profiles"].insert_one(dict(prof))
    await db["users"].insert_one({"userId": USER, "email": "owner@bukka.ng"})
    if drafts:
        await db["content_drafts"].insert_many([dict(d) for d in drafts])
    if analytics:
        await db["content_analytics"].insert_many([dict(a) for a in analytics])


# ── Auto-publish ─────────────────────────────────────────────────────────────

class TestAutoPublish:
    def _drafts(self):
        return [
            {"id": "fb1", "user_id": USER, "brand_id": BRAND, "platform": "facebook", "status": "draft",
             "content": "Hot jollof", "post_type": "feed", "image_url": "https://img/x.jpg"},
            {"id": "ig_no_image", "user_id": USER, "brand_id": BRAND, "platform": "instagram", "status": "draft",
             "content": "Hot jollof", "post_type": "feed"},
        ]

    def test_schedules_at_the_brands_posting_time_and_leaves_unpublishable_drafts(self, db):
        prof = profile(approval_workflow="auto", posting_time_mode="general",
                       posting_time_prefs={"general": "Evening (4-8pm),"})

        async def go():
            await seed(db, prof, self._drafts())
            await AutoContentService._auto_schedule(USER, db, self._drafts(), prof)
            return (await db["content_drafts"].find_one({"id": "fb1"}),
                    await db["content_drafts"].find_one({"id": "ig_no_image"}))

        fb, ig = run(go())
        assert fb["status"] in ("scheduled", "staging_scheduled")
        assert fb["scheduled_date"].hour == 17 and fb["scheduled_date"] > datetime.utcnow()   # 18:00 Lagos
        assert "Auto-published" in fb["approval_notes"]
        # Instagram needs an image: the normal publish check refuses it, so it stays a draft for review.
        assert ig["status"] == "draft"


# ── Scheduled alerts ─────────────────────────────────────────────────────────

def published(pid, days_ago, platform="instagram", content="Jollof Friday"):
    when = datetime.utcnow() - timedelta(days=days_ago)
    return {"id": pid, "user_id": USER, "brand_id": BRAND, "platform": platform, "status": "published",
            "content": content, "published_date": when.isoformat(), "updated_at": when}


def stats(pid, impressions, interactions):
    return {"draft_id": pid, "impressions": impressions, "likes": interactions, "comments": 0, "shares": 0}


async def notifications(db):
    return await db["notifications"].find({"user_id": USER}).to_list(length=50)


class TestWeeklyReport:
    def test_sent_once_to_the_chosen_channel(self, db, sent):
        prof = profile(notification_events=["Weekly performance report"], notification_channel="Email")

        async def go():
            await seed(db, prof, [published("a", 2), published("b", 3), published("c", 10)],
                       [stats("a", 1000, 50), stats("b", 500, 10), stats("c", 800, 16)])
            first = await jobs.run_weekly_reports(db)
            second = await jobs.run_weekly_reports(db)     # same week again
            return first, second, await notifications(db)

        first, second, notes = run(go())
        assert first == {"weekly_reports_sent": 1} and second == {"weekly_reports_sent": 0}
        assert [n["type"] for n in notes] == ["weekly_report"]
        assert "Posts published: 2 (last week: 1)" in notes[0]["metadata"]["message"]
        assert [name for name, _ in sent] == ["send_email"]

    def test_brands_that_did_not_ask_get_nothing(self, db, sent):
        async def go():
            await seed(db, profile(notification_events=["Post published"], notification_channel="Email"),
                       [published("a", 2)], [stats("a", 1000, 50)])
            return await jobs.run_weekly_reports(db), await notifications(db)

        result, notes = run(go())
        assert result == {"weekly_reports_sent": 0} and notes == [] and sent == []


class TestEngagementSpike:
    def test_a_post_far_above_the_usual_rate_is_reported_once(self, db, sent):
        prof = profile(notification_events=["Engagement spike detected"], notification_channel="Slack",
                       slack_webhook_url="https://hooks.slack.com/services/T/B/x")
        baseline = [published(f"old{i}", 20 + i) for i in range(6)]
        base_stats = [stats(f"old{i}", 1000, 20) for i in range(6)]            # 2% usual

        async def go():
            await seed(db, prof, baseline + [published("hot", 1, content="Party trays are back")],
                       base_stats + [stats("hot", 2000, 200)])                  # 10%
            first = await jobs.run_engagement_spikes(db)
            second = await jobs.run_engagement_spikes(db)
            return first, second, await notifications(db)

        first, second, notes = run(go())
        assert first == {"engagement_spikes_sent": 1} and second == {"engagement_spikes_sent": 0}
        assert "Party trays are back" in notes[0]["metadata"]["message"]
        assert [name for name, _ in sent] == ["send_slack"]


class TestCalendarRefill:
    def _plan(self, days_left):
        end = (datetime.utcnow().date() + timedelta(days=days_left)).strftime("%Y-%m-%d")
        return {"plan_id": "p1", "user_id": USER, "brand_id": BRAND, "status": "active",
                "period_end": end, "created_at": datetime.utcnow() - timedelta(days=25)}

    def _run(self, db, days_left):
        prof = profile(notification_events=["Content calendar needs refill"], notification_channel="Email")

        async def go():
            await seed(db, prof)
            await db["content_calendar_v2_plans"].insert_one(self._plan(days_left))
            return await jobs.run_calendar_refill(db), await jobs.run_calendar_refill(db), await notifications(db)

        return run(go())

    def test_plan_about_to_end(self, db, sent):
        first, second, notes = self._run(db, days_left=3)
        assert first == {"calendar_refills_sent": 1} and second == {"calendar_refills_sent": 0}
        assert "ends in 3 days" in notes[0]["metadata"]["message"]

    def test_plan_with_plenty_of_time_left(self, db, sent):
        first, _, notes = self._run(db, days_left=20)
        assert first == {"calendar_refills_sent": 0} and notes == []


class TestTrendingTopic:
    def _run(self, db, monkeypatch, keywords):
        from app.services.TrendDataService import TrendDataService

        async def fake_trends(**kwargs):
            return keywords

        monkeypatch.setattr(TrendDataService, "get_trending_keywords", staticmethod(fake_trends))
        prof = profile(notification_events=["Trending topic in your niche"], notification_channel="Email")

        async def go():
            await seed(db, prof)
            return await jobs.run_trending_topics(db), await notifications(db)

        return run(go())

    def test_real_riser_is_reported(self, db, sent, monkeypatch):
        result, notes = self._run(db, monkeypatch, [
            {"keyword": "small chops", "source": "google_trends", "growth_rate": 120}])
        assert result == {"trending_topics_sent": 1}
        assert notes[0]["subject"] == "Trending now: small chops"

    def test_placeholder_keywords_are_never_reported(self, db, sent, monkeypatch):
        result, notes = self._run(db, monkeypatch, [
            {"keyword": "restaurant tips", "source": "fallback", "growth_rate": 500}])
        assert result == {"trending_topics_sent": 0} and notes == [] and sent == []


# ── Routing of the existing post notifications ───────────────────────────────

class TestPostNotificationRouting:
    def _service(self, db, monkeypatch):
        from app.services import NotificationService as ns_module

        monkeypatch.setattr(ns_module, "get_db", lambda: db)
        return ns_module.NotificationService()

    def test_brand_with_preferences_uses_them(self, db, sent, monkeypatch):
        prof = profile(notification_events=["Post published"], notification_channel="Telegram", telegram_chat_id="555")
        service = self._service(db, monkeypatch)

        async def go():
            await seed(db, prof)
            await service.notify_content_posted(user_id=USER, platform="Instagram", content_preview="Hot jollof",
                                                brand_id=BRAND)
            return await notifications(db)

        notes = run(go())
        assert [name for name, _ in sent] == ["send_telegram"]
        assert len(notes) == 1 and notes[0]["type"] == "content_posted"

    def test_alert_switched_off_sends_nothing(self, db, sent, monkeypatch):
        prof = profile(notification_events=["Weekly performance report"], notification_channel="Email")
        service = self._service(db, monkeypatch)

        async def go():
            await seed(db, prof)
            await service.notify_content_posted(user_id=USER, platform="Instagram", brand_id=BRAND)
            return await notifications(db)

        assert run(go()) == [] and sent == []


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))

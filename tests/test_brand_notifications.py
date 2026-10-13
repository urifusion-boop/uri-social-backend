"""
Brand Playbook notifications: preferences -> channels, and the scheduled
alerts' decision logic (pure functions — no network, no database).
"""
from datetime import datetime

import pytest

from app.services import brand_alert_jobs as jobs
from app.services import brand_notifier as bn
from app.services import notification_channels as ch


class TestPreferences:
    def test_unconfigured_brand_keeps_old_email_behaviour(self):
        prefs = bn.preferences({})
        assert not prefs["configured"]
        assert bn.channels_for("post_ready", prefs) == ["email"]
        assert bn.channels_for("post_published", prefs) == ["email"]
        assert bn.channels_for("weekly_report", prefs) == []

    def test_chosen_channel_is_used(self):
        prefs = bn.preferences({"notification_events": ["Post published"], "notification_channel": "Slack"})
        assert bn.channels_for("post_published", prefs) == ["slack"]

    def test_event_switched_off_sends_nothing(self):
        prefs = bn.preferences({"notification_events": ["Weekly performance report"], "notification_channel": "Email"})
        assert bn.channels_for("post_published", prefs) == []

    def test_post_ready_also_goes_to_approval_channels(self):
        prefs = bn.preferences({
            "notification_events": ["Post ready for review"],
            "notification_channel": "Email",
            "approval_channels": ["Dashboard", "WhatsApp", "Email"],
        })
        assert bn.channels_for("post_ready", prefs) == ["email", "whatsapp"]

    def test_alert_chosen_without_channel_falls_back_to_email_for_posts(self):
        prefs = bn.preferences({"notification_events": ["Post published"]})
        assert bn.channels_for("post_published", prefs) == ["email"]
        prefs2 = bn.preferences({"notification_events": ["Weekly performance report"]})
        assert bn.channels_for("weekly_report", prefs2) == []

    def test_every_onboarding_label_is_known(self):
        for label in ["Post ready for review", "Post published", "Weekly performance report",
                      "Trending topic in your niche", "Content calendar needs refill", "Engagement spike detected"]:
            assert label in bn.EVENTS
        for label in ["Email", "WhatsApp", "SMS", "Push Notification", "Slack", "Telegram", "Dashboard"]:
            assert label in bn.CHANNELS


class TestSlackWebhookCheck:
    def test_only_slack_hooks_accepted(self):
        assert ch.is_slack_webhook("https://hooks.slack.com/services/T0/B0/xyz")
        assert not ch.is_slack_webhook("https://evil.example.com/hook")
        assert not ch.is_slack_webhook("")


def _post(rate, impressions=1000, platform="instagram", content="Jollof Friday is here", pid="p"):
    return {"id": pid, "platform": platform, "content": content, "impressions": impressions,
            "likes": rate * impressions / 100, "comments": 0.0, "shares": 0.0, "engagement_rate": rate,
            "_published": datetime(2026, 10, 10)}


class TestWeeklyReport:
    def test_no_posts(self):
        title, message = jobs.compose_weekly_report([], [])
        assert "No posts went out" in message

    def test_totals_change_and_best_post(self):
        this = [_post(2.0, 1000, pid="a"), _post(8.0, 500, content="Best one", pid="b")]
        last = [_post(2.0, 1000, pid="c")]
        title, message = jobs.compose_weekly_report(this, last)
        assert "Posts published: 2 (last week: 1)" in message
        assert "Views: 1,500 (+50% vs last week)" in message
        assert '"Best one"' in message and "8.0% engagement" in message


class TestEngagementSpikes:
    def test_needs_a_baseline(self):
        assert jobs.find_spikes([_post(20.0)], [_post(2.0)] * 3) == []

    def test_double_the_usual_rate_is_a_spike(self):
        baseline = [_post(2.0, pid=str(i)) for i in range(6)]
        spikes = jobs.find_spikes([_post(5.0, pid="hot"), _post(3.0, pid="meh")], baseline)
        assert [s["id"] for s in spikes] == ["hot"]

    def test_tiny_posts_ignored(self):
        baseline = [_post(2.0, pid=str(i)) for i in range(6)]
        assert jobs.find_spikes([_post(30.0, impressions=40)], baseline) == []


class TestTrendingTopic:
    def test_never_uses_fallback_placeholders(self):
        assert jobs.pick_trend([{"keyword": "food tips", "source": "fallback", "growth_rate": 900}]) is None

    def test_picks_strongest_real_riser(self):
        kws = [
            {"keyword": "small chops", "source": "google_trends", "growth_rate": 60},
            {"keyword": "jollof", "source": "google_trends", "growth_rate": 140},
            {"keyword": "suya", "source": "google_trends", "growth_rate": 20},
        ]
        assert jobs.pick_trend(kws)["keyword"] == "jollof"

    def test_small_growth_isnt_news(self):
        assert jobs.pick_trend([{"keyword": "x", "source": "google_trends", "growth_rate": 10}]) is None


class TestWiring:
    def test_channels_router_imports(self):
        from app.routers.notification_channels_router import router
        paths = {r.path for r in router.routes}
        assert "/notification-channels/telegram/webhook" in paths
        assert "/notification-channels/push/subscribe" in paths

    def test_scheduler_registers_alert_jobs(self):
        import inspect
        from app.services import notification_scheduler as ns
        src = inspect.getsource(ns.start_notification_scheduler)
        for job in ("brand_weekly_reports", "brand_engagement_spikes", "brand_calendar_refill", "brand_trending_topics"):
            assert job in src


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=None):
        return list(self.docs)


class _Coll:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    async def find_one(self, query, *a, **k):
        for d in self.docs:
            if all(d.get(key) == val for key, val in query.items() if not isinstance(val, dict)):
                return d
        return None

    def find(self, query=None, *a, **k):
        return _Cursor(self.docs)

    async def insert_one(self, doc):
        self.docs.append(doc)

    async def delete_many(self, query):
        pass


class _DB(dict):
    def __getitem__(self, name):
        if name not in self:
            self[name] = _Coll()
        return dict.__getitem__(self, name)


class TestNotifyEndToEnd:
    """notify() with a fake database and faked channel senders."""

    def _run(self, profile, monkeypatch, event="post_published"):
        import asyncio

        sent = []

        async def fake(name):
            async def _f(*args, **kwargs):
                sent.append(name)
                return True, None
            return _f

        async def setup():
            for name in ("send_email", "send_whatsapp", "send_sms", "send_slack", "send_telegram"):
                monkeypatch.setattr(ch, name, await fake(name))

        asyncio.run(setup())
        db = _DB()
        db["users"] = _Coll([{"userId": "u1", "email": "a@b.co", "whatsapp_phone": "+2348000000000"}])
        result = asyncio.run(bn.notify(db, "u1", "b1", event, "Title", "Message", profile=profile))
        return result, sent, db["notifications"].docs

    def test_one_panel_record_per_alert(self, monkeypatch):
        profile = {"notification_events": ["Post ready for review"], "notification_channel": "Slack",
                   "approval_channels": ["WhatsApp", "Dashboard"], "slack_webhook_url": "https://hooks.slack.com/x"}
        result, sent, records = self._run(profile, monkeypatch, "post_ready")
        assert sent == ["send_slack", "send_whatsapp"]
        assert len(records) == 1
        assert records[0]["type"] == "content_created"
        assert set(records[0]["metadata"]["deliveries"]) == {"slack", "whatsapp", "dashboard"}

    def test_switched_off_alert_sends_and_logs_nothing(self, monkeypatch):
        result, sent, records = self._run({"notification_events": ["Weekly performance report"],
                                           "notification_channel": "Email"}, monkeypatch)
        assert result == {"skipped": "not enabled"} and sent == [] and records == []

    def test_unconfigured_brand_gets_email(self, monkeypatch):
        result, sent, records = self._run({}, monkeypatch)
        assert sent == ["send_email"] and len(records) == 1


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))

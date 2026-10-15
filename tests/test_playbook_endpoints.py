"""
End-to-end checks of the Brand Playbook endpoints against an in-memory
MongoDB: the request goes through the real routers, dependencies and
services, and the result is read back from the database.

Needs mongomock-motor (test-only; skipped when it isn't installed).
"""
from datetime import datetime, timedelta

import pytest

pytest.importorskip("mongomock_motor")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from app.agents.social_media_manager.routers.complete_social_manager import router as smm_router  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.dependencies import get_active_brand_context, get_db_dependency  # noqa: E402
from app.routers.notification_channels_router import router as channels_router  # noqa: E402
from app.services import notification_channels  # noqa: E402

USER, BRAND = "u1", "brand_1"

PROFILE = {
    "user_id": USER, "brand_id": BRAND, "brand_name": "Bukka Express", "industry": "Restaurant",
    "region": "Nigeria", "product_description": "Home-style Nigerian food, delivered",
    "guardrails": {"avoid_topics": "politics"}, "words_to_avoid": ["cheap"],
}


@pytest.fixture()
def env():
    db = AsyncMongoMockClient()["uri_test"]
    app = FastAPI()
    app.include_router(smm_router, prefix="/social-media")
    app.include_router(channels_router, prefix="/social-media")
    app.dependency_overrides[get_db_dependency] = lambda: db
    app.dependency_overrides[get_active_brand_context] = lambda: {"user_id": USER, "brand_id": BRAND, "agency_id": None}
    with TestClient(app) as client:
        client.portal.call(db["brand_profiles"].insert_one, dict(PROFILE))
        client.portal.call(db["users"].insert_one, {"userId": USER, "email": "owner@bukka.ng"})
        yield client, db


def data(response):
    assert response.status_code == 200, response.text
    return response.json()["responseData"]


def profile_in_db(client, db):
    return client.portal.call(db["brand_profiles"].find_one, {"brand_id": BRAND})


class TestHealth:
    def test_scores_the_saved_playbook(self, env):
        client, db = env
        health = data(client.get("/social-media/brand-profile/health"))
        assert 0 < health["score"] < 100
        assert health["used_in"]["Brand name"]
        assert "brand_name" not in {s["key"] for s in health["suggestions"]}
        assert "logo_url" in {s["key"] for s in health["suggestions"]}


class TestNextPostingTime:
    def test_uses_playbook_slots_in_brand_timezone(self, env):
        client, db = env
        client.portal.call(db["brand_profiles"].update_one, {"brand_id": BRAND}, {"$set": {
            "posting_time_mode": "general", "posting_time_prefs": {"general": "Evening (4-8pm),"}}})
        result = data(client.get("/social-media/brand-profile/next-posting-time", params={"platform": "instagram"}))
        when = datetime.fromisoformat(result["scheduled_datetime"].rstrip("Z"))
        assert when.hour == 17 and when.minute == 0        # 18:00 Lagos = 17:00 UTC
        assert when > datetime.utcnow() and result["timezone"] == "Africa/Lagos"


def _edited_draft(i):
    return {"id": f"d{i}", "user_id": USER, "brand_id": BRAND, "updated_at": datetime.utcnow(),
            "human_edits": {"1": {"changes": {"content": {
                "old": f"Fresh jollof today 🍛🔥 batch {i}", "new": f"Fresh jollof today batch {i}"}}}}}


class TestSuggestions:
    def _seed_emoji_habit(self, client, db):
        client.portal.call(db["content_drafts"].insert_many, [_edited_draft(i) for i in range(5)])

    def test_nothing_to_suggest_without_evidence(self, env):
        client, db = env
        assert data(client.get("/social-media/brand-profile/suggestions"))["suggestions"] == []

    def test_suggest_then_apply_updates_the_playbook(self, env):
        client, db = env
        self._seed_emoji_habit(client, db)
        suggestions = data(client.get("/social-media/brand-profile/suggestions"))["suggestions"]
        assert [s["id"] for s in suggestions] == ["emoji_none"]
        assert "apply" not in suggestions[0]            # the change itself never leaves the server

        updated = data(client.post("/social-media/brand-profile/suggestions/apply", json={"id": "emoji_none"}))
        assert updated["guardrails"] == {"avoid_topics": "politics", "emoji_usage": "no"}
        assert profile_in_db(client, db)["guardrails"]["emoji_usage"] == "no"
        # Once applied, it's no longer suggested.
        assert data(client.get("/social-media/brand-profile/suggestions"))["suggestions"] == []

    def test_unknown_or_stale_id_writes_nothing(self, env):
        client, db = env
        r = client.post("/social-media/brand-profile/suggestions/apply", json={"id": "emoji_none"})
        assert r.status_code == 404
        assert "emoji_usage" not in profile_in_db(client, db)["guardrails"]

    def test_dismiss_hides_it(self, env):
        client, db = env
        self._seed_emoji_habit(client, db)
        data(client.post("/social-media/brand-profile/suggestions/dismiss", json={"id": "emoji_none"}))
        assert data(client.get("/social-media/brand-profile/suggestions"))["suggestions"] == []
        assert "emoji_usage" not in profile_in_db(client, db)["guardrails"]

    def test_other_brands_edits_are_not_used(self, env):
        client, db = env
        other = [{**_edited_draft(i), "user_id": "someone_else", "brand_id": "other_brand"} for i in range(5)]
        client.portal.call(db["content_drafts"].insert_many, other)
        assert data(client.get("/social-media/brand-profile/suggestions"))["suggestions"] == []


class TestNotificationChannels:
    def test_status_reflects_what_is_connected(self, env):
        client, db = env
        status = data(client.get("/social-media/notification-channels/status"))
        assert status["email"] == {"available": True, "connected": True, "detail": "owner@bukka.ng"}
        assert status["slack"]["connected"] is False and status["telegram"]["connected"] is False

    def test_test_needs_a_channel_first(self, env):
        client, db = env
        assert client.post("/social-media/notification-channels/test", json={}).status_code == 400

    def test_test_sends_through_the_chosen_channel_and_logs_once(self, env, monkeypatch):
        client, db = env
        sent = []

        async def fake_slack(url, title, message, link):
            sent.append(url)
            return True, None

        monkeypatch.setattr(notification_channels, "send_slack", fake_slack)
        client.portal.call(db["brand_profiles"].update_one, {"brand_id": BRAND}, {"$set": {
            "notification_channel": "Slack", "slack_webhook_url": "https://hooks.slack.com/services/T/B/x"}})
        results = data(client.post("/social-media/notification-channels/test", json={}))
        assert results["slack"]["sent"] is True and sent == ["https://hooks.slack.com/services/T/B/x"]
        logged = client.portal.call(db["notifications"].count_documents, {"user_id": USER})
        assert logged == 1

    def test_push_subscribe_and_unsubscribe(self, env):
        client, db = env
        sub = {"endpoint": "https://push.example.com/abc", "keys": {"p256dh": "k", "auth": "a"}}
        data(client.post("/social-media/notification-channels/push/subscribe", json=sub))
        assert client.portal.call(db["push_subscriptions"].count_documents, {"user_id": USER}) == 1
        data(client.post("/social-media/notification-channels/push/unsubscribe", json={"endpoint": sub["endpoint"]}))
        assert client.portal.call(db["push_subscriptions"].count_documents, {"user_id": USER}) == 0

    def test_push_rejects_a_bad_subscription(self, env):
        client, db = env
        r = client.post("/social-media/notification-channels/push/subscribe",
                        json={"endpoint": "http://insecure", "keys": {"p256dh": "k", "auth": "a"}})
        assert r.status_code == 400


class TestTelegramLink:
    @pytest.fixture(autouse=True)
    def telegram_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "token")
        monkeypatch.setattr(settings, "TELEGRAM_BOT_USERNAME", "uri_bot")
        monkeypatch.setattr(settings, "TELEGRAM_WEBHOOK_SECRET", "s3cret")
        self.messages = []

        async def fake_send(chat_id, title, message, link):
            self.messages.append((chat_id, title))
            return True, None

        monkeypatch.setattr(notification_channels, "send_telegram", fake_send)

    def _code(self, client):
        url = data(client.post("/social-media/notification-channels/telegram/link"))["url"]
        assert url.startswith("https://t.me/uri_bot?start=")
        return url.split("start=")[1]

    def _webhook(self, client, text, secret="s3cret", chat_id=555):
        return client.post("/social-media/notification-channels/telegram/webhook",
                           json={"message": {"text": text, "chat": {"id": chat_id, "username": "bukka_owner"}}},
                           headers={"X-Telegram-Bot-Api-Secret-Token": secret})

    def test_start_with_the_code_connects_the_brand(self, env):
        client, db = env
        code = self._code(client)
        assert self._webhook(client, f"/start {code}").status_code == 200
        saved = profile_in_db(client, db)
        assert saved["telegram_chat_id"] == "555" and saved["telegram_username"] == "bukka_owner"
        assert data(client.get("/social-media/notification-channels/status"))["telegram"]["connected"] is True
        # A code works once.
        client.portal.call(db["brand_profiles"].update_one, {"brand_id": BRAND}, {"$unset": {"telegram_chat_id": ""}})
        self._webhook(client, f"/start {code}", chat_id=999)
        assert "telegram_chat_id" not in profile_in_db(client, db)

    def test_wrong_secret_is_refused(self, env):
        client, db = env
        code = self._code(client)
        assert self._webhook(client, f"/start {code}", secret="guess").status_code == 403
        assert "telegram_chat_id" not in profile_in_db(client, db)

    def test_expired_code_does_not_connect(self, env):
        client, db = env
        code = self._code(client)
        client.portal.call(db["telegram_link_codes"].update_one, {"code": code},
                           {"$set": {"created_at": datetime.utcnow() - timedelta(hours=2)}})
        self._webhook(client, f"/start {code}")
        assert "telegram_chat_id" not in profile_in_db(client, db)

    def test_disconnect(self, env):
        client, db = env
        self._webhook(client, f"/start {self._code(client)}")
        data(client.post("/social-media/notification-channels/telegram/disconnect"))
        assert "telegram_chat_id" not in profile_in_db(client, db)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))


def test_platform_tones_are_keyed_by_lowercase_platform():
    from app.agents.social_media_manager.services.brand_profile_service import normalize_platform_tones
    merged = normalize_platform_tones({"Instagram": "Casual", "LinkedIn": "Educational", "linkedin": "formal, no slang",
                                       "TikTok": ""})
    assert merged == {"instagram": "Casual", "linkedin": "formal, no slang"}


def test_a_partial_save_does_not_reset_the_tone_switch():
    from app.agents.social_media_manager.routers.complete_social_manager import BrandProfileRequest
    assert "same_tone_everywhere" not in BrandProfileRequest(tagline="x").dict(exclude_none=True)
    assert BrandProfileRequest(same_tone_everywhere=False).dict(exclude_none=True)["same_tone_everywhere"] is False

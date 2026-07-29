"""
Unit tests for the daily per-brand ads-connection token health check
(ads_connection.run_token_health_check — Per-Brand Page Connection plan §8).

httpx (verify_token_live), the Meta adapter, and NotificationService are all
mocked — this proves the pause+notify-once orchestration, not live Graph API
or email/WhatsApp delivery behavior.
"""
import asyncio
from unittest.mock import AsyncMock, patch

from app.agents.jane_ads.ads_connection import REQUIRED_ADS_SCOPES, run_token_health_check


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return [dict(d) for d in self._docs]


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    def _matches(self, doc, query):
        for key, val in query.items():
            if isinstance(val, dict) and "$ne" in val:
                if doc.get(key) == val["$ne"]:
                    return False
            elif doc.get(key) != val:
                return False
        return True

    def find(self, query):
        return FakeCursor([d for d in self.docs if self._matches(d, query)])

    async def find_one(self, query, sort=None):
        matches = [d for d in self.docs if self._matches(d, query)]
        return dict(matches[0]) if matches else None

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if self._matches(d, query):
                d.update(update.get("$set", {}))
                return

    async def update_many(self, query, update):
        for d in self.docs:
            if self._matches(d, query):
                d.update(update.get("$set", {}))


class FakeDb:
    def __init__(self, connections=None, campaigns=None):
        self._colls = {
            "social_connections": FakeCollection(connections),
            "jane_ads_meta_campaigns": FakeCollection(campaigns),
        }

    def __getitem__(self, name):
        return self._colls[name]


def _conn(**kw):
    base = dict(
        id="fbads_pg123", platform="facebook_ads", connection_status="active",
        brand_id="brnd_1", user_id="user_1", page_id="pg123", page_access_token="tok",
        account_name="Brand Page",
    )
    base.update(kw)
    return base


def _campaign(**kw):
    base = dict(campaign_id="cmp_1", brand_id="brnd_1")
    base.update(kw)
    return base


def _patched(valid_scopes_result, campaigns=None, connections=None):
    db = FakeDb(connections=connections if connections is not None else [_conn()],
                campaigns=campaigns if campaigns is not None else [_campaign()])
    mock_settings = patch("app.core.config.settings.META_AD_ACCOUNT_ID", "act123")
    mock_token = patch("app.core.config.settings.META_ADS_ACCESS_TOKEN", "systok")
    mock_verify = patch("app.agents.jane_ads.ads_connection.verify_token_live",
                         new=AsyncMock(return_value=valid_scopes_result))
    mock_adapter_cls = patch("app.agents.jane_ads.adapters.meta.MetaAdPlatformAdapter")
    mock_notify = patch("app.services.NotificationService.notification_service._log_notification",
                         new=AsyncMock())
    return db, mock_settings, mock_token, mock_verify, mock_adapter_cls, mock_notify


def test_healthy_token_does_nothing():
    db, ms, mt, mv, ma, mn = _patched((True, REQUIRED_ADS_SCOPES))
    with ms, mt, mv, ma as MockAdapter, mn as mock_notify:
        result = _run(run_token_health_check(db))
    assert result == {"checked": 1, "paused": 0, "notified": 0}
    MockAdapter.assert_not_called()
    mock_notify.assert_not_called()


def test_dead_token_pauses_campaign_and_notifies_once():
    db, ms, mt, mv, ma, mn = _patched((False, set()))
    with ms, mt, mv, ma as MockAdapter, mn as mock_notify:
        MockAdapter.return_value.set_delivery = AsyncMock(return_value={"status": "paused"})
        result = _run(run_token_health_check(db))
    assert result == {"checked": 1, "paused": 1, "notified": 1}
    MockAdapter.return_value.set_delivery.assert_called_once_with("cmp_1", False)
    mock_notify.assert_called_once()
    conn = _run(db["social_connections"].find_one({"id": "fbads_pg123"}))
    assert conn["token_expired_notified"] is True
    camp = _run(db["jane_ads_meta_campaigns"].find_one({"campaign_id": "cmp_1"}))
    assert camp["paused_for_token_health"] is True


def test_missing_scope_counts_as_dead():
    partial = REQUIRED_ADS_SCOPES - {"ads_management"}
    db, ms, mt, mv, ma, mn = _patched((True, partial))
    with ms, mt, mv, ma as MockAdapter, mn as mock_notify:
        MockAdapter.return_value.set_delivery = AsyncMock(return_value={"status": "paused"})
        result = _run(run_token_health_check(db))
    assert result["paused"] == 1
    assert result["notified"] == 1


def test_already_notified_connection_is_not_notified_again():
    db, ms, mt, mv, ma, mn = _patched(
        (False, set()),
        connections=[_conn(token_expired_notified=True)],
        campaigns=[_campaign(paused_for_token_health=True)],
    )
    with ms, mt, mv, ma as MockAdapter, mn as mock_notify:
        result = _run(run_token_health_check(db))
    assert result == {"checked": 1, "paused": 0, "notified": 0}
    MockAdapter.return_value.set_delivery.assert_not_called()
    mock_notify.assert_not_called()


def test_already_paused_campaign_is_skipped_but_still_checked():
    db, ms, mt, mv, ma, mn = _patched(
        (False, set()),
        campaigns=[_campaign(paused_for_token_health=True)],
    )
    with ms, mt, mv, ma as MockAdapter, mn as mock_notify:
        result = _run(run_token_health_check(db))
    assert result == {"checked": 1, "paused": 0, "notified": 1}
    MockAdapter.return_value.set_delivery.assert_not_called()

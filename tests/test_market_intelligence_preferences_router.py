"""
Uri Market Intelligence — preferences/snooze router tests (PRD §14, §16).
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.agents.market_intelligence.models import NotificationCategory, PreferencesUpdateRequest
from app.agents.market_intelligence.router import get_preferences, snooze_insight, update_preferences


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCollection:
    def __init__(self):
        self.docs: list[dict] = []

    async def find_one(self, query):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update.get("$set", {}))
                return
        if upsert:
            new_doc = dict(query)
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)


class FakeDb:
    def __init__(self):
        self._colls: dict[str, FakeCollection] = {}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


CTX = {"user_id": "u1", "brand_id": "b1"}


def test_get_preferences_creates_defaults_on_first_use():
    db = FakeDb()
    res = _run(get_preferences(ctx=CTX, db=db))
    prefs = res["responseData"]
    assert prefs["email_enabled"] is False
    assert prefs["timezone"] == "Africa/Lagos"


def test_update_preferences_toggles_email_enabled():
    db = FakeDb()
    _run(get_preferences(ctx=CTX, db=db))
    res = _run(update_preferences(PreferencesUpdateRequest(email_enabled=True), ctx=CTX, db=db))
    assert res["responseData"]["email_enabled"] is True


def test_update_preferences_mutes_and_unmutes_topic():
    db = FakeDb()
    _run(get_preferences(ctx=CTX, db=db))
    res = _run(update_preferences(PreferencesUpdateRequest(mute_topic_id="t1"), ctx=CTX, db=db))
    assert "t1" in res["responseData"]["muted_topic_ids"]

    res2 = _run(update_preferences(PreferencesUpdateRequest(unmute_topic_id="t1"), ctx=CTX, db=db))
    assert "t1" not in res2["responseData"]["muted_topic_ids"]


def test_update_preferences_mutes_category():
    db = FakeDb()
    _run(get_preferences(ctx=CTX, db=db))
    res = _run(update_preferences(
        PreferencesUpdateRequest(mute_category=NotificationCategory.USEFUL_PATTERN), ctx=CTX, db=db
    ))
    assert NotificationCategory.USEFUL_PATTERN.value in res["responseData"]["muted_categories"]


def test_snooze_insight_requires_ownership():
    db = FakeDb()
    # No insight in mi_insights at all -> 404, matching the existing
    # "never confirm existence for another tenant" posture.
    with pytest.raises(HTTPException) as exc_info:
        _run(snooze_insight("i1", ctx=CTX, db=db))
    assert exc_info.value.status_code == 404


def test_snooze_insight_adds_to_preferences():
    db = FakeDb()
    db["mi_insights"].docs.append({"id": "i1", "brand_id": "b1"})
    res = _run(snooze_insight("i1", ctx=CTX, db=db))
    assert "i1" in res["responseData"]["snoozed_insight_ids"]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))

"""
Publish confirmation: a post Outstand accepted is "confirming" until the
network's answer comes back — published, failed (with the reason in plain
words), or, with no answer at all after 24 hours, failed as unconfirmed.
Never failed just because the answer is slow; a late success always wins.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from app.agents.social_media_manager.services import publish_confirmation as pc
from app.services import brand_notifier as bn

# Outstand's own words for Vchain's failed posts (production, October 2026).
VCHAIN_ERROR = ("Error publishing post to Facebook: Failed to upload Facebook photo: 400 - Error validating access "
                "token: The session has been invalidated because the user changed their password or Facebook has "
                "changed the session for security reasons.")


def _match(doc, q):
    for k, v in q.items():
        if k == "$or":
            if not any(_match(doc, sub) for sub in v):
                return False
        elif isinstance(v, dict) and any(op.startswith("$") for op in v):
            if "$exists" in v and (k in doc) != v["$exists"]:
                return False
            if "$ne" in v and doc.get(k) == v["$ne"]:
                return False
        elif doc.get(k) != v:
            return False
    return True


class _Res:
    def __init__(self, n):
        self.matched_count = self.modified_count = n


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=None):
        return list(self.docs)


class _Coll:
    def __init__(self, docs=None):
        self.docs = docs or []

    def find(self, q=None, *a, **k):
        return _Cursor([d for d in self.docs if _match(d, q or {})])

    async def find_one(self, q, *a, **k):
        return next((d for d in self.docs if _match(d, q)), None)

    async def _update(self, q, upd, many):
        n = 0
        for d in self.docs:
            if _match(d, q):
                d.update(upd.get("$set", {}))
                for f in upd.get("$unset", {}):
                    d.pop(f, None)
                n += 1
                if not many:
                    break
        return _Res(n)

    async def update_one(self, q, upd, **k):
        return await self._update(q, upd, False)

    async def update_many(self, q, upd, **k):
        return await self._update(q, upd, True)

    async def find_one_and_update(self, q, upd, return_document=None, **k):
        d = next((d for d in self.docs if _match(d, q)), None)
        if d is None:
            return None
        before = dict(d)
        d.update(upd.get("$set", {}))
        for f, n in upd.get("$inc", {}).items():
            d[f] = d.get(f, 0) + n
        return dict(d) if return_document else before


class _DB(dict):
    def __missing__(self, name):
        self[name] = _Coll()
        return self[name]


class _Outstand:
    def __init__(self, answer=None, raises=None):
        self.answer, self.raises = answer, raises

    async def get_post(self, post_id):
        if self.raises:
            raise self.raises
        return self.answer


NOW = datetime(2026, 10, 15, 9, 0)


def _setup(submitted_minutes_ago=2, slow=False):
    db = _DB()
    draft = {"id": "d1", "user_id": "u1", "brand_id": "b1", "platform": "facebook", "status": pc.CONFIRMING,
             "platform_post_id": "gQXux", "outstand_account_id": "acc1", "content": "Jollof Friday",
             "publish_submitted_at": NOW - timedelta(minutes=submitted_minutes_ago)}
    if slow:
        draft["publish_slow"] = True
    db["content_drafts"] = _Coll([draft])
    db["social_connections"] = _Coll([
        {"id": "c1", "user_id": "u1", "brand_id": "b1", "platform": "facebook", "outstand_account_id": "acc1",
         "connection_status": "active"},
        {"id": "c2", "user_id": "u1", "brand_id": "b2", "platform": "facebook", "outstand_account_id": "acc2",
         "connection_status": "active"},
    ])
    return db, draft


@pytest.fixture(autouse=True)
def _quiet_notifications(monkeypatch):
    sent = []

    async def fake_notify(db, draft, published, message=""):
        sent.append((published, message))
    monkeypatch.setattr(pc, "_notify", fake_notify)
    return sent


def _run(coro):
    # A private loop: asyncio.run would clear the shared loop later tests use.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestExplain:
    def test_expired_facebook_login_says_reconnect(self):
        info = pc.explain("facebook", VCHAIN_ERROR)
        assert info["reconnect"]
        assert info["category"] == "known"
        assert "Reconnect Facebook in Connected Accounts" in info["message"]

    def test_other_errors_keep_the_networks_words(self):
        info = pc.explain("linkedin", "Content is a duplicate")
        assert not info["reconnect"]
        assert info["category"] == "unknown"
        assert "LinkedIn" in info["message"] and "duplicate" in info["message"]

    def test_lost_page_admin_rights_gets_its_own_accurate_message_not_the_generic_one(self):
        # Live-reported: MEEQAT TRAVELS AND TOURS LTD's Facebook post failed
        # this way. The generic "reconnect" message would be misleading here
        # (reconnecting with the same now-demoted account hits this exact
        # error again) — this needs its own specific explanation.
        raw = ("Failed to upload Facebook photo: 400 - The user must be an administrator, editor, or moderator "
               "of the page in order to impersonate it.")
        info = pc.explain("facebook", raw)
        assert info["reconnect"] is True
        assert info["category"] == "known"
        assert "no longer an admin, editor or moderator" in info["message"]
        assert "connection has expired" not in info["message"]

    def test_confirm_your_identity_does_not_ask_to_reconnect(self):
        raw = "Failed to upload Facebook photo: 400 - Confirm your identity before you can publish as this Page."
        info = pc.explain("facebook", raw)
        assert info["reconnect"] is False
        assert info["category"] == "content"
        assert "confirm your identity" in info["message"].lower()


class _MotorLikeDB(_DB):
    """A real Motor database raises if it is used as a boolean ("Database objects do not implement
    truth value testing"); a plain dict never does, which is how this went unnoticed."""

    def __bool__(self):
        raise NotImplementedError("Database objects do not implement truth value testing or bool().")


class TestARealDatabaseIsNeverTestedForTruth:
    def test_an_unclassified_failure_still_counts_against_the_connection_and_notifies(self, _quiet_notifications):
        db, draft = _setup()
        db = _MotorLikeDB(db)
        msg = _run(pc.failed_on_submit(db, draft, {"outstand_account_id": "acc1"}, "something nobody has seen before"))
        assert db["social_connections"].docs[0]["consecutive_publish_failures"] == 1
        assert _quiet_notifications == [(False, msg)]  # the owner is told; bookkeeping did not abort first

class TestFailureAlertIsAlwaysOn:
    def test_post_failed_reaches_the_owner_whatever_they_picked(self):
        assert bn.channels_for("post_failed", bn.preferences({})) == ["email"]
        only_weekly = bn.preferences({"notification_events": ["Weekly performance report"], "notification_channel": "Slack"})
        assert bn.channels_for("post_failed", only_weekly) == ["slack"]

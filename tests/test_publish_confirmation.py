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


class TestReadOutcome:
    def test_accepted_but_not_out_yet_is_pending(self):
        assert pc.read_outcome({"post": {"id": "x", "publishedAt": None,
                                         "socialAccounts": [{"status": "pending"}]}})["state"] == "pending"
        assert pc.read_outcome({"data": {"id": "x", "status": "queued"}})["state"] == "pending"
        assert pc.read_outcome({})["state"] == "pending"

    def test_per_account_failure_is_failed_with_the_networks_words(self):
        out = pc.read_outcome({"post": {"publishedAt": None, "socialAccounts": [
            {"status": "failed", "error": VCHAIN_ERROR}]}})
        assert out == {"state": "failed", "error": VCHAIN_ERROR}

    def test_live(self):
        assert pc.read_outcome({"post": {"publishedAt": "2026-10-15T09:00:04Z", "isDraft": False,
                                         "socialAccounts": [{"status": "published"}]}})["state"] == "published"
        assert pc.read_outcome({"post": {"publishedAt": "2026-10-15T09:00:04Z"}})["state"] == "published"

    def test_published_at_with_one_account_still_going_is_not_yet_published(self):
        assert pc.read_outcome({"post": {"publishedAt": "2026-10-15T09:00:04Z", "socialAccounts": [
            {"status": "published"}, {"status": "processing"}]}})["state"] == "pending"


class TestExplain:
    def test_expired_facebook_login_says_reconnect(self):
        info = pc.explain("facebook", VCHAIN_ERROR)
        assert info["reconnect"]
        assert "Reconnect Facebook in Connected Accounts" in info["message"]

    def test_other_errors_keep_the_networks_words(self):
        info = pc.explain("linkedin", "Content is a duplicate")
        assert not info["reconnect"]
        assert "LinkedIn" in info["message"] and "duplicate" in info["message"]


class TestConfirm:
    def test_vchain_case_marks_failed_flags_reconnect_and_tells_the_owner(self, _quiet_notifications):
        db, draft = _setup()
        answer = {"post": {"publishedAt": None, "socialAccounts": [{"status": "failed", "error": VCHAIN_ERROR}]}}
        assert _run(pc.confirm(db, draft, outstand=_Outstand(answer), now=NOW)) == "failed"
        saved = db["content_drafts"].docs[0]
        assert saved["status"] == "publish_failed"
        assert "Reconnect Facebook" in saved["error_message"]
        assert saved["network_error"] == VCHAIN_ERROR
        conns = {c["id"]: c for c in db["social_connections"].docs}
        assert conns["c1"].get("needs_reconnect") is True
        assert not conns["c2"].get("needs_reconnect")          # the other brand's page is untouched
        assert _quiet_notifications == [(False, saved["error_message"])]

    def test_live_marks_published_and_clears_a_reconnect_flag(self, _quiet_notifications):
        db, draft = _setup()
        db["social_connections"].docs[0]["needs_reconnect"] = True
        answer = {"post": {"publishedAt": "2026-10-15T09:01:00Z", "socialAccounts": [{"status": "published"}]}}
        assert _run(pc.confirm(db, draft, outstand=_Outstand(answer), now=NOW)) == "published"
        assert db["content_drafts"].docs[0]["status"] == "published"
        assert "needs_reconnect" not in db["social_connections"].docs[0]
        assert _quiet_notifications == [(True, "")]

    def test_still_going_is_never_called_failed(self, _quiet_notifications):
        db, draft = _setup(submitted_minutes_ago=5)
        pending = {"post": {"publishedAt": None, "socialAccounts": [{"status": "processing"}]}}
        assert _run(pc.confirm(db, draft, outstand=_Outstand(pending), now=NOW)) == "pending"
        assert db["content_drafts"].docs[0]["status"] == pc.CONFIRMING
        assert "publish_slow" not in db["content_drafts"].docs[0]
        assert _quiet_notifications == []

    def test_after_30_minutes_it_shows_as_taking_longer(self):
        db, draft = _setup(submitted_minutes_ago=31)
        pending = {"post": {"publishedAt": None}}
        assert _run(pc.confirm(db, draft, outstand=_Outstand(pending), now=NOW)) == "pending"
        assert db["content_drafts"].docs[0]["publish_slow"] is True
        assert db["content_drafts"].docs[0]["status"] == pc.CONFIRMING

    def test_late_success_after_slow_still_wins(self):
        db, draft = _setup(submitted_minutes_ago=300, slow=True)
        live = {"post": {"publishedAt": "2026-10-15T08:59:00Z"}}
        assert _run(pc.confirm(db, draft, outstand=_Outstand(live), now=NOW)) == "published"
        saved = db["content_drafts"].docs[0]
        assert saved["status"] == "published" and "publish_slow" not in saved

    def test_no_answer_after_24_hours_is_failed_honestly(self, _quiet_notifications):
        db, draft = _setup(submitted_minutes_ago=24 * 60 + 1)
        assert _run(pc.confirm(db, draft, outstand=_Outstand({"post": {}}), now=NOW)) == "failed"
        saved = db["content_drafts"].docs[0]
        assert saved["status"] == "publish_failed"
        assert "couldn't confirm" in saved["error_message"]
        assert not db["social_connections"].docs[0].get("needs_reconnect")

    def test_outstand_down_just_waits(self, _quiet_notifications):
        db, draft = _setup()
        out = _Outstand(raises=RuntimeError("connection reset"))
        assert _run(pc.confirm(db, draft, outstand=out, now=NOW)) == "pending"
        assert db["content_drafts"].docs[0]["status"] == pc.CONFIRMING
        assert _quiet_notifications == []

    def test_post_gone_from_outstand_is_no_longer_called_published(self):
        db, draft = _setup()
        out = _Outstand(raises=RuntimeError("Client error '404 Not Found'"))
        assert _run(pc.confirm(db, draft, outstand=out, now=NOW)) == "failed"
        saved = db["content_drafts"].docs[0]
        assert saved["status"] == "publish_failed" and "Check your page" in saved["error_message"]

    def test_only_a_draft_still_confirming_is_changed(self):
        """If the owner already republished or the post was settled elsewhere,
        a stale answer must not overwrite it."""
        db, draft = _setup()
        db["content_drafts"].docs[0]["status"] = "published"
        failed = {"post": {"socialAccounts": [{"status": "failed", "error": "x"}]}}
        _run(pc.confirm(db, draft, outstand=_Outstand(failed), now=NOW))
        assert db["content_drafts"].docs[0]["status"] == "published"


class TestSubmitHelpers:
    def test_accepted_outstand_post_needs_confirming(self):
        assert pc.needs_confirmation({"success": True, "via": "outstand", "confirmed": False})
        assert not pc.needs_confirmation({"success": True, "via": "outstand", "confirmed": True})   # TikTok's own poll
        assert not pc.needs_confirmation({"success": True, "post_id": "123_456"})                    # direct API, already live

    def test_awaiting_fields(self):
        f = pc.awaiting_fields({"post_id": "gQXux", "outstand_status": "queued", "outstand_account_id": "acc1"}, now=NOW)
        assert f["status"] == pc.CONFIRMING and f["platform_post_id"] == "gQXux"
        assert f["publish_submitted_at"] == NOW and f["published_date"] is None

    def test_refused_on_submit_flags_reconnect(self, _quiet_notifications):
        db, draft = _setup()
        msg = _run(pc.failed_on_submit(db, draft, {"outstand_account_id": "acc1"}, f"Outstand publish failed: {VCHAIN_ERROR}"))
        assert "Reconnect Facebook" in msg
        assert db["social_connections"].docs[0]["needs_reconnect"] is True
        assert _quiet_notifications == [(False, msg)]


class TestFailureAlertIsAlwaysOn:
    def test_post_failed_reaches_the_owner_whatever_they_picked(self):
        assert bn.channels_for("post_failed", bn.preferences({})) == ["email"]
        only_weekly = bn.preferences({"notification_events": ["Weekly performance report"], "notification_channel": "Slack"})
        assert bn.channels_for("post_failed", only_weekly) == ["slack"]

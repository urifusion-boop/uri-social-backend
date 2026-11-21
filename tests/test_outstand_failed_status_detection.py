"""
publish_scheduled_content's Outstand-poll branch must detect a real platform
failure, not just "not published yet".

Live-reported: a post scheduled for 16:24 UTC was still showing "Publishing
soon..." well after its time passed. Traced via Outstand's own get_post API:
Facebook had rejected it outright (socialAccounts[0].status == "failed",
with a real, specific error — "Confirm your identity before you can publish
as this Page"). Our own polling code only ever checked post.publishedAt,
never each socialAccounts[] entry's own status, so a genuine platform-side
failure was silently indistinguishable from "hasn't published yet" — the
draft stayed status="scheduled" forever, with the real, actionable error
from Outstand/Facebook never reaching the user.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.social_media_manager.services.approval_workflow_service import (
    ApprovalWorkflowService,
    _friendlier_facebook_error,
    _should_flag_connection,
    _record_publish_success,
    UNKNOWN_FAILURE_ESCALATION_THRESHOLD,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeDb:
    """An in-memory MongoDB (mongomock) behind the async calls the publisher makes.
    The hand-written fake this replaces understood only part of MongoDB's query and
    update language; the publisher's queries grew past it ($or with $lte/$exists,
    $unset in find_one_and_update), and the fake silently matched or updated the
    wrong drafts. `db[name].docs` is the collection's current contents."""

    def __init__(self, collections: dict[str, list[dict]] | None = None):
        import mongomock
        self._db = mongomock.MongoClient()["publisher_test"]
        self._colls: dict = {}
        for name, docs in (collections or {}).items():
            if docs:
                self._db[name].insert_many([dict(d) for d in docs])

    def __getitem__(self, name):
        if name not in self._colls:
            self._colls[name] = _AsyncCollection(self._db[name])
        return self._colls[name]


class _AsyncCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def sort(self, key, direction=1):
        self._cursor = self._cursor.sort(key, direction)
        return self

    def limit(self, n):
        self._cursor = self._cursor.limit(n)
        return self

    async def to_list(self, length=None):
        docs = list(self._cursor)
        return docs[:length] if length is not None else docs


class _AsyncCollection:
    def __init__(self, coll):
        self._c = coll

    @property
    def docs(self) -> list[dict]:
        return list(self._c.find({}))

    async def find_one(self, query=None, projection=None, sort=None, **kw):
        return self._c.find_one(query or {}, projection, sort=sort)

    def find(self, query=None, projection=None, **kw):
        return _AsyncCursor(self._c.find(query or {}, projection))

    async def find_one_and_update(self, query, update, projection=None, return_document=None, upsert=False, **kw):
        from pymongo import ReturnDocument
        return self._c.find_one_and_update(query, update, projection, upsert=upsert,
                                           return_document=return_document or ReturnDocument.BEFORE)

    async def update_one(self, query, update, upsert=False, **kw):
        return self._c.update_one(query, update, upsert=upsert)

    async def update_many(self, query, update, upsert=False, **kw):
        return self._c.update_many(query, update, upsert=upsert)

    async def insert_one(self, doc, **kw):
        return self._c.insert_one(doc)

    async def count_documents(self, query, **kw):
        return self._c.count_documents(query)


def _outstand_scheduled_draft():
    now = datetime.utcnow()
    return {
        "id": "draft-A", "user_id": "user-1", "platform": "facebook",
        "post_type": "feed", "content": "Mondays can set the tone...", "status": "scheduled",
        "scheduled_date": now - timedelta(minutes=30), "created_at": now - timedelta(hours=1),
        "platform_post_id": "Npl7E",
    }


def _outstand_response_with_failed_account(error="Confirm your identity before you can publish as this Page."):
    return {
        "success": True,
        "post": {
            "id": "Npl7E",
            "publishedAt": None,
            "isDraft": False,
            "socialAccounts": [
                {"id": "LdZGV", "network": "facebook", "status": "failed", "error": f"Error publishing post to Facebook: {error}"},
            ],
        },
    }


def test_a_platform_side_failure_is_detected_and_surfaced():
    db = FakeDb({"content_drafts": [_outstand_scheduled_draft()]})

    get_post = AsyncMock(return_value=_outstand_response_with_failed_account())
    with patch(
        "app.agents.social_media_manager.services.outstand_service.OutstandService.get_post", new=get_post,
    ):
        result = _run(ApprovalWorkflowService.publish_scheduled_content(db))
        draft = db["content_drafts"].docs[0]
        assert draft["status"] == "publish_failed"
        assert "Confirm your identity" in draft["error_message"]
        assert result["published_count"] == 0
        # Dead submission: the next run neither polls this failed post again nor
        # re-publishes it (it is no longer "scheduled"; the post id stays as the
        # record of what was sent).
        polls = get_post.await_count
        _run(ApprovalWorkflowService.publish_scheduled_content(db))
        assert get_post.await_count == polls
        assert db["content_drafts"].docs[0]["status"] == "publish_failed"


def test_a_post_with_no_failed_accounts_yet_is_left_alone_as_pending():
    """Not every unpublished post is a failure — most are just not there yet.
    Must not be marked failed prematurely."""
    db = FakeDb({"content_drafts": [_outstand_scheduled_draft()]})

    pending_response = {
        "success": True,
        "post": {
            "id": "Npl7E", "publishedAt": None, "isDraft": False,
            "socialAccounts": [{"id": "LdZGV", "network": "facebook", "status": "queued"}],
        },
    }

    with patch(
        "app.agents.social_media_manager.services.outstand_service.OutstandService.get_post",
        new=AsyncMock(return_value=pending_response),
    ):
        _run(ApprovalWorkflowService.publish_scheduled_content(db))

    draft = db["content_drafts"].docs[0]
    # Due, but the network hasn't answered yet: handed to the confirmation step
    # (publish_confirmation.py), which waits for the answer. Not failed, not
    # re-published.
    assert draft["status"] == "confirming"
    assert draft.get("error_message") is None


def test_a_successfully_published_post_is_still_marked_published_not_failed():
    """Regression guard: the new failed-account check must not interfere
    with the existing, already-working success path."""
    db = FakeDb({"content_drafts": [_outstand_scheduled_draft()]})

    published_response = {
        "success": True,
        "post": {
            "id": "Npl7E", "publishedAt": "2026-09-28T16:25:00.000Z", "isDraft": False,
            "socialAccounts": [{"id": "LdZGV", "network": "facebook", "status": "published"}],
        },
    }

    with patch(
        "app.agents.social_media_manager.services.outstand_service.OutstandService.get_post",
        new=AsyncMock(return_value=published_response),
    ):
        result = _run(ApprovalWorkflowService.publish_scheduled_content(db))

    draft = db["content_drafts"].docs[0]
    assert draft["status"] == "published"
    assert result["published_count"] == 1


# ── _friendlier_facebook_error ───────────────────────────────────────────────

def test_friendly_error_explains_the_identity_checkpoint_in_plain_language():
    raw = "Error publishing post to Facebook: Failed to upload Facebook photo: 400 - Confirm your identity before you can publish as this Page. Open the Facebook app on your phone and follow the instructions."
    friendly = _friendlier_facebook_error(raw)
    assert "confirm your identity" in friendly.lower()
    assert "Facebook app on your phone" in friendly
    assert "reconnecting won't fix it" in friendly
    # Raw detail kept for support/debugging, not discarded.
    assert raw in friendly


def test_friendly_error_wraps_unrecognized_errors_in_plain_language():
    # An error with no catalogued pattern still gets a plain-language lead-in
    # immediately, on the very first occurrence — not raw passthrough, and
    # not held back pending any kind of threshold (that threshold only
    # gates whether the *connection* gets flagged for reconnect, a separate
    # decision from what the owner sees on this one post).
    raw = "Some other Outstand error we don't have a translation for"
    friendly = _friendlier_facebook_error(raw)
    assert friendly.startswith("This post couldn't be published to Facebook.")
    assert raw in friendly


def test_friendly_error_handles_missing_error_text():
    assert _friendlier_facebook_error(None) == "This post couldn't be published to Facebook. Facebook's detail: unknown error"
    assert _friendlier_facebook_error("") == "This post couldn't be published to Facebook. Facebook's detail: unknown error"


# ── _should_flag_connection / _record_publish_success ───────────────────────

def test_known_signature_escalates_on_the_very_first_failure():
    db = FakeDb({"social_connections": [{"user_id": "user-1", "platform": "facebook"}]})
    flag, reason = _run(_should_flag_connection(
        db=db, user_id="user-1", platform="facebook", connection=None,
        raw_error="Error validating access token: the session has been invalidated",
    ))
    assert flag is True
    assert "recognized error signature" in reason
    # A known signature never needs the counter at all — it's untouched.
    conn = db["social_connections"].docs[0]
    assert conn.get("consecutive_publish_failures", 0) == 0


def test_unrecognized_error_does_not_escalate_on_a_single_failure():
    db = FakeDb({"social_connections": [{"user_id": "user-1", "platform": "facebook"}]})
    flag, reason = _run(_should_flag_connection(
        db=db, user_id="user-1", platform="facebook", connection=None,
        raw_error="Some brand-new Outstand error nobody has seen before",
    ))
    assert flag is False
    assert db["social_connections"].docs[0]["consecutive_publish_failures"] == 1


def test_unrecognized_error_escalates_after_the_threshold_is_reached():
    db = FakeDb({"social_connections": [{"user_id": "user-1", "platform": "facebook"}]})
    results = []
    for _ in range(UNKNOWN_FAILURE_ESCALATION_THRESHOLD):
        results.append(_run(_should_flag_connection(
            db=db, user_id="user-1", platform="facebook", connection=None,
            raw_error="Some brand-new Outstand error nobody has seen before",
        )))
    # Every failure before the threshold does NOT escalate; only hitting the
    # threshold does — a single one-off failure must never trigger a
    # "reconnect your account" alert on its own.
    assert [flag for flag, _ in results[:-1]] == [False] * (UNKNOWN_FAILURE_ESCALATION_THRESHOLD - 1)
    assert results[-1][0] is True
    assert "consecutive unrecognized" in results[-1][1]


def test_a_success_in_between_resets_the_unrecognized_failure_count():
    db = FakeDb({"social_connections": [{"user_id": "user-1", "platform": "facebook"}]})
    _run(_should_flag_connection(db=db, user_id="user-1", platform="facebook", connection=None, raw_error="unknown issue"))
    _run(_should_flag_connection(db=db, user_id="user-1", platform="facebook", connection=None, raw_error="unknown issue"))
    assert db["social_connections"].docs[0]["consecutive_publish_failures"] == 2
    _run(_record_publish_success(db=db, user_id="user-1", platform="facebook", connection=None))
    assert db["social_connections"].docs[0]["consecutive_publish_failures"] == 0
    # Counting starts fresh after the reset, not from where it left off.
    flag, _ = _run(_should_flag_connection(db=db, user_id="user-1", platform="facebook", connection=None, raw_error="unknown issue"))
    assert flag is False
    assert db["social_connections"].docs[0]["consecutive_publish_failures"] == 1


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))

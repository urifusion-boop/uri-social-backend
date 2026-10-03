"""
reconcile_published_posts closes the gap publish_scheduled_content's Sept-28
fix left open: that fix taught the SCHEDULED-post cron to check each
socialAccounts[] entry's real status instead of trusting Outstand's initial
"accepted" response — but a draft published via "Publish Now" is marked
status="published" the instant Outstand accepts the submission
(outstand_post_status="queued"/"processing"), and nothing ever revisited it
afterward to see whether the platform actually accepted it.

Live-reported: a "Vchain" Facebook post (post_id=MADzP) was marked published
in our DB; CloudWatch logs confirmed it had actually failed on Facebook's
side with "Error validating access token: The session has been invalidated"
— discovered only because someone happened to open that post's analytics.
The connection was never marked disconnected, so every subsequent publish to
that Page kept failing silently the same way.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.social_media_manager.services.approval_workflow_service import (
    ApprovalWorkflowService,
    _friendlier_facebook_error,
    _is_token_invalidation_error,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCursor:
    def __init__(self, docs: list[dict]):
        self._docs = docs

    async def to_list(self, length=None):
        return [dict(d) for d in (self._docs[:length] if length is not None else self._docs)]


class FakeUpdateResult:
    def __init__(self, modified_count=0, matched_count=0):
        self.modified_count = modified_count
        self.matched_count = matched_count


class FakeCollection:
    def __init__(self, docs=None):
        self.docs: list[dict] = docs or []

    def _field_matches(self, d, k, v):
        val = d.get(k)
        if isinstance(v, dict):
            if "$gte" in v:
                return val is not None and val >= v["$gte"]
            if "$lte" in v:
                return val is not None and val <= v["$lte"]
            if "$ne" in v:
                return val != v["$ne"]
            if "$exists" in v:
                return (k in d) == v["$exists"]
            if "$in" in v:
                return val in v["$in"]
        return val == v

    def _match(self, query):
        return [d for d in self.docs if all(self._field_matches(d, k, v) for k, v in query.items())]

    def find(self, query):
        return FakeCursor(self._match(query))

    async def update_one(self, query, update, upsert=False):
        matches = self._match(query)
        for d in matches:
            d.update(update.get("$set", {}))
            return FakeUpdateResult(modified_count=1, matched_count=1)
        return FakeUpdateResult(modified_count=0, matched_count=0)


class FakeDb:
    def __init__(self, collections: dict[str, list[dict]] | None = None):
        self._colls = {name: FakeCollection(docs) for name, docs in (collections or {}).items()}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


def _published_draft(**overrides):
    now = datetime.utcnow()
    d = {
        "id": "draft-MADzP", "user_id": "user-vchain", "platform": "facebook",
        "status": "published", "outstand_post_status": "queued",
        "platform_post_id": "MADzP", "published_date": now - timedelta(hours=2),
    }
    d.update(overrides)
    return d


def _social_connection(**overrides):
    c = {"user_id": "user-vchain", "platform": "facebook", "outstand_account_id": "86753", "connection_status": "active"}
    c.update(overrides)
    return c


# ── reconcile_published_posts ────────────────────────────────────────────────

def test_a_silently_failed_published_post_is_corrected_to_publish_failed():
    db = FakeDb({"content_drafts": [_published_draft()]})
    failed_response = {
        "post": {
            "publishedAt": None,
            "socialAccounts": [{"network": "facebook", "status": "failed",
                                 "error": "Error publishing post to Facebook: Error validating access token: The session has been invalidated because the user changed their password."}],
        }
    }
    with patch(
        "app.agents.social_media_manager.services.outstand_service.OutstandService.get_post",
        new=AsyncMock(return_value=failed_response),
    ):
        result = _run(ApprovalWorkflowService.reconcile_published_posts(db))

    draft = db["content_drafts"].docs[0]
    assert draft["status"] == "publish_failed"
    assert "reconnect" in draft["error_message"].lower()
    assert result == {"checked": 1, "corrected": 1}


def test_token_invalidation_failure_marks_the_connection_disconnected_and_notifies():
    db = FakeDb({
        "content_drafts": [_published_draft()],
        "social_connections": [_social_connection()],
    })
    failed_response = {
        "post": {
            "publishedAt": None,
            "socialAccounts": [{"network": "facebook", "status": "failed",
                                 "error": "Error validating access token: The session has been invalidated."}],
        }
    }
    with patch(
        "app.agents.social_media_manager.services.outstand_service.OutstandService.get_post",
        new=AsyncMock(return_value=failed_response),
    ), patch(
        "app.services.NotificationService.notification_service.notify_connection_disconnected",
        new=AsyncMock(),
    ) as mock_notify:
        _run(ApprovalWorkflowService.reconcile_published_posts(db))

    conn = db["social_connections"].docs[0]
    assert conn["connection_status"] == "disconnected"
    mock_notify.assert_awaited_once_with(user_id="user-vchain", platform="facebook")


def test_a_non_token_failure_does_not_touch_the_connection():
    """A content/policy rejection (e.g. the identity-checkpoint case) is not a
    dead connection — must not be disconnected over it."""
    db = FakeDb({
        "content_drafts": [_published_draft()],
        "social_connections": [_social_connection()],
    })
    failed_response = {
        "post": {
            "publishedAt": None,
            "socialAccounts": [{"network": "facebook", "status": "failed",
                                 "error": "Confirm your identity before you can publish as this Page."}],
        }
    }
    with patch(
        "app.agents.social_media_manager.services.outstand_service.OutstandService.get_post",
        new=AsyncMock(return_value=failed_response),
    ):
        _run(ApprovalWorkflowService.reconcile_published_posts(db))

    conn = db["social_connections"].docs[0]
    assert conn["connection_status"] == "active"


def test_a_still_pending_post_is_left_alone():
    db = FakeDb({"content_drafts": [_published_draft()]})
    pending_response = {"post": {"publishedAt": None, "socialAccounts": [{"network": "facebook", "status": "queued"}]}}
    with patch(
        "app.agents.social_media_manager.services.outstand_service.OutstandService.get_post",
        new=AsyncMock(return_value=pending_response),
    ):
        result = _run(ApprovalWorkflowService.reconcile_published_posts(db))

    draft = db["content_drafts"].docs[0]
    assert draft["status"] == "published"
    assert draft["outstand_post_status"] == "queued"
    assert result == {"checked": 1, "corrected": 0}


def test_a_confirmed_published_post_is_marked_so_it_stops_being_rechecked():
    db = FakeDb({"content_drafts": [_published_draft()]})
    confirmed_response = {"post": {"publishedAt": "2026-10-02T08:10:05.000Z", "socialAccounts": [{"network": "facebook", "status": "published"}]}}
    with patch(
        "app.agents.social_media_manager.services.outstand_service.OutstandService.get_post",
        new=AsyncMock(return_value=confirmed_response),
    ):
        _run(ApprovalWorkflowService.reconcile_published_posts(db))

    draft = db["content_drafts"].docs[0]
    assert draft["status"] == "published"
    assert draft["outstand_post_status"] == "published"


def test_posts_outside_the_7_day_window_are_not_candidates():
    old_draft = _published_draft(id="draft-old", published_date=datetime.utcnow() - timedelta(days=10))
    db = FakeDb({"content_drafts": [old_draft]})
    with patch(
        "app.agents.social_media_manager.services.outstand_service.OutstandService.get_post",
        new=AsyncMock(),
    ) as mock_get_post:
        result = _run(ApprovalWorkflowService.reconcile_published_posts(db))

    mock_get_post.assert_not_called()
    assert result == {"checked": 0, "corrected": 0}


def test_already_confirmed_drafts_are_not_candidates():
    """outstand_post_status="published" (or anything other than queued/processing)
    must drop out of the query on its own — no infinite re-polling."""
    confirmed = _published_draft(outstand_post_status="published")
    db = FakeDb({"content_drafts": [confirmed]})
    with patch(
        "app.agents.social_media_manager.services.outstand_service.OutstandService.get_post",
        new=AsyncMock(),
    ) as mock_get_post:
        result = _run(ApprovalWorkflowService.reconcile_published_posts(db))

    mock_get_post.assert_not_called()
    assert result == {"checked": 0, "corrected": 0}


# ── _is_token_invalidation_error / _friendlier_facebook_error ───────────────

def test_token_invalidation_patterns_are_recognized():
    assert _is_token_invalidation_error("Error validating access token: The session has been invalidated because the user changed their password.")
    assert _is_token_invalidation_error("The session is invalid")
    assert not _is_token_invalidation_error("Confirm your identity before you can publish as this Page.")
    assert not _is_token_invalidation_error(None)


def test_friendly_error_for_token_invalidation_tells_the_user_to_reconnect():
    raw = "Error publishing post to Facebook: Failed to upload Facebook photo: 400 - Error validating access token: The session has been invalidated because the user changed their password."
    friendly = _friendlier_facebook_error(raw)
    assert "reconnect" in friendly.lower()
    assert raw in friendly


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))

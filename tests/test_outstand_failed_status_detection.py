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

from app.agents.social_media_manager.services.approval_workflow_service import ApprovalWorkflowService


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCursor:
    def __init__(self, docs: list[dict]):
        self._docs = docs

    def sort(self, key, direction=1):
        self._docs = sorted(self._docs, key=lambda d: d.get(key), reverse=(direction == -1))
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return [dict(d) for d in (self._docs[:length] if length is not None else self._docs)]


class FakeUpdateResult:
    def __init__(self, modified_count=0, matched_count=0):
        self.modified_count = modified_count
        self.matched_count = matched_count


class FakeCollection:
    def __init__(self, docs=None):
        self.docs: list[dict] = docs or []

    def _match(self, query):
        if "$or" in query:
            return [d for d in self.docs if any(
                all(self._field_matches(d, k, v) for k, v in clause.items())
                for clause in query["$or"]
            )]
        return [d for d in self.docs if all(self._field_matches(d, k, v) for k, v in query.items())]

    def _field_matches(self, d, k, v):
        val = d.get(k)
        if isinstance(v, dict):
            if "$lte" in v:
                return val is not None and val <= v["$lte"]
            if "$ne" in v:
                return val != v["$ne"]
            if "$exists" in v:
                return (k in d) == v["$exists"]
            if "$in" in v:
                return val in v["$in"]
        return val == v

    async def find_one(self, query, projection=None, sort=None):
        matches = self._match(query)
        if sort:
            key, direction = sort[0]
            matches.sort(key=lambda d: d.get(key), reverse=(direction == -1))
        return dict(matches[0]) if matches else None

    def find(self, query):
        return FakeCursor(self._match(query))

    async def find_one_and_update(self, query, update, return_document=None):
        matches = self._match(query)
        if not matches:
            return None
        d = matches[0]
        before = dict(d)
        d.update(update.get("$set", {}))
        return before

    async def update_one(self, query, update, upsert=False):
        matches = self._match(query)
        for d in matches:
            d.update(update.get("$set", {}))
            for k in update.get("$unset", {}):
                d.pop(k, None)
            return FakeUpdateResult(modified_count=1, matched_count=1)
        return FakeUpdateResult(modified_count=0, matched_count=0)

    async def update_many(self, query, update):
        matches = self._match(query)
        for d in matches:
            d.update(update.get("$set", {}))
        return FakeUpdateResult(modified_count=len(matches), matched_count=len(matches))


class FakeDb:
    def __init__(self, collections: dict[str, list[dict]] | None = None):
        self._colls = {name: FakeCollection(docs) for name, docs in (collections or {}).items()}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


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

    with patch(
        "app.agents.social_media_manager.services.outstand_service.OutstandService.get_post",
        new=AsyncMock(return_value=_outstand_response_with_failed_account()),
    ):
        result = _run(ApprovalWorkflowService.publish_scheduled_content(db))

    draft = db["content_drafts"].docs[0]
    assert draft["status"] == "publish_failed"
    assert "Confirm your identity" in draft["error_message"]
    # Dead submission — must not keep polling the same failed Outstand post.
    assert draft["platform_post_id"] is None
    assert result["published_count"] == 0


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
    assert draft["status"] == "scheduled"  # untouched — genuinely still pending
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


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))

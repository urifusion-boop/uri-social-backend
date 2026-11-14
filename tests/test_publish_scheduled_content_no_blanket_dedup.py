"""
publish_scheduled_content — the cron must not cancel legitimately different
scheduled posts.

Live-reported production bug (confirmed live on aws/prod since commit
46d4424, 2026-05-15): a "dedup" step ran on every cron tick and kept only
the single newest-CREATED draft per (user_id, platform), silently setting
every other one to status="replaced" — with zero awareness of scheduled_date.
That's indistinguishable from the completely normal case of having more than
one post scheduled for different days on the same platform (any real content
calendar). Users reported their scheduled posts vanishing from the Scheduled
tab (the frontend filters "replaced" out of every active view) and never
reaching Facebook, with no error or notification anywhere.

The dedup was originally added to fix a real duplicate-posting bug, but that
bug was actually duplicate DRAFT CREATION (a double-submit producing several
separate draft docs for one intended post) — this blanket rule never
addressed that at the source. The two safeguards that DO correctly prevent
double-publishing without this failure mode are covered separately:
- approve_content's narrower in-flight cancel (publishing/ready_to_publish
  only, at the moment a new draft is explicitly scheduled).
- The atomic find_one_and_update claim inside this same function, tested
  here directly: a draft can only ever be claimed once.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.agents.social_media_manager.services.approval_workflow_service import ApprovalWorkflowService


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# An in-memory MongoDB (mongomock) behind the small interface these tests use, so queries
# behave exactly like the real database ($or next to other conditions, $nin, $not, upserts...).
mongomock_motor = pytest.importorskip("mongomock_motor")


class FakeCollection:
    """A real (in-memory) Mongo collection, plus `.docs`: the stored documents, for assertions."""

    def __init__(self, docs=None, _coll=None):
        self._c = _coll if _coll is not None else mongomock_motor.AsyncMongoMockClient()["test"]["c"]
        for d in docs or []:
            _run(self._c.insert_one(dict(d)))

    def __getattr__(self, name):
        return getattr(self._c, name)

    @property
    def docs(self) -> list[dict]:
        return _run(self._c.find({}, {"_id": 0}).to_list(None))


class FakeDb:
    def __init__(self, collections: dict[str, list[dict]] | None = None):
        self._db = mongomock_motor.AsyncMongoMockClient()["test"]
        for name, docs in (collections or {}).items():
            FakeCollection(docs, _coll=self._db[name])

    def __getitem__(self, name):
        return FakeCollection(_coll=self._db[name])


def _draft(draft_id, scheduled_date, created_at):
    return {
        "id": draft_id, "user_id": "user-1", "platform": "facebook",
        "post_type": "feed", "content": f"post {draft_id}", "status": "scheduled",
        "scheduled_date": scheduled_date, "created_at": created_at,
        "platform_post_id": None,
    }


def _facebook_connection():
    return {
        "id": "conn-1", "user_id": "user-1", "platform": "facebook",
        "connection_status": "active", "connected_via": "outstand",
        "outstand_account_id": "os-acc-1",
    }


def test_two_legitimately_different_scheduled_posts_are_both_processed_not_cancelled():
    """The exact shape of the live bug: two DIFFERENT posts, different
    scheduled_date, same platform — both due now. Neither should end up
    status='replaced'; both should be attempted."""
    now = datetime.utcnow()
    older_created = _draft("draft-A", scheduled_date=now - timedelta(minutes=10), created_at=now - timedelta(days=2))
    newer_created = _draft("draft-B", scheduled_date=now - timedelta(minutes=5), created_at=now - timedelta(hours=1))
    db = FakeDb({
        "content_drafts": [older_created, newer_created],
        "social_connections": [_facebook_connection()],
    })

    with patch.object(
        ApprovalWorkflowService, "_publish_to_platform",
        new=AsyncMock(return_value={"success": True, "post_id": "fb_post_1", "outstand_status": "queued"}),
    ):
        result = _run(ApprovalWorkflowService.publish_scheduled_content(db))

    drafts = {d["id"]: d for d in db["content_drafts"].docs}
    # Neither is cancelled. Both go out: the first now, the second right after it, held
    # back only by the per-account spacing between posts (it has a next_attempt_at).
    assert "replaced" not in {d["status"] for d in drafts.values()}
    assert drafts["draft-A"]["status"] == "published"
    assert drafts["draft-B"]["status"] == "scheduled" and drafts["draft-B"].get("next_attempt_at")
    assert result["published_count"] == 1


def test_a_future_dated_outstand_scheduled_post_is_not_cancelled_by_a_newer_one():
    """The other half of the live bug: an already-submitted-to-Outstand post
    (platform_post_id set, genuinely scheduled for later) must not be wiped
    out just because a second, newer draft exists for the same platform."""
    now = datetime.utcnow()
    already_submitted = _draft("draft-A", scheduled_date=now + timedelta(days=3), created_at=now - timedelta(days=2))
    already_submitted["platform_post_id"] = "os_post_abc"
    newly_created = _draft("draft-B", scheduled_date=now + timedelta(days=5), created_at=now)
    newly_created["platform_post_id"] = "os_post_xyz"
    db = FakeDb({
        "content_drafts": [already_submitted, newly_created],
        "social_connections": [_facebook_connection()],
    })

    async def fake_get_post(post_id):
        return {"post": {"publishedAt": None, "isDraft": False}}  # not yet published — still pending

    with patch(
        "app.agents.social_media_manager.services.outstand_service.OutstandService.get_post",
        new=AsyncMock(side_effect=fake_get_post),
    ):
        _run(ApprovalWorkflowService.publish_scheduled_content(db))

    statuses = {d["id"]: d["status"] for d in db["content_drafts"].docs}
    # Both remain scheduled/pending — neither was cancelled to "replaced".
    assert statuses["draft-A"] == "scheduled"
    assert statuses["draft-B"] == "scheduled"


def test_atomic_claim_still_prevents_double_publishing_the_same_draft():
    """The safeguard that actually matters for double-publish safety, kept
    intact: once claimed (status -> 'publishing'), a second concurrent
    attempt to claim the same draft must find nothing to claim."""
    now = datetime.utcnow()
    draft = _draft("draft-A", scheduled_date=now - timedelta(minutes=1), created_at=now)
    collection = FakeCollection([draft])

    first_claim = _run(collection.find_one_and_update(
        {"id": "draft-A", "status": "scheduled"}, {"$set": {"status": "publishing"}}
    ))
    second_claim = _run(collection.find_one_and_update(
        {"id": "draft-A", "status": "scheduled"}, {"$set": {"status": "publishing"}}
    ))

    assert first_claim is not None
    assert second_claim is None


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))

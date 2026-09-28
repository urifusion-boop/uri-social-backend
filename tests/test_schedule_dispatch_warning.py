"""
Facebook "connection is expired" surprise at scheduled-post time.

Live-reported bug: Connected Accounts kept showing Facebook as connected,
but a scheduled post later failed with a connection-expired error the user
had no warning about. Root cause, traced end to end:

1. Scheduling a Facebook feed post doesn't just save it for later — the
   backend immediately submits it to Outstand right at schedule time
   (approve_content's "Outstand native scheduling" block) so Outstand can
   handle the timed release itself.
2. If THAT submission fails (e.g. the underlying Facebook token really is
   dead), the failure used to be completely silent: only a print() to the
   server log, no error persisted anywhere, and the response never
   mentioned it — so the frontend showed a plain "Scheduled!" even though
   nothing was actually confirmed working. The draft still ends up
   status="scheduled" on purpose (the cron gets its own honest retry at the
   real send time rather than giving up on one early hiccup) — but the user
   had zero signal anything was wrong until that retry also failed, framed
   as a surprise, unexplained error days or hours later.

Fix: surface the dispatch failure immediately via a new `warnings[]` list,
kept deliberately separate from `errors[]` — the draft IS genuinely
scheduled (unlike an errors[] entry, which means it was never accepted at
all), so the frontend must not treat this as a hard failure. Also persists
`last_dispatch_warning` onto the draft so it isn't lost even if the toast is
missed, cleared the next time a dispatch attempt actually succeeds.
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


class FakeCollection:
    def __init__(self, docs=None):
        self.docs: list[dict] = docs or []

    async def find_one(self, query, projection=None, sort=None):
        matches = [d for d in self.docs if all(d.get(k) == v for k, v in query.items())]
        if sort:
            key, direction = sort[0]
            matches.sort(key=lambda d: d.get(key), reverse=(direction == -1))
        return dict(matches[0]) if matches else None

    def find(self, query):
        matches = [d for d in self.docs if all(d.get(k) == v for k, v in query.items())]
        return FakeCursor(matches)

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update.get("$set", {}))
                for k in update.get("$unset", {}):
                    d.pop(k, None)
                return
        if upsert:
            new_doc = dict(query)
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)

    async def update_many(self, query, update):
        class _Result:
            modified_count = 0
        result = _Result()
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update.get("$set", {}))
                result.modified_count += 1
        return result


class FakeDb:
    def __init__(self, collections: dict[str, list[dict]] | None = None):
        self._colls = {name: FakeCollection(docs) for name, docs in (collections or {}).items()}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


def _facebook_draft():
    return {
        "id": "draft-1", "user_id": "user-1", "platform": "facebook",
        "post_type": "feed", "content": "hello world", "status": "approved",
    }


def _facebook_outstand_connection():
    return {
        "id": "conn-1", "user_id": "user-1", "platform": "facebook",
        "connection_status": "active", "connected_via": "outstand",
        "outstand_account_id": "os-acc-1",
    }


# ── approve_content: warnings[] surfaced from a failed schedule-time dispatch ──

def test_schedule_dispatch_failure_produces_a_warning_not_an_error():
    db = FakeDb({
        "content_drafts": [_facebook_draft()],
        "social_connections": [_facebook_outstand_connection()],
    })
    failing_result = {"draft-1": {"success": False, "error": "OAuthException: session has expired"}}

    with patch.object(
        ApprovalWorkflowService, "_trigger_immediate_publishing",
        new=AsyncMock(return_value=failing_result),
    ):
        result = _run(ApprovalWorkflowService.approve_content(
            db, "user-1", ["draft-1"], schedule_option="schedule",
            scheduled_datetime=datetime.utcnow() + timedelta(hours=2),
        ))

    data = result["responseData"]
    assert data["errors"] == []
    assert len(data["warnings"]) == 1
    warning = data["warnings"][0]
    assert warning["draft_id"] == "draft-1"
    assert "session has expired" in warning["warning"]
    assert "still queued" in warning["warning"]
    assert "Facebook connection" in warning["warning"]
    # The draft itself is genuinely scheduled — a warning must never look
    # like the post wasn't accepted at all.
    assert data["approved_drafts"][0]["status"] == "scheduled"


def test_schedule_dispatch_success_produces_no_warning():
    db = FakeDb({
        "content_drafts": [_facebook_draft()],
        "social_connections": [_facebook_outstand_connection()],
    })
    ok_result = {"draft-1": {"success": True, "post_id": "fb_post_123", "outstand_status": "queued"}}

    with patch.object(
        ApprovalWorkflowService, "_trigger_immediate_publishing",
        new=AsyncMock(return_value=ok_result),
    ):
        result = _run(ApprovalWorkflowService.approve_content(
            db, "user-1", ["draft-1"], schedule_option="schedule",
            scheduled_datetime=datetime.utcnow() + timedelta(hours=2),
        ))

    data = result["responseData"]
    assert data["errors"] == []
    assert data["warnings"] == []


def test_schedule_dispatch_exception_produces_a_warning_not_a_crash():
    db = FakeDb({
        "content_drafts": [_facebook_draft()],
        "social_connections": [_facebook_outstand_connection()],
    })

    with patch.object(
        ApprovalWorkflowService, "_trigger_immediate_publishing",
        new=AsyncMock(side_effect=RuntimeError("Outstand timed out")),
    ):
        result = _run(ApprovalWorkflowService.approve_content(
            db, "user-1", ["draft-1"], schedule_option="schedule",
            scheduled_datetime=datetime.utcnow() + timedelta(hours=2),
        ))

    data = result["responseData"]
    assert data["errors"] == []
    assert len(data["warnings"]) == 1
    assert "Outstand timed out" in data["warnings"][0]["warning"]


# ── _trigger_immediate_publishing: persists/clears last_dispatch_warning ────

def test_failed_scheduled_dispatch_persists_last_dispatch_warning_and_keeps_status_scheduled():
    db = FakeDb({
        "content_drafts": [_facebook_draft()],
        "social_connections": [_facebook_outstand_connection()],
    })

    with patch.object(
        ApprovalWorkflowService, "_publish_to_platform",
        new=AsyncMock(return_value={"success": False, "error": "token expired"}),
    ):
        _run(ApprovalWorkflowService._trigger_immediate_publishing(
            db, "user-1", ["draft-1"], scheduled_datetime=datetime.utcnow() + timedelta(hours=2),
        ))

    draft = db["content_drafts"].docs[0]
    assert draft["status"] == "scheduled"  # not overwritten to publish_failed — cron still retries
    assert draft["last_dispatch_warning"] == "token expired"


def test_successful_scheduled_dispatch_clears_a_stale_last_dispatch_warning():
    seeded = _facebook_draft()
    seeded["last_dispatch_warning"] = "token expired"  # left over from an earlier failed attempt
    db = FakeDb({
        "content_drafts": [seeded],
        "social_connections": [_facebook_outstand_connection()],
    })

    with patch.object(
        ApprovalWorkflowService, "_publish_to_platform",
        new=AsyncMock(return_value={"success": True, "post_id": "fb_post_123", "outstand_status": "queued"}),
    ):
        _run(ApprovalWorkflowService._trigger_immediate_publishing(
            db, "user-1", ["draft-1"], scheduled_datetime=datetime.utcnow() + timedelta(hours=2),
        ))

    draft = db["content_drafts"].docs[0]
    assert draft["status"] == "scheduled"
    assert "last_dispatch_warning" not in draft


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))

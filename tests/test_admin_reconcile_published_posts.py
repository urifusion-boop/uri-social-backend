"""
POST /api/admin/social-media/reconcile-published-posts — thin admin wrapper
around ApprovalWorkflowService.reconcile_published_posts, added so an admin
can trigger it from the Admin page instead of needing a cron secret nobody
on the team actually has.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from app.routers.admin_router import admin_reconcile_published_posts


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _admin():
    return {"claims": {"email": "admin@urisocial.com"}}


class FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return [dict(d) for d in (self._docs[:length] if length is not None else self._docs)]


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = docs or []

    def find(self, query):
        def matches(d, k, v):
            val = d.get(k)
            if isinstance(v, dict):
                if "$gte" in v:
                    return val is not None and val >= v["$gte"]
                if "$ne" in v:
                    return val != v["$ne"]
                if "$exists" in v:
                    return (k in d) == v["$exists"]
                if "$in" in v:
                    return val in v["$in"]
            return val == v
        return FakeCursor([d for d in self.docs if all(matches(d, k, v) for k, v in query.items())])

    async def update_one(self, query, update, upsert=False):
        return type("Result", (), {"matched_count": 1})()


class FakeDb:
    def __init__(self, collections=None):
        self._colls = {name: FakeCollection(docs) for name, docs in (collections or {}).items()}

    def __getitem__(self, name):
        return self._colls.setdefault(name, FakeCollection())


def test_admin_endpoint_requires_no_cron_secret_and_delegates():
    """The whole point: an admin can call this with nothing but their own
    login — no X-Cron-Secret, no SSM lookup."""
    db = FakeDb({"content_drafts": []})
    result = _run(admin_reconcile_published_posts(admin_user=_admin(), db=db))
    assert result == {"status": True, "checked": 0, "corrected": 0}


def test_admin_endpoint_surfaces_a_real_correction():
    now = datetime.utcnow()
    draft = {
        "id": "draft-MADzP", "user_id": "user-vchain", "platform": "facebook",
        "status": "published", "outstand_post_status": "queued",
        "platform_post_id": "MADzP", "published_date": now - timedelta(hours=1),
    }
    db = FakeDb({"content_drafts": [draft]})
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
    ):
        result = _run(admin_reconcile_published_posts(admin_user=_admin(), db=db))

    assert result["status"] is True
    assert result["checked"] == 1
    assert result["corrected"] == 1

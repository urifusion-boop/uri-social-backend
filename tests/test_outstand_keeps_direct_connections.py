"""Connecting through Outstand must not delete a direct Meta connection for the same
platform: only the direct one carries the Page token the inbox needs, so wiping it
silently stopped DMs and auto-replies."""
import asyncio
from unittest.mock import AsyncMock, patch

from app.agents.social_media_manager.routers.complete_social_manager import finalize_outstand_direct
from app.agents.social_media_manager.services.social_account_service import SocialAccountService


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _matches(doc, query):
    if "$or" in query:
        return any(_matches(doc, q) for q in query["$or"])
    return all(doc.get(k) == v for k, v in query.items())


class FakeCollection:
    def __init__(self, docs):
        self.docs = docs

    async def delete_many(self, query):
        self.docs[:] = [d for d in self.docs if not _matches(d, query)]

    async def delete_one(self, query):
        for d in self.docs:
            if _matches(d, query):
                self.docs.remove(d)
                return

    async def insert_one(self, doc):
        self.docs.append(dict(doc))


class FakeDb:
    def __init__(self, docs):
        self.conns = FakeCollection(docs)
        self.other = FakeCollection([])

    def __getitem__(self, name):
        return self.conns if name == "social_connections" else self.other


def _docs():
    return [
        {"id": "fb_pg1", "user_id": "u1", "platform": "facebook", "connected_via": "facebook_direct_oauth",
         "page_access_token": "tok"},
        {"id": "old_outstand", "user_id": "u1", "platform": "facebook", "connected_via": "outstand"},
    ]


def test_outstand_direct_finalize_replaces_only_outstand_records():
    docs = _docs()
    _run(finalize_outstand_direct("acc1", "facebook", "page", None, db=FakeDb(docs),
                                  ctx={"user_id": "u1", "brand_id": None}))
    ids = {d["id"] for d in docs}
    assert ids == {"fb_pg1", "acc1"}


def test_outstand_session_finalize_replaces_only_outstand_records():
    docs = _docs()
    outstand = AsyncMock()
    outstand.finalize_connection.return_value = {"connectedAccounts": [{"id": "acc1", "network": "facebook"}]}
    with patch("app.agents.social_media_manager.services.social_account_service.OutstandService",
               return_value=outstand):
        _run(SocialAccountService.finalize_connection(FakeDb(docs), "u1", "sess", ["p1"]))
    ids = {d["id"] for d in docs}
    assert ids == {"fb_pg1", "acc1"}


class _UpdatingCollection(FakeCollection):
    async def update_one(self, query, update):
        for d in self.docs:
            if _matches(d, query):
                d.update(update.get("$set", {}))
                for k in update.get("$unset", {}):
                    d.pop(k, None)
                return type("R", (), {"matched_count": 1})()
        return type("R", (), {"matched_count": 0})()


def test_instagram_finalize_activates_the_fresh_record_not_an_older_one():
    from app.agents.social_media_manager.routers.complete_social_manager import instagram_direct_finalize

    docs = [
        {"id": "old_ig", "ig_user_id": "17", "brand_id": "brnd_agency", "connection_status": "active"},
        {"id": "17", "ig_user_id": "17", "user_id": None, "connection_status": "pending_user_match"},
    ]
    db = FakeDb(docs)
    db.conns = _UpdatingCollection(docs)
    _run(instagram_direct_finalize("17", db=db, ctx={"user_id": "u1", "brand_id": None}))
    fresh = next(d for d in docs if d["id"] == "17")
    assert fresh["connection_status"] == "active" and fresh["user_id"] == "u1"
    assert next(d for d in docs if d["id"] == "old_ig")["brand_id"] == "brnd_agency"

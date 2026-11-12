"""
Posting to a Facebook Page connected directly with Meta.

A direct connection that holds the Page and its token posts straight to the Page (Graph API) and
is picked ahead of a newer Outstand connection. The result is a plain pageId_postId with no `via`, so
nothing polls Outstand for it (that polling once produced duplicate posts). FACEBOOK_POST_DIRECT=false
restores the old routing. A draft belonging to one brand never borrows another brand's Page.
"""
import asyncio
from unittest.mock import AsyncMock, patch

from app.agents.social_media_manager.services import publish_confirmation as pc
from app.agents.social_media_manager.services import publish_dispatch as pd
from app.agents.social_media_manager.services.approval_workflow_service import ApprovalWorkflowService
from app.services.FacebookService import FacebookService

OUTSTAND_LIST = "app.agents.social_media_manager.services.outstand_service.OutstandService.list_accounts"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── A small Mongo stand-in that understands the filter used here ──────────────

_MISSING = object()


def _matches(doc, filt):
    for field, cond in filt.items():
        if field == "$or":
            if not any(_matches(doc, f) for f in cond):
                return False
            continue
        value = doc.get(field, _MISSING)
        if isinstance(cond, dict) and any(k.startswith("$") for k in cond):
            for op, operand in cond.items():
                if op == "$exists" and (value is not _MISSING) != bool(operand):
                    return False
                if op == "$ne" and (None if value is _MISSING else value) == operand:
                    return False
        elif (None if value is _MISSING else value) != cond:
            return False
    return True


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self, length=None):
        return list(self.docs)


class Conns:
    def __init__(self, docs):
        self.docs = docs

    async def find_one(self, filt, *a, **k):
        return next((d for d in self.docs if _matches(d, filt)), None)

    def find(self, filt, *a, **k):
        return _Cursor([d for d in self.docs if _matches(d, filt)])


class Db(dict):
    def __init__(self, docs):
        super().__init__(social_connections=Conns(docs))


def direct(page_id="p1", brand_id=None, **extra):
    doc = {"id": f"fb_{page_id}", "user_id": "u1", "platform": "facebook", "connected_via": "facebook_direct_oauth",
           "connection_status": "active", "page_id": page_id, "page_access_token": "tok"}
    if brand_id:
        doc["brand_id"] = brand_id
    return {**doc, **extra}


def outstand_conn():
    return {"id": "P94f0", "user_id": "u1", "platform": "facebook", "connected_via": "outstand",
            "connection_status": "active", "outstand_account_id": "P94f0"}


# ── posts_directly ────────────────────────────────────────────────────────────

def test_a_direct_connection_with_a_page_and_token_posts_directly():
    assert pd.posts_directly("facebook", direct())


def test_it_needs_the_page_and_its_token():
    assert not pd.posts_directly("facebook", direct(page_access_token=None))
    assert not pd.posts_directly("facebook", direct(page_id=None))


def test_an_outstand_connection_or_another_platform_does_not():
    assert not pd.posts_directly("facebook", outstand_conn())
    assert not pd.posts_directly("instagram", direct())
    assert not pd.posts_directly("facebook", None)


def test_the_switch_turns_it_off(monkeypatch):
    monkeypatch.setenv("FACEBOOK_POST_DIRECT", "false")
    assert not pd.posts_directly("facebook", direct())


# ── which connection is picked ────────────────────────────────────────────────

def test_the_direct_page_wins_even_when_a_newer_outstand_connection_exists():
    db = Db([outstand_conn(), direct()])
    assert _run(pd.preferred_direct_facebook(db, "u1"))["page_id"] == "p1"


def test_no_direct_connection_means_no_preference():
    assert _run(pd.preferred_direct_facebook(Db([outstand_conn()]), "u1")) is None
    assert _run(pd.preferred_direct_facebook(Db([direct(page_access_token=None)]), "u1")) is None


def test_another_users_page_is_never_used():
    assert _run(pd.preferred_direct_facebook(Db([direct()]), "someone-else")) is None


def test_a_brand_gets_its_own_page_not_another_brands():
    db = Db([direct("pA", brand_id="A"), direct("pB", brand_id="B")])
    assert _run(pd.preferred_direct_facebook(db, "u1", "B"))["page_id"] == "pB"
    assert _run(pd.preferred_direct_facebook(db, "u1", "C")) is None   # never borrows A's or B's


def test_a_brand_may_use_a_connection_that_belongs_to_no_brand():
    db = Db([direct("pA", brand_id="A"), direct("pLegacy")])
    assert _run(pd.preferred_direct_facebook(db, "u1", "C"))["page_id"] == "pLegacy"


def test_the_switch_turns_the_preference_off(monkeypatch):
    monkeypatch.setenv("FACEBOOK_POST_DIRECT", "false")
    assert _run(pd.preferred_direct_facebook(Db([direct()]), "u1")) is None


# ── publishing ────────────────────────────────────────────────────────────────

DRAFT = {"id": "d1", "platform": "facebook", "content": "Jollof Friday", "post_type": "feed", "brand_id": None}
POSTED = {"status": True, "responseData": {"id": "p1_9001"}}


def test_a_direct_page_posts_to_facebook_and_never_asks_outstand():
    list_accounts = AsyncMock(return_value={"data": [{"id": "P94f0", "network_unique_id": "p1"}]})
    post = AsyncMock(return_value=POSTED)
    with patch(OUTSTAND_LIST, new=list_accounts), patch.object(FacebookService, "post_on_facebook", new=post):
        result = _run(ApprovalWorkflowService._publish_to_platform("facebook", dict(DRAFT), direct(), None, db=Db([])))
    assert result["success"] is True and result["post_id"] == "p1_9001"
    assert post.await_count == 1 and post.await_args.kwargs["access_token"] == "tok"
    assert post.await_args.kwargs["page_id"] == "p1"
    assert list_accounts.await_count == 0


def test_a_direct_post_is_already_live_so_nothing_polls_for_it():
    post = AsyncMock(return_value=POSTED)
    with patch.object(FacebookService, "post_on_facebook", new=post):
        result = _run(ApprovalWorkflowService._publish_to_platform("facebook", dict(DRAFT), direct(), None, db=Db([])))
    assert "via" not in result and not pc.needs_confirmation(result)


def test_with_the_switch_off_a_direct_page_goes_through_outstand_again(monkeypatch):
    monkeypatch.setenv("FACEBOOK_POST_DIRECT", "false")
    list_accounts = AsyncMock(return_value={"data": []})
    post = AsyncMock(return_value=POSTED)
    with patch(OUTSTAND_LIST, new=list_accounts), patch.object(FacebookService, "post_on_facebook", new=post):
        _run(ApprovalWorkflowService._publish_to_platform("facebook", dict(DRAFT), direct(), None, db=Db([])))
    assert list_accounts.await_count == 1   # looked for the Page at Outstand first, as before


def test_a_meta_throttle_on_a_direct_post_comes_back_typed():
    from app.agents.social_media_manager.services import rate_limit as rl
    limit = rl.RateLimitInfo(kind=rl.LimitKind.TEMPORARY, platform="facebook", code="32")
    failed = {"status": False, "meta_error": {"code": 32}, "rate_limit": limit,
              "responseMessage": "Page request limit reached"}
    with patch.object(FacebookService, "post_on_facebook", new=AsyncMock(return_value=failed)):
        result = _run(ApprovalWorkflowService._publish_to_platform("facebook", dict(DRAFT), direct(), None, db=Db([])))
    assert result["success"] is False and result["rate_limit"].code == "32"

"""
Facebook posts go straight to the brand's Page through its direct Meta connection (Graph API);
the brand's own Page is picked, never another brand's. An account still recorded as connected
through Outstand (retired) is asked to reconnect instead of being posted to.
"""
import asyncio
from unittest.mock import AsyncMock, patch

from app.agents.social_media_manager.services import publish_dispatch as pd
from app.agents.social_media_manager.services.approval_workflow_service import ApprovalWorkflowService
from app.services.FacebookService import FacebookService



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


# ── publishing ────────────────────────────────────────────────────────────────

DRAFT = {"id": "d1", "platform": "facebook", "content": "Jollof Friday", "post_type": "feed", "brand_id": None}
POSTED = {"status": True, "responseData": {"id": "p1_9001"}}


def test_a_direct_page_posts_straight_to_facebook():
    post = AsyncMock(return_value=POSTED)
    with patch.object(FacebookService, "post_on_facebook", new=post):
        result = _run(ApprovalWorkflowService._publish_to_platform("facebook", dict(DRAFT), direct(), None, db=Db([])))
    assert result["success"] is True and result["post_id"] == "p1_9001"
    assert post.await_count == 1 and post.await_args.kwargs["access_token"] == "tok"
    assert post.await_args.kwargs["page_id"] == "p1"
    assert "via" not in result


def test_an_account_recorded_as_connected_through_outstand_is_asked_to_reconnect():
    post = AsyncMock(return_value=POSTED)
    with patch.object(FacebookService, "post_on_facebook", new=post):
        result = _run(ApprovalWorkflowService._publish_to_platform("facebook", dict(DRAFT), outstand_conn(), None, db=Db([])))
    assert result["success"] is False and result.get("needs_reconnect") is True
    assert "Reconnect your Facebook account" in result["error"]
    assert post.await_count == 0


def test_a_meta_throttle_on_a_direct_post_comes_back_typed():
    from app.agents.social_media_manager.services import rate_limit as rl
    limit = rl.RateLimitInfo(kind=rl.LimitKind.TEMPORARY, platform="facebook", code="32")
    failed = {"status": False, "meta_error": {"code": 32}, "rate_limit": limit,
              "responseMessage": "Page request limit reached"}
    with patch.object(FacebookService, "post_on_facebook", new=AsyncMock(return_value=failed)):
        result = _run(ApprovalWorkflowService._publish_to_platform("facebook", dict(DRAFT), direct(), None, db=Db([])))
    assert result["success"] is False and result["rate_limit"].code == "32"

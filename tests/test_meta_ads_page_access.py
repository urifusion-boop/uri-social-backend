"""Prod 2026-11-16: Page owners were told "you don't have full admin access to this Page" and
could never launch, however often they reconnected. Meta refuses the Page-side share with
"(#200) Permissions error" when the Page belongs to a Business portfolio (only the portfolio can
add a partner), and the system-user grant then fails too. Uri now sends the Page a partner
request from its own Business Manager, the launch check re-tries the grant until the owner
approves it, and someone with several Pages chooses which one instead of getting the first."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from mongomock_motor import AsyncMongoMockClient

from app.agents.jane_ads import ads_connection as AC
from app.agents.social_media_manager.routers import complete_social_manager as CSM

M = "app.services.MetaAdsService"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _db():
    return AsyncMongoMockClient()["t"]


PAGE = {"id": "pg1", "name": "Chidiogo", "access_token": "pt", "picture": {"data": {"url": "https://p/1.jpg"}}}


def _connect(share=None, assign=None, request=None):
    db = _db()
    with patch(f"{M}.share_page_with_business_manager", new=AsyncMock(side_effect=share)), \
         patch(f"{M}.assign_page_to_system_user", new=AsyncMock(side_effect=assign)), \
         patch(f"{M}.request_page_access", new=AsyncMock(return_value={"access_status": "PENDING"},
                                                         side_effect=request)) as req:
        out = _run(CSM._connect_ads_page(db, PAGE, "lt"))
    doc = _run(db["social_connections"].find_one({"id": "fbads_pg1"}))
    return out, doc, req


def test_both_grants_refused_sends_uris_partner_request():
    out, doc, req = _connect(share=RuntimeError("Business Manager page-share failed: (#200) Permissions error"),
                             assign=RuntimeError("Application does not have permission for this action"))
    assert out == {"page_id": "pg1", "page_name": "Chidiogo"}
    req.assert_awaited_once_with("pg1")
    assert doc["access_request_status"] == "PENDING" and doc["access_request_at"]
    assert doc["business_manager_shared"] is False and doc["system_user_assigned"] is False
    assert doc["profile_picture_url"] == "https://p/1.jpg"


def test_no_request_when_a_grant_worked_or_uri_already_has_the_page():
    _, doc, req = _connect()
    req.assert_not_awaited()
    assert doc["business_manager_shared"] is True and doc["access_request_status"] is None
    _, doc, req = _connect(share=RuntimeError("You are trying to assign a duplicated asset to this agency."),
                           assign=RuntimeError("x"))
    req.assert_not_awaited()


def test_a_failed_request_is_recorded_not_raised():
    _, doc, _ = _connect(share=RuntimeError("(#200)"), assign=RuntimeError("no"), request=RuntimeError("boom"))
    assert doc["access_request_status"] is None and "boom" in doc["access_request_error"]


def _stuck(db, **extra):
    doc = {"id": "fbads_pg1", "platform": "facebook_ads", "brand_id": "brnd_1", "connection_status": "active",
           "page_id": "pg1", "page_access_token": "pt", "user_access_token": "ut", "account_name": "Chidiogo",
           "business_manager_shared": False, "system_user_assigned": False,
           "business_manager_error": "(#200) Permissions error", **extra}
    _run(db["social_connections"].insert_one(doc))


def _state(db, assign=None, request=None):
    with patch(f"{M}.assign_page_to_system_user", new=AsyncMock(side_effect=assign)) as a, \
         patch(f"{M}.request_page_access", new=AsyncMock(return_value={"access_status": "PENDING"},
                                                         side_effect=request)) as r, \
         patch.object(AC, "verify_token_live", new=AsyncMock(return_value=(True, AC.REQUIRED_ADS_SCOPES))), \
         patch.object(AC, "get_ads_connection", new=lambda db_, u, b: db_["social_connections"].find_one({"id": "fbads_pg1"})):
        state, ads = _run(AC.resolve_connection_state(db, None, "brnd_1", require_whatsapp=False))
    return state, ads, a, r


def test_once_the_owner_approves_the_launch_goes_through_without_reconnecting():
    db = _db()
    _stuck(db, access_request_status="PENDING")
    state, _, a, _ = _state(db)
    assert state == AC.ConnectionState.READY
    a.assert_awaited_once()
    doc = _run(db["social_connections"].find_one({"id": "fbads_pg1"}))
    assert doc["system_user_assigned"] is True and doc["access_request_status"] == "APPROVED"


def test_still_waiting_stays_blocked_and_is_not_retried_every_call():
    db = _db()
    _stuck(db, access_request_status="PENDING")
    state, ads, a, r = _state(db, assign=RuntimeError("Application does not have permission for this action"))
    assert state == AC.ConnectionState.PAGE_NOT_SHARED and ads["access_request_status"] == "PENDING"
    r.assert_not_awaited()  # already sent
    state, _, a2, _ = _state(db, assign=RuntimeError("still no"))
    assert state == AC.ConnectionState.PAGE_NOT_SHARED
    a2.assert_not_awaited()  # tried under two minutes ago


def test_a_page_stuck_from_before_gets_its_request_sent_once():
    db = _db()
    _stuck(db)  # connected before partner requests existed
    _, ads, _, r = _state(db, assign=RuntimeError("no"))
    r.assert_awaited_once_with("pg1")
    assert ads["access_request_status"] == "PENDING"
    _run(db["social_connections"].update_one({"id": "fbads_pg1"}, {"$set": {
        "regrant_tried_at": datetime.now(timezone.utc) - timedelta(minutes=5)}}))
    _, _, _, r2 = _state(db, assign=RuntimeError("no"))
    r2.assert_not_awaited()


def test_a_badge_read_without_live_check_never_calls_meta():
    db = _db()
    _stuck(db, access_request_status="PENDING")
    with patch(f"{M}.assign_page_to_system_user", new=AsyncMock()) as a, \
         patch.object(AC, "get_ads_connection", new=lambda db_, u, b: db_["social_connections"].find_one({"id": "fbads_pg1"})):
        state, _ = _run(AC.resolve_connection_state(db, None, "brnd_1", live_check=False))
    assert state == AC.ConnectionState.PAGE_NOT_SHARED
    a.assert_not_awaited()


# ── several Pages: choose one ──────────────────────────────────────────────────

def _pending(db, age_minutes=0):
    _run(db["pending_fb_ads_pages"].insert_one({
        "token": "tok", "long_token": "lt", "created_at": datetime.utcnow() - timedelta(minutes=age_minutes),
        "pages": [{"id": "pg1", "name": "Chidiogo", "access_token": "pt1", "picture": "https://p/1.jpg"},
                  {"id": "pg2", "name": "Chidi Beauty", "access_token": "pt2", "picture": ""}]}))


def test_the_choices_never_include_page_tokens():
    db = _db()
    _pending(db)
    out = _run(CSM.facebook_ads_pending_pages("tok", db=db, ctx={}))
    assert out == {"pages": [{"id": "pg1", "name": "Chidiogo", "picture": "https://p/1.jpg"},
                             {"id": "pg2", "name": "Chidi Beauty", "picture": ""}]}


def test_picking_connects_that_page_and_uses_up_the_choice():
    db = _db()
    _pending(db)
    with patch.object(CSM, "_connect_ads_page", new=AsyncMock(return_value={"page_id": "pg2", "page_name": "Chidi Beauty"})) as c:
        out = _run(CSM.facebook_ads_pick_page(CSM.FacebookAdsPickBody(token="tok", page_id="pg2"), db=db, ctx={}))
    assert out == {"fb_page_id": "pg2", "page_name": "Chidi Beauty"}
    assert c.await_args.args[1]["access_token"] == "pt2" and c.await_args.args[2] == "lt"
    assert _run(db["pending_fb_ads_pages"].find_one({"token": "tok"})) is None


def test_an_old_or_unknown_choice_is_refused():
    from fastapi import HTTPException
    db = _db()
    _pending(db, age_minutes=90)
    with pytest.raises(HTTPException) as e:
        _run(CSM.facebook_ads_pending_pages("tok", db=db, ctx={}))
    assert e.value.status_code == 404
    db = _db()
    _pending(db)
    with pytest.raises(HTTPException) as e:
        _run(CSM.facebook_ads_pick_page(CSM.FacebookAdsPickBody(token="tok", page_id="someone_elses"), db=db, ctx={}))
    assert e.value.status_code == 400


def test_the_partner_request_goes_from_uris_business_manager():
    from app.services import MetaAdsService as S
    calls = []

    class Resp:
        def json(self):
            return {"access_status": "PENDING"}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, params=None):
            calls.append((url, params))
            return Resp()

    with patch.multiple(S.settings, META_BUSINESS_MANAGER_ID="bm1", META_ADS_ACCESS_TOKEN="sys", FACEBOOK_API_VERSION="v21.0"), \
         patch.object(S.httpx, "AsyncClient", return_value=Client()):
        assert _run(S.request_page_access("pg1")) == {"access_status": "PENDING"}
    url, params = calls[0]
    assert url.endswith("/bm1/client_pages") and params["page_id"] == "pg1"
    assert params["permitted_tasks"] == '["ADVERTISE"]' and params["access_token"] == "sys"

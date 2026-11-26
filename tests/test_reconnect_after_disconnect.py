"""
Live-reported 2026-11-26: after disconnecting a Facebook Page, connecting the same Page again said
"Facebook connected" but the card stayed "Not connected". Disconnecting leaves a mark so the account
stays gone, and the reconnected Page (same id, fb_<page_id>) kept being hidden by it.

A connection made after the mark is the owner connecting it again: it shows, and the mark goes.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from mongomock_motor import AsyncMongoMockClient

from app.agents.social_media_manager.services import social_account_service as svc

USER = "u1"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _page(connected_at):
    return {"id": "fb_123", "user_id": USER, "platform": "facebook", "connected_via": "facebook_direct_oauth",
            "page_id": "123", "account_name": "Uri Social", "connection_status": "active",
            "connected_at": connected_at}


def _facebook(db):
    outstand = AsyncMock()
    outstand.list_accounts = AsyncMock(return_value={"data": []})
    with patch.object(svc, "OutstandService", return_value=outstand):
        out = _run(svc.SocialAccountService.get_user_connections(db, USER, None))
    return out["responseData"]["connections"].get("facebook", [])


def _mark(db, when):
    _run(db["disconnected_accounts"].insert_one(
        {"user_id": USER, "outstand_account_id": "fb_123", "platform": "facebook", "disconnected_at": when}))


def test_a_page_connected_again_after_it_was_disconnected_shows():
    db = AsyncMongoMockClient()["t_reconnect"]
    _mark(db, datetime.utcnow() - timedelta(minutes=10))
    _run(db["social_connections"].insert_one(_page(datetime.now(timezone.utc).isoformat())))
    assert [a["account_name"] for a in _facebook(db)] == ["Uri Social"]
    assert _run(db["disconnected_accounts"].count_documents({})) == 0


def test_a_connection_older_than_the_disconnect_stays_hidden():
    db = AsyncMongoMockClient()["t_reconnect_old"]
    _mark(db, datetime.utcnow())
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    _run(db["social_connections"].insert_one(_page(old)))
    assert _facebook(db) == []
    no_time = {**_page(old)}
    no_time.pop("connected_at")
    _run(db["social_connections"].replace_one({"id": "fb_123"}, no_time))
    assert _facebook(db) == []
    assert _run(db["disconnected_accounts"].count_documents({})) == 1

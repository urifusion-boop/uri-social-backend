"""
Live-reported on staging 2026-11-26: after "Disconnect All" on Facebook, connecting the same Page
again said "Facebook connected" but the card stayed "Not connected". Disconnecting leaves a mark
so the account stays gone, and the reconnected Page (same id) kept being hidden by it.

A connection made after the mark is the owner connecting it again: it shows, and the mark goes.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from mongomock_motor import AsyncMongoMockClient

from app.agents.social_media_manager.services.social_account_service import SocialAccountService as S

USER = "u1"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _page(connected_at):
    return {"id": "fb_123", "user_id": USER, "platform": "facebook", "connected_via": "facebook_direct_oauth",
            "page_id": "123", "account_name": "Uri Social", "connection_status": "active",
            "connected_at": connected_at}


def _facebook(db):
    out = _run(S.get_user_connections(db, USER, None))
    return out["responseData"]["connections"].get("facebook", [])


def test_a_page_connected_again_after_disconnect_all_shows():
    db = AsyncMongoMockClient()["t_reconnect"]
    before = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _run(db["social_connections"].insert_one(_page(before)))
    assert [a["id"] for a in _facebook(db)] == ["fb_123"]

    _run(S.disconnect_all_for_platform(db, USER, "facebook", None))
    assert _facebook(db) == []

    # Connect the same Page again, the way the Facebook connect saves it.
    now = datetime.now(timezone.utc).isoformat()
    _run(db["social_connections"].update_one({"id": "fb_123"}, {"$set": _page(now)}, upsert=True))
    assert [a["account_name"] for a in _facebook(db)] == ["Uri Social"]
    assert _run(db["disconnected_accounts"].count_documents({})) == 0


def test_a_connection_older_than_the_disconnect_stays_hidden():
    db = AsyncMongoMockClient()["t_reconnect_old"]
    _run(db["disconnected_accounts"].insert_one(
        {"user_id": USER, "account_id": "fb_123", "disconnected_at": datetime.utcnow()}))
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    _run(db["social_connections"].insert_one(_page(old)))
    assert _facebook(db) == []
    no_time = {**_page(old)}
    no_time.pop("connected_at")
    _run(db["social_connections"].replace_one({"id": "fb_123"}, no_time))
    assert _facebook(db) == []
    assert _run(db["disconnected_accounts"].count_documents({})) == 1

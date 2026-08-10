"""
Live-diagnosed real bug: a personal-brand user's facebook_ads connection was never
tagged with brand_id (only agency/non-personal brands get that), so every Page that
user ever connected via Ads OAuth stayed matched by get_ads_connection's user_id-wide
lookup — reconnecting a DIFFERENT page didn't retire the old one, it just added
another "active" row, and whichever had the newest connected_at silently won. A real
account connected "Living the truth", then later reconnected an unrelated page for
other testing, and every ad kept using the unrelated page with no way to tell from the
data which one was actually meant to be in use.

facebook_ads_finalize now supersedes every other active connection in the same scope
(user_id for personal, brand_id for agency) before activating the new one, so exactly
one connection is ever active at a time.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.agents.social_media_manager.routers.complete_social_manager import facebook_ads_finalize


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _fake_db():
    # Each collection needs its own mock — the finalize call also touches
    # jane_ads_meta_campaigns, and a shared mock across collections clobbers
    # call_args on social_connections with that later, unrelated call.
    collections: dict = {}

    def _collection(name):
        if name not in collections:
            collections[name] = MagicMock(
                update_many=AsyncMock(),
                update_one=AsyncMock(return_value=MagicMock(matched_count=1)),
            )
        return collections[name]

    db = MagicMock()
    db.__getitem__ = MagicMock(side_effect=_collection)
    return db


def test_personal_brand_supersedes_by_user_id_not_by_brand():
    db = _fake_db()
    ctx = {"user_id": "u1", "brand_id": "brnd_personal_u1"}
    with patch(
        "app.models.brand_account.BrandAccount.personal_brand_id",
        return_value="brnd_personal_u1",
    ):
        _run(facebook_ads_finalize(fb_page_id="new_page", db=db, ctx=ctx))

    conn_collection = db.__getitem__("social_connections")
    supersede_call = conn_collection.update_many.call_args
    query, update = supersede_call.args
    assert query["platform"] == "facebook_ads"
    assert query["connection_status"] == "active"
    assert query["id"] == {"$ne": "fbads_new_page"}
    assert query["user_id"] == "u1"
    assert "brand_id" not in query
    assert update["$set"]["connection_status"] == "superseded"


def test_agency_brand_supersedes_scoped_to_brand_id_only():
    db = _fake_db()
    ctx = {"user_id": "u1", "brand_id": "brnd_agency_client42"}
    with patch(
        "app.models.brand_account.BrandAccount.personal_brand_id",
        return_value="brnd_personal_u1",
    ):
        _run(facebook_ads_finalize(fb_page_id="new_page", db=db, ctx=ctx))

    conn_collection = db.__getitem__("social_connections")
    query, _update = conn_collection.update_many.call_args.args
    assert query["brand_id"] == "brnd_agency_client42"
    assert "user_id" not in query


def test_the_new_connection_itself_is_excluded_from_supersession():
    db = _fake_db()
    ctx = {"user_id": "u1", "brand_id": "brnd_personal_u1"}
    with patch(
        "app.models.brand_account.BrandAccount.personal_brand_id",
        return_value="brnd_personal_u1",
    ):
        _run(facebook_ads_finalize(fb_page_id="living_truth_page", db=db, ctx=ctx))

    conn_collection = db.__getitem__("social_connections")
    query, _update = conn_collection.update_many.call_args.args
    assert query["id"] == {"$ne": "fbads_living_truth_page"}

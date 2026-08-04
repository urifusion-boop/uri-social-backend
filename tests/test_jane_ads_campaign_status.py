"""
Unit tests for the pause/resume endpoint's self-heal (set_meta_campaign_status).

The endpoint is called directly with explicit db/brand_ctx rather than through HTTP —
its FastAPI Depends() are plain defaults, so this exercises the real handler without
standing up the app. The Meta adapter is mocked; what's under test is how the handler
reacts to Meta's specific "this campaign was deleted" rejection.

Live-confirmed case this covers: a campaign deleted on Meta's side (in Ads Manager or
a manual cleanup) left a stale row in jane_ads_meta_campaigns, and pausing it surfaced
Meta's raw "Deleted campaigns can't be edited" text to the user as a 502. The campaign
LIST already self-healed on exactly this; the status endpoint didn't.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.agents.jane_ads.adapters.meta import MetaAPIError
from app.agents.jane_ads.router import (
    META_DELETED_CAMPAIGN_SUBCODE,
    CampaignStatusBody,
    set_meta_campaign_status,
)

BRAND = "brnd_personal_abc"
CAMPAIGN = "52548217874410"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class FakeCollection:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    async def find_one(self, query, *a, **kw):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return dict(d)
        return None

    async def delete_one(self, query):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not all(d.get(k) == v for k, v in query.items())]
        return type("R", (), {"deleted_count": before - len(self.docs)})()


class FakeDb:
    def __init__(self, docs=None):
        self._coll = FakeCollection(docs)

    def __getitem__(self, name):
        return self._coll


def _db_with_campaign():
    return FakeDb([{"campaign_id": CAMPAIGN, "brand_id": BRAND, "adset_id": "a1", "ad_id": "ad1"}])


def _configured_settings():
    return patch.multiple(
        "app.core.config.settings",
        META_AD_ACCOUNT_ID="1361196959314321",
        META_ADS_ACCESS_TOKEN="tok",
    )


def _adapter_raising(err):
    return patch(
        "app.agents.jane_ads.adapters.meta.MetaAdPlatformAdapter.set_delivery",
        new=AsyncMock(side_effect=err),
    )


def test_deleted_campaign_returns_410_and_drops_the_stale_row():
    db = _db_with_campaign()
    err = MetaAPIError("campaign status update: Deleted campaigns can't be edited",
                       code=100, subcode=META_DELETED_CAMPAIGN_SUBCODE)
    with _configured_settings(), _adapter_raising(err):
        with pytest.raises(HTTPException) as exc:
            _run(set_meta_campaign_status(
                CAMPAIGN, CampaignStatusBody(active=False),
                db=db, brand_ctx={"brand_id": BRAND, "user_id": "u1"},
            ))
    assert exc.value.status_code == 410
    # Plain language, not Meta's raw error text.
    assert "no longer exists" in exc.value.detail
    assert "subcode" not in exc.value.detail.lower()
    # Self-healed: the stale record is gone, so it stops haunting the list.
    assert _run(db["jane_ads_meta_campaigns"].find_one({"campaign_id": CAMPAIGN})) is None


def test_other_meta_errors_keep_their_existing_handling_and_the_row():
    # A permissions/rate-limit/etc. failure is NOT evidence the campaign is gone —
    # deleting the row there would lose a live campaign from the user's list.
    db = _db_with_campaign()
    err = MetaAPIError("campaign status update: Missing Permissions", code=200, subcode=4841013)
    with _configured_settings(), _adapter_raising(err):
        with pytest.raises(HTTPException) as exc:
            _run(set_meta_campaign_status(
                CAMPAIGN, CampaignStatusBody(active=False),
                db=db, brand_ctx={"brand_id": BRAND, "user_id": "u1"},
            ))
    assert exc.value.status_code != 410
    assert _run(db["jane_ads_meta_campaigns"].find_one({"campaign_id": CAMPAIGN})) is not None


def test_a_successful_toggle_is_returned_untouched():
    db = _db_with_campaign()
    ok = {"status": "PAUSED", "updated": {"campaign": True, "adset": True, "ad": True}}
    with _configured_settings(), patch(
        "app.agents.jane_ads.adapters.meta.MetaAdPlatformAdapter.set_delivery",
        new=AsyncMock(return_value=ok),
    ):
        result = _run(set_meta_campaign_status(
            CAMPAIGN, CampaignStatusBody(active=False),
            db=db, brand_ctx={"brand_id": BRAND, "user_id": "u1"},
        ))
    assert result == ok
    assert _run(db["jane_ads_meta_campaigns"].find_one({"campaign_id": CAMPAIGN})) is not None


def test_another_brands_campaign_is_still_404_not_self_healed():
    db = _db_with_campaign()
    with _configured_settings():
        with pytest.raises(HTTPException) as exc:
            _run(set_meta_campaign_status(
                CAMPAIGN, CampaignStatusBody(active=False),
                db=db, brand_ctx={"brand_id": "brnd_someone_else", "user_id": "u2"},
            ))
    assert exc.value.status_code == 404
    # Crucially, another brand's failed lookup must never delete the owner's row.
    assert _run(db["jane_ads_meta_campaigns"].find_one({"campaign_id": CAMPAIGN})) is not None

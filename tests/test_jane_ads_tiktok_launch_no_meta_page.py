"""
A TikTok launch must not depend on the brand's Facebook connection.

TikTok ads run under URI's own advertiser and identity, so there is no Meta Page involved
(plan time already skips the Meta lookup for TikTok). The launch route used to re-resolve
the brand's Meta Page for everything except Google, so a brand whose Facebook token had
lapsed got `meta_connection_expired` on a TikTok launch. A Meta plan must still get it.

The destination is a website with no URL, so a launch that gets past the Meta gate stops at
the very next check ("nowhere to send people", 400) — before any wallet or ad platform call.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.agents.jane_ads import router as jane_router
from app.agents.jane_ads.ads_connection import AdsConnectionRequired, ConnectionState
from app.agents.jane_ads.models import CampaignPlan, CampaignRequest, Goal, Platform, PlatformPlan, PurchaseBehaviour

BRAND = {"brand_id": "brand-1", "user_id": "user-1"}
NO_LINK_DESTINATION = {
    "destination_type": "website", "whatsapp_number": "", "website_url": "",
    "instagram_username": "", "custom_url": "", "destination_cta": "",
}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _doc(platform):
    plan = CampaignPlan(
        business_id="b1", goal=Goal.MESSAGES, behaviour=PurchaseBehaviour.DISCOVER,
        platforms=[PlatformPlan(platform=platform, budget_ngn=31_000, days=1, variants=1, test_scope="none")],
        per_business_cap_ngn=31_000, account_cap_ngn=31_000,
    )
    req = CampaignRequest(business_id="b1", budget_ngn=31_000)
    return {
        "plan_id": "p1", "brand_id": "brand-1", "business_id": "b1", "status": "pending",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "plan": plan.model_dump(mode="json"), "req": req.model_dump(mode="json"),
    }


class _Coll:
    def __init__(self, doc):
        self.doc = doc

    async def find_one(self, query):
        return self.doc


class _Db:
    def __init__(self, doc):
        self.plans = _Coll(doc)

    def __getitem__(self, name):
        return self.plans


def _launch(platform):
    """Launch a pending `platform` plan for a brand whose Meta connection has expired."""
    doc = _doc(platform)
    resolve = AsyncMock(side_effect=AdsConnectionRequired(ConnectionState.EXPIRED))
    with patch("app.agents.jane_ads.launch_governance.require_launch_role", AsyncMock()), \
         patch.object(jane_router, "_release_if_stale_launch", AsyncMock(return_value=doc)), \
         patch("app.agents.jane_ads.brief.load", AsyncMock(return_value={})), \
         patch("app.agents.jane_ads.brief.check_plan", return_value=[]), \
         patch("app.agents.jane_ads.brief.unresolved_blocks", return_value=[]), \
         patch("app.agents.jane_ads.destination.get_brand_destination",
               AsyncMock(return_value=NO_LINK_DESTINATION)), \
         patch("app.agents.jane_ads.whatsapp.get_brand_whatsapp", AsyncMock(return_value="")), \
         patch("app.agents.jane_ads.ads_connection.resolve_ads_page_for_launch", resolve):
        with pytest.raises(HTTPException) as err:
            _run(jane_router.meta_launch_plan("p1", None, _Db(doc), BRAND))
    return err.value, resolve


def test_a_tiktok_launch_is_not_blocked_by_an_expired_facebook_connection():
    err, resolve = _launch(Platform.TIKTOK)
    resolve.assert_not_called()
    assert err.status_code == 400
    assert "nowhere to send people" in err.detail


def test_a_meta_launch_still_needs_a_working_facebook_connection():
    err, resolve = _launch(Platform.META)
    resolve.assert_called_once()
    assert (err.status_code, err.detail) == (409, "meta_connection_expired")

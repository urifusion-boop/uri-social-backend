"""
TikTok pays no URI fee, so "Keep it running" on a TikTok campaign costs exactly its ad
spend — the same as the campaign itself did at launch. Meta's extension keeps its fee.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.agents.jane_ads import constants as C
from app.agents.jane_ads import router as jane_router
from app.core.config import settings

BRAND = {"brand_id": "brand-1", "user_id": "user-1"}
SCHEDULE = {"daily_ngn": 31_000.0, "end_time": "2026-10-27 12:55:00", "has_ended": False,
            "budget_ngn": 31_000.0, "adgroup_id": "222"}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _Coll:
    def __init__(self, docs=None):
        self.docs = {d["campaign_id"]: d for d in (docs or [])}

    async def find_one(self, query):
        return self.docs.get(query.get("campaign_id"))


class _Db:
    def __init__(self, tiktok=None):
        self.colls = {"jane_ads_meta_campaigns": _Coll(), "jane_ads_tiktok_campaigns": _Coll(tiktok)}

    def __getitem__(self, name):
        return self.colls.setdefault(name, _Coll())


@pytest.fixture(autouse=True)
def _tiktok_configured(monkeypatch):
    monkeypatch.setattr(settings, "TIKTOK_ADS_ADVERTISER_ID", "adv123", raising=False)
    monkeypatch.setattr(settings, "TIKTOK_ADS_ACCESS_TOKEN", "tok", raising=False)


def test_a_tiktok_extension_costs_exactly_its_ad_spend():
    # A record launched with a fee-based markup must not drag one into the extension.
    db = _Db(tiktok=[{"campaign_id": "tt1", "brand_id": "brand-1", "business_id": "b1",
                      "ad_spend_markup": C.AD_SPEND_MARKUP}])
    with patch("app.agents.jane_ads.adapters.tiktok.TikTokAdsAdapter.fetch_adgroup_schedule",
               AsyncMock(return_value=SCHEDULE)), \
         patch("app.agents.jane_ads.wallet.WalletService.get_balance", AsyncMock(return_value=500_000.0)):
        quote = _run(jane_router.extend_quote("tt1", days=2, db=db, brand_ctx=BRAND))

    assert quote["ad_spend_ngn"] == 62_000.0
    assert quote["total_due_ngn"] == 62_000.0   # no fee on top
    assert quote["affordable"] is True


def test_an_extension_the_wallet_cannot_cover_is_refused_at_the_quoted_amount():
    db = _Db(tiktok=[{"campaign_id": "tt1", "brand_id": "brand-1", "business_id": "b1"}])
    with patch("app.agents.jane_ads.adapters.tiktok.TikTokAdsAdapter.fetch_adgroup_schedule",
               AsyncMock(return_value=SCHEDULE)), \
         patch("app.agents.jane_ads.wallet.WalletService.get_balance", AsyncMock(return_value=50_000.0)), \
         patch("app.agents.jane_ads.adapters.tiktok.TikTokAdsAdapter.extend_adgroup", AsyncMock()) as extend:
        with pytest.raises(HTTPException) as e:
            _run(jane_router.extend_campaign(
                "tt1", jane_router.ExtendBody(days=2, confirm=True), db=db, brand_ctx=BRAND))

    assert e.value.status_code == 402
    assert "62,000" in e.value.detail
    extend.assert_not_called()   # nothing was extended, so nothing was charged

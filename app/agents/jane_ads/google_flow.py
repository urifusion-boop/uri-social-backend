"""
Jane + Ads — Google in the campaign flow.

Meta and TikTok campaigns all run in ONE shared URI ad account, so the router
builds one adapter from settings and uses it everywhere. Google is different:
every brand's campaigns run in that brand's OWN Google Ads account (linked to
URI's manager account — see google_ads_connection.py). So the adapter has to be
built per brand, or per campaign.

This module is that difference, kept in one place so the router's campaign
flow (plan → launch → list → pause/resume → delete → hourly spend sweep) can
treat Google like the other two platforms:

  • is_ready()            — can this brand run Google ads right now? (no network)
  • adapter_for_launch()  — the brand's adapter, after the same pre-flight gate
                            a real launch must pass (link re-checked with Google)
  • GoogleCampaigns       — an "adapter" over ALL Google campaigns, following the
                            fetch_campaign_summary / set_delivery / delete_campaign
                            convention billing.py and the campaign list already use;
                            it finds each campaign's own account for you
  • budget_too_low()      — Search needs enough clicks a day to learn anything
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException

from app.core.config import settings

from .adapters.google import (
    COLLECTION as GOOGLE_CAMPAIGNS,
    GoogleAdsAdapter,
    GoogleAdsAPIError,
    _DEFAULT_ESTIMATED_CPC_NGN,
    _MIN_USEFUL_CLICKS_PER_DAY,
    estimate_clicks_per_day,
)
from .google_ads_connection import (
    CONNECTIONS,
    AdsConnectionRequired,
    ConnectionState,
    GoogleAdsConnectionError,
    get_admin_valid_access_token,
    resolve_connection_state,
    resolve_customer_id_for_launch,
)

__all__ = ["GOOGLE_CAMPAIGNS", "GoogleCampaigns", "GoogleAdsAPIError", "adapter_for_launch", "budget_too_low",
           "configured", "find_record", "is_ready", "raise_http", "ads_url"]


def configured() -> bool:
    """URI's own side of Google Ads is set up (developer token + manager account)."""
    return bool(settings.GOOGLE_ADS_DEVELOPER_TOKEN and settings.GOOGLE_ADS_MCC_CUSTOMER_ID)


async def is_ready(db, user_id: Optional[str], brand_id: Optional[str]) -> bool:
    """This brand has a linked Google Ads account, per our own record. No
    network call — used while planning, where it's asked on every message.
    The link is re-checked with Google itself at launch."""
    if not configured():
        return False
    try:
        state, _ = await resolve_connection_state(db, user_id, brand_id)
    except Exception:
        return False
    return state == ConnectionState.READY


def raise_http(e: Exception) -> None:
    """A Google failure as the HTTP error the campaign flow returns."""
    raise HTTPException(status_code=502, detail=str(e))


async def adapter_for_launch(db, brand_ctx: dict) -> tuple[GoogleAdsAdapter, dict]:
    """The brand's own adapter, through the pre-flight gate: the link is checked
    against Google right now and a WhatsApp number must be on file. Raises the
    HTTP error the launch endpoints return (409 with the connection state)."""
    try:
        conn = await resolve_customer_id_for_launch(db, brand_ctx.get("user_id"), brand_ctx.get("brand_id"))
    except AdsConnectionRequired as e:
        raise HTTPException(status_code=409, detail=f"google_ads_connection_{e.state.value}")
    except GoogleAdsConnectionError as e:
        raise HTTPException(status_code=502, detail=str(e))
    try:
        adapter = GoogleAdsAdapter(
            db, customer_id=conn["customer_id"], login_customer_id=conn["login_customer_id"],
            access_token=conn["access_token"], currency_code=conn.get("currency_code", ""),
        )
    except GoogleAdsAPIError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return adapter, conn


async def find_record(db, campaign_id: str, brand_id: Optional[str]) -> Optional[dict]:
    """A Google campaign of THIS brand, or None."""
    if not brand_id:
        return None
    record = await db[GOOGLE_CAMPAIGNS].find_one({"campaign_id": campaign_id})
    # Only a record the Google adapter itself wrote — never a look-alike id.
    if record and record.get("brand_id") == brand_id and record.get("platform") == "google":
        return record
    return None


def ads_url(customer_id: str) -> str:
    return f"https://ads.google.com/aw/campaigns?__c={customer_id}"


def budget_too_low(budget_ngn: float, days: int) -> Optional[str]:
    """Why this budget can't work on Google Search, or None when it can. A
    search campaign needs enough clicks a day to learn what's working; below
    that the money is spent without a result. Said while planning, in naira,
    instead of failing at launch."""
    days = max(int(days or 1), 1)
    daily = float(budget_ngn or 0) / days
    if estimate_clicks_per_day(daily, _DEFAULT_ESTIMATED_CPC_NGN) >= _MIN_USEFUL_CLICKS_PER_DAY:
        return None
    needed = _MIN_USEFUL_CLICKS_PER_DAY * _DEFAULT_ESTIMATED_CPC_NGN * days
    return (f"On Google Search, ₦{budget_ngn:,.0f} over {days} day{'s' if days != 1 else ''} is about "
            f"₦{daily:,.0f} a day — too little to get the ~{_MIN_USEFUL_CLICKS_PER_DAY:.0f} clicks a day a search ad "
            f"needs to work. Raise the budget to about ₦{needed:,.0f}, run it for fewer days, or let me use "
            f"Instagram and Facebook instead.")


class GoogleCampaigns:
    """All Google campaigns behind the one-adapter convention the campaign list
    and billing.py's sweep use (fetch_campaign_summary / set_delivery /
    delete_campaign by campaign_id). Each call finds the campaign's own Google
    Ads account and currency, and talks to it as URI's admin identity."""

    def __init__(self, db) -> None:
        self._db = db
        self._token: Optional[str] = None
        self._currency: dict[str, str] = {}

    async def _adapter(self, campaign_id: str) -> GoogleAdsAdapter:
        record = await self._db[GOOGLE_CAMPAIGNS].find_one({"campaign_id": campaign_id})
        if not record or not record.get("customer_id"):
            raise GoogleAdsAPIError(f"no Google campaign record for campaign_id={campaign_id}")
        customer_id = record["customer_id"]
        if customer_id not in self._currency:
            currency = record.get("currency_code") or ""
            if not currency:
                conn = await self._db[CONNECTIONS].find_one({"platform": "google_ads", "customer_id": customer_id})
                currency = (conn or {}).get("currency_code") or ""
            self._currency[customer_id] = currency
        if self._token is None:
            try:
                self._token = await get_admin_valid_access_token(self._db)
            except GoogleAdsConnectionError as e:
                raise GoogleAdsAPIError(str(e))
        return GoogleAdsAdapter(
            self._db, customer_id=customer_id,
            login_customer_id=record.get("login_customer_id") or settings.GOOGLE_ADS_MCC_CUSTOMER_ID,
            access_token=self._token, currency_code=self._currency[customer_id],
        )

    async def fetch_campaign_summary(self, campaign_id: str) -> dict:
        return await (await self._adapter(campaign_id)).fetch_campaign_summary(campaign_id)

    async def set_delivery(self, campaign_id: str, active: bool) -> dict:
        return await (await self._adapter(campaign_id)).set_delivery(campaign_id, active)

    async def delete_campaign(self, campaign_id: str) -> bool:
        return await (await self._adapter(campaign_id)).delete_campaign(campaign_id)

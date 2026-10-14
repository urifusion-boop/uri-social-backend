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

from datetime import datetime, timezone
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


# How a brand's Google ads run — its own choice, in Connected Accounts:
#   "uri" — in URI's own Google Ads account. Nothing to connect; the whole budget
#           comes from the URI wallet and URI pays Google. The default.
#   "own" — in the brand's own Google Ads account. Google bills the brand's card
#           for the ad spend; the URI wallet pays only URI's fee.
SETTINGS = "jane_ads_google_settings"
MODES = ("uri", "own")


async def get_mode(db, brand_id: Optional[str]) -> str:
    if not brand_id:
        return "uri"
    doc = await db[SETTINGS].find_one({"brand_id": brand_id, "kind": "google_mode"})
    return (doc or {}).get("mode") if (doc or {}).get("mode") in MODES else "uri"


async def set_mode(db, brand_id: str, mode: str) -> str:
    if mode not in MODES:
        raise ValueError("mode must be 'uri' or 'own'")
    from datetime import datetime, timezone
    await db[SETTINGS].update_one(
        {"brand_id": brand_id, "kind": "google_mode"},
        {"$set": {"mode": mode, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return mode


NOT_AVAILABLE = ("Google ads run by URI aren't available just yet — we're setting them up. "
                 "You can connect your own Google Ads account instead (Connected Accounts → Google Ads), "
                 "or run this on Instagram & Facebook.")


async def readiness(db, user_id: Optional[str], brand_id: Optional[str]) -> tuple[bool, str]:
    """(ready, what to tell the owner if not) for the brand's chosen way of
    running Google ads. No network call — asked on every planning message."""
    if not configured():
        return False, "Google ads aren't available on URI right now. Try Instagram & Facebook instead."
    mode = await get_mode(db, brand_id)
    if mode == "own":
        try:
            state, _ = await resolve_connection_state(db, user_id, brand_id)
        except Exception as e:
            print(f"[google_flow] connection check failed: {e}", flush=True)
            return False, "Couldn't check your Google Ads account just now. Try again in a minute."
        if state == ConnectionState.READY:
            return True, ""
        return False, (connection_message(state.value) + " Or switch to “Run by URI” there — "
                       "nothing to connect.")
    from .google_uri_accounts import available
    try:
        return (True, "") if await available(db, brand_id) else (False, NOT_AVAILABLE)
    except Exception as e:
        print(f"[google_flow] readiness check failed: {e}", flush=True)
        return False, "Couldn't check Google ads just now. Try again in a minute."


async def is_ready(db, user_id: Optional[str], brand_id: Optional[str]) -> bool:
    return (await readiness(db, user_id, brand_id))[0]


_CONNECT_HERE = "Connected Accounts → Google Ads"

_CONNECTION_MESSAGES = {
    "none": f"Google Ads isn't connected for this brand yet. Connect it in {_CONNECT_HERE}, then try again.",
    "needs_account_selection": f"Choose which Google Ads account to use in {_CONNECT_HERE}, then try again.",
    "manager_link_pending": (f"Your Google Ads account hasn't approved URI yet. {_CONNECT_HERE} shows how "
                             "to approve it — then try again."),
    "manager_link_refused": (f"Google won't let URI manage that Google Ads account. Open {_CONNECT_HERE} "
                             "for why, or choose another account."),
    "manager_link_declined": ("URI no longer has access to your Google Ads account — the request was declined "
                              f"or the link was removed in Google Ads. Reconnect it in {_CONNECT_HERE}."),
    "no_whatsapp": "Add your business WhatsApp number in Settings first, then try again.",
}


def connection_message(state: str) -> str:
    """Why Google Ads isn't usable right now, and where to fix it."""
    return _CONNECTION_MESSAGES.get(state, f"Google Ads needs attention in {_CONNECT_HERE}.")


# Google's error codes (the [CODE] the adapter appends) → what the owner can do.
_ERROR_MESSAGES = [
    (("CUSTOMER_NOT_ENABLED", "CUSTOMER_NOT_ACTIVE"),
     "Your Google Ads account isn't active — it may be cancelled or suspended. Check it in Google Ads."),
    (("USER_PERMISSION_DENIED", "CUSTOMER_NOT_FOUND", "NOT_ADS_USER", "ACTION_NOT_PERMITTED"),
     f"URI can't reach your Google Ads account any more. Reconnect it in {_CONNECT_HERE}."),
    (("BILLING_SETUP", "NO_BILLING", "PAYMENTS"),
     "Your Google Ads account has no working payment method. Add one in Google Ads → Billing, then try again."),
    (("POLICY_FINDING", "POLICY_VIOLATION", "PROHIBITED"),
     "Google won't accept the ad's wording under its ad policies. Ask Jane to reword it, then try again."),
    (("RESOURCE_COUNT_LIMIT", "ACCOUNT_LIMIT", "CAMPAIGN_LIMIT"),
     "Your Google Ads account has reached Google's limit on campaigns. Remove old ones in Google Ads, then try again."),
    (("RESOURCE_EXHAUSTED", "RATE_EXCEEDED", "QUOTA"),
     "Google is busy right now. Wait a minute and try again."),
    (("UNAUTHENTICATED", "OAUTH_TOKEN", "AUTHENTICATION"),
     "URI's connection to Google needs refreshing. Try again in a minute — if it keeps happening, contact support."),
]


def friendly_error(e: Exception) -> str:
    """A Google Ads failure in words an owner can act on. Messages the adapter
    already wrote for people (currency, budget) pass through; Google's own
    technical text is kept only as a short reference at the end."""
    text = str(e)
    # Match Google's own error codes (the adapter puts them in [BRACKETS]) — never
    # the free text, where "payments accounts lookup" read as a billing problem.
    import re as _re
    codes_found = " ".join(_re.findall(r"\[([A-Z_]+)\]", text))
    for codes, message in _ERROR_MESSAGES:
        if codes_found and any(code in codes_found for code in codes):
            return message
    if ":" not in text or text.startswith(("This Google Ads account", "That daily budget")):
        return text
    detail = text.split(":", 1)[1].strip()
    return f"Google Ads didn't accept this: {detail[:180]}. Nothing was charged — try again, or contact support."


def raise_http(e: Exception) -> None:
    """A Google failure as the HTTP error the campaign flow returns."""
    raise HTTPException(status_code=502, detail=friendly_error(e))


async def adapter_for_launch(db, brand_ctx: dict) -> tuple[GoogleAdsAdapter, dict]:
    """The adapter for wherever this brand's Google ads run: URI's account (given
    to the brand the first time) or the brand's own linked account, re-checked
    with Google now. conn["uri_managed"] says which. Raises the HTTP error the
    launch endpoints return, in words the owner can act on."""
    from .google_uri_accounts import NoUriAccountAvailable, launch_connection

    brand_id = brand_ctx.get("brand_id")
    try:
        if await get_mode(db, brand_id) == "own":
            conn = await resolve_customer_id_for_launch(db, brand_ctx.get("user_id"), brand_id)
            conn = {**conn, "uri_managed": False}
        else:
            conn = await launch_connection(db, brand_id)
    except NoUriAccountAvailable:
        print(f"[google_flow] no free URI Google Ads account for brand {brand_id}", flush=True)
        raise HTTPException(status_code=409, detail=NOT_AVAILABLE)
    except AdsConnectionRequired as e:
        raise HTTPException(status_code=409, detail=connection_message(e.state.value))
    except GoogleAdsConnectionError as e:
        raise HTTPException(status_code=502, detail=friendly_error(e))
    try:
        adapter = GoogleAdsAdapter(
            db, customer_id=conn["customer_id"], login_customer_id=conn["login_customer_id"],
            access_token=conn["access_token"], currency_code=conn.get("currency_code", ""),
        )
    except GoogleAdsAPIError as e:
        raise HTTPException(status_code=502, detail=friendly_error(e))
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


def _markup() -> float:
    """Ad spend → the budget the owner typed (URI's fee comes out of it)."""
    from . import constants as C
    return C.AD_SPEND_MARKUP


def budget_too_low(budget_ngn: float, days: int) -> Optional[str]:
    """Why this budget can't work on Google Search, or None when it can. A
    search campaign needs enough clicks a day to learn what's working; below
    that the money is spent without a result. Said while planning, in naira,
    instead of failing at launch."""
    days = max(int(days or 1), 1)
    daily = float(budget_ngn or 0) / days
    if estimate_clicks_per_day(daily, _DEFAULT_ESTIMATED_CPC_NGN) >= _MIN_USEFUL_CLICKS_PER_DAY:
        return None
    # Told in the owner's own figures (their budget, URI's fee included), not ad spend.
    m = _markup()
    needed = _MIN_USEFUL_CLICKS_PER_DAY * _DEFAULT_ESTIMATED_CPC_NGN * days * m
    return (f"On Google Search, ₦{budget_ngn * m:,.0f} over {days} day{'s' if days != 1 else ''} is about "
            f"₦{daily * m:,.0f} a day — too little to get the ~{_MIN_USEFUL_CLICKS_PER_DAY:.0f} clicks a day a search ad "
            f"needs to work. Raise the budget to about ₦{needed:,.0f}, run it for fewer days, or let me use "
            f"Instagram and Facebook instead.")


# Google's reasons a campaign isn't (fully) serving, in words an owner can act on.
_SERVING_REASONS = {
    "HAS_ADS_DISAPPROVED": "Google rejected the ad. Open it in Google Ads to see which policy, then ask Jane for a new one.",
    "HAS_ADS_LIMITED_BY_POLICY": "Google is limiting where the ad shows because of one of its ad policies.",
    "MOST_ADS_UNDER_REVIEW": "Google is still reviewing the ad — usually within a day. It starts showing once approved.",
    "AD_GROUP_ADS_PAUSED": "The ad itself is paused. Press play again to switch everything on.",
    "AD_GROUPS_PAUSED": "The ad group is paused. Press play again to switch everything on.",
    "KEYWORDS_PAUSED": "Its keywords are paused in Google Ads.",
    "NO_KEYWORDS": "It has no keywords, so Google has nothing to show it for.",
    "NO_AD_GROUP_ADS": "It has no ad.",
    "BUDGET_CONSTRAINED": "The daily budget is small, so Google shows it less often than it could.",
    "BIDDING_STRATEGY_LEARNING": "Google is still learning who to show it to — normal for the first days.",
    "BIDDING_STRATEGY_LIMITED": "Google's bidding is limited — usually too little budget or data yet.",
    "BIDDING_STRATEGY_CONSTRAINED": "Google's bidding is limited by the budget.",
    "SEARCH_VOLUME_LIMITED": "Few people search for these keywords, so it shows rarely.",
    "CAMPAIGN_ENDED": "Its end date has passed.",
    "CAMPAIGN_PENDING": "Its start date hasn't arrived yet.",
}

_NO_BILLING = ("Your Google Ads account has no payment method, so Google won't show any ads. "
               "Add one in Google Ads → Billing → Payment methods.")


def serving_note(summary: dict) -> str:
    """One plain sentence on why a Google campaign isn't showing (or "" when
    nothing needs saying). Read from Google's own primary_status/reasons and
    the account's billing, never guessed."""
    if summary.get("delivery") != "Active":
        if summary.get("billing_ready") is False:
            return "Before you turn this on: " + _NO_BILLING[0].lower() + _NO_BILLING[1:]
        return ""
    if summary.get("billing_ready") is False:
        return _NO_BILLING
    status = summary.get("primary_status") or ""
    if status in ("", "ELIGIBLE", "UNKNOWN", "UNSPECIFIED"):
        return ""
    reasons = [r for r in summary.get("primary_status_reasons") or [] if r != "CAMPAIGN_PAUSED"]
    said = [_SERVING_REASONS[r] for r in reasons if r in _SERVING_REASONS]
    if said:
        return " ".join(said[:2])
    if status == "NOT_ELIGIBLE":
        return "Google says it isn't eligible to show yet. Open it in Google Ads to see why."
    if reasons:
        return "Google reports: " + ", ".join(r.replace("_", " ").lower() for r in reasons[:2]) + "."
    return ""


# Google's own per-day floors (it refuses to create below them). YouTube &
# Discover: US$5 a day, confirmed live — ₦8,000 covers it at our exchange rate.
_PLACEMENT_MIN_DAILY_NGN = {"demand_gen": 8_000.0}


def below_placement_minimum(placement: str, budget_ngn: float, days: int) -> Optional[str]:
    """Why Google won't accept this budget for this placement, or None.
    `budget_ngn` is the ad spend (URI's fee already out of it); the owner is
    told in the figures they typed — their whole budget, fee included — and
    only offered fixes that actually work."""
    floor = _PLACEMENT_MIN_DAILY_NGN.get(placement)
    if not floor:
        return None
    days = max(int(days or 1), 1)
    daily = float(budget_ngn or 0) / days
    if daily >= floor:
        return None
    from . import constants as C
    from .adapters.google_visual import PLACEMENT_LABELS
    label = PLACEMENT_LABELS[placement]
    markup = C.AD_SPEND_MARKUP
    stated = float(budget_ngn or 0) * markup
    floor_stated = floor * markup
    plural = lambda n: "" if n == 1 else "s"  # noqa: E731
    head = (f"{label} needs at least ₦{floor_stated:,.0f} a day — Google won't run it for less. "
            f"₦{stated:,.0f} over {days} day{plural(days)} is ₦{stated / days:,.0f} a day.")
    fits = int(float(budget_ngn or 0) // floor)
    if fits >= 1:
        return (f"{head} Raise the budget to ₦{floor_stated * days:,.0f} for {days} day{plural(days)}, "
                f"run it for {fits} day{plural(fits)} instead, or pick another Google placement.")
    return (f"{head} The smallest {label} campaign is ₦{floor_stated:,.0f} for one day. Raise the budget, "
            "or pick another Google placement — Display and Search have no minimum.")


def budget_too_low_warning(budget_ngn: float, days: int) -> str:
    """The same point as budget_too_low, for when the owner goes ahead anyway:
    what to expect, not a refusal."""
    days = max(int(days or 1), 1)
    daily = float(budget_ngn or 0) / days
    clicks = estimate_clicks_per_day(daily, _DEFAULT_ESTIMATED_CPC_NGN)
    m = _markup()
    needed = _MIN_USEFUL_CLICKS_PER_DAY * _DEFAULT_ESTIMATED_CPC_NGN * days * m
    return (f"About ₦{daily * m:,.0f} a day — expect only ~{max(clicks, 0):.0f} click{'s' if round(clicks) != 1 else ''} "
            f"a day, so treat this as a test. For real results on Search, about ₦{needed:,.0f} over "
            f"{days} day{'s' if days != 1 else ''}.")


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
        if record.get("detached_at"):
            # The brand let go of this Google Ads account; URI has no access to it any more.
            raise GoogleAdsAPIError(NO_LONGER_MANAGED)
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


# ── Letting go of an account that still has URI campaigns in it ──────────────

NO_LONGER_MANAGED = ("URI no longer has access to this campaign's Google Ads account, so it can't change it. "
                     "Manage it in Google Ads.")


async def campaigns_in_account(db, brand_id: Optional[str], customer_id: str) -> list[dict]:
    """URI campaigns of this brand that still exist in that Google Ads account,
    each with its live delivery (Active / Paused...). Asked before the brand
    lets go of the account: afterwards URI can't pause, track or bill them."""
    if not (brand_id and customer_id):
        return []
    records = await db[GOOGLE_CAMPAIGNS].find(
        {"brand_id": brand_id, "customer_id": customer_id, "platform": "google"}, {"_id": 0},
    ).to_list(length=200)
    dispatcher = GoogleCampaigns(db)
    out = []
    for r in records:
        if r.get("detached_at"):
            continue
        try:
            delivery = (await dispatcher.fetch_campaign_summary(r["campaign_id"]))["delivery"]
        except Exception as e:      # can't read it: count it, don't drop it
            print(f"[google_flow] could not read {r.get('campaign_id')}: {e}")
            delivery = "Unknown"
        if delivery == "Deleted":
            continue
        out.append({"campaign_id": r["campaign_id"], "name": r.get("display_name") or "Campaign",
                    "delivery": delivery})
    return out


async def pause_campaigns(db, campaigns: list[dict]) -> int:
    """Pause the ones still delivering (or unreadable). Raises GoogleAdsAPIError
    if one can't be paused — the account is then NOT let go of, so nothing is
    left running that URI can no longer reach."""
    dispatcher = GoogleCampaigns(db)
    paused = 0
    for c in campaigns:
        if c["delivery"] in ("Active", "Unknown"):
            await dispatcher.set_delivery(c["campaign_id"], False)
            paused += 1
    return paused


async def detach_campaigns(db, brand_id: Optional[str], customer_id: str) -> None:
    """Mark them as no longer managed by URI: they stay in the list (and the
    record keeps what was charged), but URI stops trying to reach them."""
    await db[GOOGLE_CAMPAIGNS].update_many(
        {"brand_id": brand_id, "customer_id": customer_id, "platform": "google"},
        {"$set": {"detached_at": datetime.now(timezone.utc)}},
    )

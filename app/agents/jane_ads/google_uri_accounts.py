"""
URI-owned Google Ads accounts (the "URI pays Google" model).

A brand never connects or pays Google. URI keeps its own Google Ads accounts
under its manager account (MCC), each paid for by URI. The first time a brand
runs a Google campaign it is given one of them, and every Google campaign of
that brand runs there. The brand pays only from its URI wallet; Google bills
URI.

An account joins the set when an admin adds it (POST /jane-ads/admin/google/
uri-accounts). It is checked first: it must sit under URI's manager account and
have a working payment method, otherwise ads there would never show. Google
lets URI's manager account create accounts on its own only after it has a spend
history; until then they're created once in Google Ads and added here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx

from app.core.config import settings

COLLECTION = "jane_ads_google_uri_accounts"


class UriAccountError(Exception):
    """An account can't be used (not under URI's manager account, no payment method...)."""


class NoUriAccountAvailable(Exception):
    """Every URI account is already given to a brand — an admin needs to add one."""


def _api_base() -> str:
    return f"https://googleads.googleapis.com/{settings.GOOGLE_ADS_API_VERSION}"


def _digits(customer_id: str) -> str:
    return "".join(ch for ch in str(customer_id or "") if ch.isdigit())


async def _admin_headers(db) -> dict:
    from .google_ads_connection import get_admin_valid_access_token

    return {
        "Authorization": f"Bearer {await get_admin_valid_access_token(db)}",
        "developer-token": settings.GOOGLE_ADS_DEVELOPER_TOKEN,
        "login-customer-id": _digits(settings.GOOGLE_ADS_MCC_CUSTOMER_ID),
    }


async def _search(db, customer_id: str, query: str) -> list[dict]:
    from .adapters.google import _raise_for_error

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{_api_base()}/customers/{customer_id}/googleAds:search",
                                 headers=await _admin_headers(db), json={"query": query})
    data = resp.json()
    _raise_for_error(data, "Google Ads lookup")
    return data.get("results", [])


async def inspect_account(db, customer_id: str) -> dict:
    """What Google says about this account, seen from URI's manager account:
    whether it is one of ours, its name and currency, and whether it can pay."""
    cid = _digits(customer_id)
    if len(cid) != 10:
        raise UriAccountError("A Google Ads customer ID has 10 digits, like 123-456-7890.")
    mcc = _digits(settings.GOOGLE_ADS_MCC_CUSTOMER_ID)
    rows = await _search(db, mcc, (
        "SELECT customer_client.id, customer_client.descriptive_name, customer_client.currency_code, "
        "customer_client.manager, customer_client.status FROM customer_client "
        f"WHERE customer_client.id = {cid}"))
    if not rows:
        raise UriAccountError("That account isn't under URI's Google Ads manager account.")
    client = rows[0].get("customerClient", {})
    if client.get("manager"):
        raise UriAccountError("That's a manager account. Add an ordinary Google Ads account.")
    if client.get("status") not in (None, "", "ENABLED"):
        raise UriAccountError(f"That account isn't active in Google Ads (status: {client.get('status')}).")
    try:
        billing = await _search(db, cid, "SELECT billing_setup.status FROM billing_setup")
        billing_ready = any(r.get("billingSetup", {}).get("status") in ("APPROVED", "APPROVED_HELD") for r in billing)
    except Exception as e:
        print(f"[google_uri_accounts] billing read failed for {cid}: {e}", flush=True)
        billing_ready = False
    return {"customer_id": cid, "name": client.get("descriptiveName") or "",
            "currency_code": client.get("currencyCode") or "", "billing_ready": billing_ready}


async def add_account(db, customer_id: str, added_by: str) -> dict:
    """Check an account and make it available to brands."""
    info = await inspect_account(db, customer_id)
    if not info["billing_ready"]:
        raise UriAccountError(
            "That account has no working payment method, so Google won't show ads there. "
            "Add URI's payment method in Google Ads → Billing, then add the account again.")
    if info["currency_code"] not in ("NGN", "USD"):
        raise UriAccountError(f"That account is in {info['currency_code']}. Use an NGN (or USD) account.")
    now = datetime.now(timezone.utc)
    await db[COLLECTION].update_one(
        {"customer_id": info["customer_id"]},
        {"$set": {**info, "checked_at": now, "added_by": added_by},
         "$setOnInsert": {"brand_id": None, "added_at": now}},
        upsert=True,
    )
    return await db[COLLECTION].find_one({"customer_id": info["customer_id"]}, {"_id": 0})


async def list_accounts(db) -> list[dict]:
    return await db[COLLECTION].find({}, {"_id": 0}).to_list(length=500)


async def brand_account(db, brand_id: Optional[str]) -> Optional[dict]:
    """The URI account this brand already runs in, if any."""
    if not brand_id:
        return None
    return await db[COLLECTION].find_one({"brand_id": brand_id}, {"_id": 0})


async def available(db, brand_id: Optional[str]) -> bool:
    """Can this brand run on Google through URI right now (no network call)?"""
    if await brand_account(db, brand_id):
        return True
    return bool(await db[COLLECTION].find_one({"brand_id": None, "billing_ready": True}))


async def account_for_brand(db, brand_id: str) -> dict:
    """The brand's URI account, giving it a free one the first time."""
    mine = await brand_account(db, brand_id)
    if mine:
        return mine
    taken = await db[COLLECTION].find_one_and_update(
        {"brand_id": None, "billing_ready": True},
        {"$set": {"brand_id": brand_id, "assigned_at": datetime.now(timezone.utc)}},
    )
    if not taken:
        raise NoUriAccountAvailable("No free URI Google Ads account.")
    return await brand_account(db, brand_id) or taken


async def launch_connection(db, brand_id: str) -> dict:
    """What the Google adapter needs to launch in this brand's URI account —
    the same shape resolve_customer_id_for_launch returns for an own account."""
    from .google_ads_connection import get_admin_valid_access_token
    from .whatsapp import get_brand_whatsapp

    account = await account_for_brand(db, brand_id)
    return {
        "customer_id": account["customer_id"],
        "login_customer_id": _digits(settings.GOOGLE_ADS_MCC_CUSTOMER_ID),
        "access_token": await get_admin_valid_access_token(db),
        "currency_code": account.get("currency_code", ""),
        "whatsapp_number": await get_brand_whatsapp(db, brand_id) or "",
        "uri_managed": True,
    }

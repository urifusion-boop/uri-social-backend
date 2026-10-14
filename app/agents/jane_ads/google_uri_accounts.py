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


# ── Creating accounts and paying for them automatically ─────────────────────

async def billing_options(db) -> list[dict]:
    """The payments accounts URI's manager account can attach to a new account
    by software. Google lists these only for monthly invoicing (consolidated
    billing); with card billing the list is empty and the card has to be added
    to each account by hand in Google Ads."""
    from .adapters.google import _raise_for_error

    mcc = _digits(settings.GOOGLE_ADS_MCC_CUSTOMER_ID)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{_api_base()}/customers/{mcc}/paymentsAccounts", headers=await _admin_headers(db))
    data = resp.json()
    _raise_for_error(data, "payments accounts lookup")
    return [
        {"resource_name": a.get("resourceName", ""), "name": a.get("name", ""),
         "currency_code": a.get("currencyCode", ""), "payments_account_id": a.get("paymentsAccountId", "")}
        for a in data.get("paymentsAccounts", [])
    ]


async def create_account(db, name: str, created_by: str) -> dict:
    """Create a blank NGN Google Ads account under URI's manager account and
    add it to URI's accounts. It can't show ads until it has a payment method:
    attached here when Google offers one (monthly invoicing), otherwise it waits
    for staff to add URI's card in Google Ads (billing_ready False until then)."""
    from .adapters.google import _raise_for_error
    from .google_ads_connection import _is_ineligible_mcc_error, set_mcc_creation_eligibility
    import json

    mcc = _digits(settings.GOOGLE_ADS_MCC_CUSTOMER_ID)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_api_base()}/customers/{mcc}:createCustomerClient",
            headers=await _admin_headers(db),
            json={"customerClient": {"descriptiveName": name, "currencyCode": "NGN", "timeZone": "Africa/Lagos"}},
        )
    data = resp.json()
    if "error" in data and _is_ineligible_mcc_error(json.dumps(data.get("error", {}))):
        await set_mcc_creation_eligibility(db, False, "Google won't let URI's manager account create accounts yet.")
        print(f"[google_uri_accounts] account creation refused: {data.get('error')}", flush=True)
        raise UriAccountError(
            f"(Google said: {((data.get('error') or {}).get('details') or [{}])[0].get('errors', [{}])[0].get('message') or (data.get('error') or {}).get('message')}) "
            "Google won't let URI's manager account create new accounts automatically yet — it needs more "
            "ad-spend history. Create the account in Google Ads (Accounts → + → Create new account) and add it here.")
    _raise_for_error(data, "account creation")
    await set_mcc_creation_eligibility(db, True)
    customer_id = (data.get("resourceName") or "").split("/")[-1]

    billing_attached = False
    try:
        options = [o for o in await billing_options(db) if o["currency_code"] in ("NGN", "")]
        if options:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{_api_base()}/customers/{customer_id}/billingSetups:mutate",
                    headers=await _admin_headers(db),
                    json={"operation": {"create": {"paymentsAccount": options[0]["resource_name"],
                                                   "startTimeType": "NOW"}}},
                )
            _raise_for_error(resp.json(), "billing setup")
            billing_attached = True
    except Exception as e:
        print(f"[google_uri_accounts] couldn't attach billing to {customer_id}: {e}", flush=True)

    now = datetime.now(timezone.utc)
    await db[COLLECTION].update_one(
        {"customer_id": customer_id},
        {"$set": {"customer_id": customer_id, "name": name, "currency_code": "NGN",
                  "billing_ready": billing_attached, "created_by_uri": True, "added_by": created_by,
                  "checked_at": now},
         "$setOnInsert": {"brand_id": None, "added_at": now}},
        upsert=True,
    )
    return {"customer_id": customer_id, "name": name, "billing_ready": billing_attached}

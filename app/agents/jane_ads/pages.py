"""
Jane + Ads — per-brand Facebook Page resolution for Click-to-WhatsApp routing.

A CTWA ad routes conversations to the WhatsApp number connected to the ad's Facebook
Page. For per-brand routing, that Page is the BRAND's own — connected via the existing
Facebook OAuth flow (/social-media/connect/facebook-direct), which stores the page +
token in `social_connections`. This module finds that page for the active brand and
(best-effort) shares it with URI's Business Manager so URI's ad account can advertise
with it. Reads only; the connect flow itself lives in the social-media manager.
"""
from __future__ import annotations

from typing import Optional

CONNECTIONS = "social_connections"


async def resolve_brand_facebook_page(db, user_id: Optional[str], brand_id: Optional[str]) -> Optional[dict]:
    """The brand's connected Facebook Page ({page_id, page_access_token, name}) or None
    if they haven't connected one yet. Scoped the same way the social manager scopes
    connections: an agency brand matches by brand_id; a personal brand by user_id."""
    if db is None:
        return None
    from app.services.BrandAccountService import BrandAccount

    is_personal = (not brand_id) or (user_id and brand_id == BrandAccount.personal_brand_id(user_id))
    scope = {"user_id": user_id} if is_personal else {"brand_id": brand_id}
    query = {"platform": "facebook", "page_id": {"$exists": True, "$ne": None}, **scope}
    doc = await db[CONNECTIONS].find_one(query, sort=[("connected_at", -1)])
    if not doc or not doc.get("page_id"):
        return None
    return {
        "page_id": doc["page_id"],
        "page_access_token": doc.get("page_access_token", ""),
        "name": doc.get("account_name", ""),
    }


async def ensure_page_shared_with_business_manager(page: dict) -> None:
    """Best-effort: grant URI's Business Manager ADVERTISE access to the brand's page, so
    URI's ad account can run ads with it. Idempotent on Meta's side; never raises (a
    failure surfaces later as a clear Meta permissions error at ad creation, and many
    pages are already shared)."""
    token = page.get("page_access_token")
    page_id = page.get("page_id")
    if not (token and page_id):
        return
    try:
        from app.services.MetaAdsService import share_page_with_business_manager, BusinessManagerNotConfigured
        try:
            await share_page_with_business_manager(page_id, token)
        except BusinessManagerNotConfigured:
            pass  # BM id not set — the ad may still work if the page is already shared
    except Exception as e:
        print(f"[jane-ads] page BM-share skipped: {e}", flush=True)

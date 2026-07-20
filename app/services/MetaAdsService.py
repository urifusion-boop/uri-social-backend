"""
Meta Ads — Business Manager page-sharing.

The "hybrid page grant" from the engineering work split: grant URI's Business
Manager advertising access to a client's Page WITHOUT transferring ownership.

The correct call is POST /{page_id}/agencies with permitted_tasks=["ADVERTISE"] —
this shares the Page with a Business Manager for a specific task only. The Graph API
also has POST /{business_id}/owned_pages, which CLAIMS a Page for the business
(ownership transfer) — that endpoint must never be used here.
Ref: https://developers.facebook.com/docs/graph-api/reference/page/agencies
"""
from __future__ import annotations

import httpx

from app.core.config import settings


class BusinessManagerNotConfigured(Exception):
    """META_BUSINESS_MANAGER_ID isn't set yet. The OAuth connect flow still runs
    and stores the page's access token; this step just hasn't been reached."""


async def share_page_with_business_manager(page_id: str, page_access_token: str) -> dict:
    """Request ADVERTISE-only access to `page_id` for URI's Business Manager.
    Requires a Page access token from a user with MANAGE on that Page (exactly
    what the OAuth connect flow's /me/accounts call returns)."""
    if not settings.META_BUSINESS_MANAGER_ID:
        raise BusinessManagerNotConfigured(
            "META_BUSINESS_MANAGER_ID is not set — cannot request advertising "
            "access to URI's Business Manager yet."
        )

    graph_base = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{graph_base}/{page_id}/agencies",
            params={
                "business": settings.META_BUSINESS_MANAGER_ID,
                "permitted_tasks": "ADVERTISE",
                "access_token": page_access_token,
            },
        )
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Business Manager page-share failed: {data['error'].get('message')}")
        return data

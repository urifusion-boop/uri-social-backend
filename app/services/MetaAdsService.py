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

import json

import httpx

from app.core.config import settings


class SystemUserNotConfigured(Exception):
    """META_ADS_SYSTEM_USER_ID isn't set. The Page is shared with the business, but
    the system user that creates ads still has no access to it."""


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


async def assign_page_to_system_user(page_id: str, page_access_token: str) -> dict:
    """Give the ads system user ADVERTISE access to `page_id`.

    Sharing the Page with the Business Manager (share_page_with_business_manager
    above) is necessary but not sufficient: a system user does NOT inherit Page
    access from the business it belongs to. Without this second step, ad-creative
    creation fails with

        (#10) To create posts for Page <id>, contact an admin to get permission
        for Advertiser role or higher

    — live-confirmed 2026-08-31 on a client Page that the business already held
    PROFILE_PLUS_ADVERTISE on. The alternative is an admin assigning every client
    Page by hand in Business Settings, which does not scale past a handful.

    Two things this call is fussy about, both found the hard way:

    · It must use URI's OWN system-user token, not the client's page token. The
      client's token cannot resolve a user scoped to URI's app, and rejects both
      forms of the id in a loop ("pass an app-scoped ID" / "user is not business
      scoped"). `page_access_token` is accepted for signature symmetry with
      share_page_with_business_manager and is deliberately unused.
    · `user` must be the APP-SCOPED id (GET /me with the system user's token), not
      the business-scoped id Business Settings displays.
    """
    if not settings.META_ADS_SYSTEM_USER_ID:
        raise SystemUserNotConfigured(
            "META_ADS_SYSTEM_USER_ID is not set — the client's Page was shared with "
            "the business, but the system user cannot advertise on it yet."
        )
    if not settings.META_ADS_ACCESS_TOKEN:
        raise SystemUserNotConfigured(
            "META_ADS_ACCESS_TOKEN is not set — no system-user token to assign with."
        )

    graph_base = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{graph_base}/{page_id}/assigned_users",
            params={
                "user": settings.META_ADS_SYSTEM_USER_ID,
                "tasks": json.dumps(["ADVERTISE"]),
                "business": settings.META_BUSINESS_MANAGER_ID,
                "access_token": settings.META_ADS_ACCESS_TOKEN,
            },
        )
        data = resp.json()
        if "error" in data:
            raise RuntimeError(
                f"System-user page assignment failed: {data['error'].get('message')}"
            )
        return data

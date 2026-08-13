"""
WhatsApp Business Platform — Embedded Signup (Jane on WhatsApp client onboarding).

Embedded Signup is Meta's self-serve flow for a client to grant our Business
Manager access to their own WhatsApp Business Account (WABA), automating what we
did by hand for URI's own rehearsal number: sharing a WABA as an asset to our
System User. Because Meta authorizes WhatsApp Graph API calls by asset-grant (which
WABAs a System User has been given access to) rather than by per-client token
issuance, no per-client access token is stored anywhere — every onboarded client's
messages are sent using the same WHATSAPP_SYSTEM_USER_ACCESS_TOKEN.

Two calls make up the server-side half of the flow, both required before a
connection can be trusted as working:
1. exchange_embedded_signup_code — completes the code exchange Meta's Embedded
   Signup SDK hands back after the client grants access.
2. subscribe_app_to_waba — subscribes our app to receive webhook traffic for the
   client's WABA. Without this succeeding, a connection would look "active" while
   never actually receiving a single inbound message.
"""
from __future__ import annotations

import httpx

from app.core.config import settings

GRAPH_BASE = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"


class SubscribedAppsFailed(Exception):
    """POST /{waba_id}/subscribed_apps did not succeed. The caller must not mark
    the connection active — surface this as a retryable state instead."""


async def exchange_embedded_signup_code(code: str) -> dict:
    """Complete the code exchange Meta's Embedded Signup SDK hands back after the
    client grants access. Unlike the redirect-based OAuth connectors elsewhere in
    this codebase, Embedded Signup's exchange takes no redirect_uri.

    NOTE: confirm the exact request/response shape against Meta's live Embedded
    Signup docs before relying on this in production — this call has drifted
    across Graph API versions and this implementation has not yet been exercised
    against a real Embedded Signup flow."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/oauth/access_token",
            params={
                "client_id": settings.META_APP_ID,
                "client_secret": settings.META_APP_SECRET,
                "code": code,
            },
        )
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Embedded Signup code exchange failed: {data['error'].get('message')}")
        return data


async def subscribe_app_to_waba(waba_id: str) -> dict:
    """Subscribe our app to receive webhook traffic (messages, statuses) for a
    client's WABA. Mandatory after every successful Embedded Signup — a WABA
    shared as an asset but never subscribed sends us nothing."""
    if not settings.WHATSAPP_SYSTEM_USER_ACCESS_TOKEN:
        raise SubscribedAppsFailed(
            "WHATSAPP_SYSTEM_USER_ACCESS_TOKEN is not set — cannot subscribe to "
            "this WABA's webhooks."
        )

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/{waba_id}/subscribed_apps",
            params={"access_token": settings.WHATSAPP_SYSTEM_USER_ACCESS_TOKEN},
        )
        data = resp.json()
        if "error" in data or not data.get("success"):
            raise SubscribedAppsFailed(
                f"subscribed_apps failed for waba_id={waba_id}: {data.get('error', {}).get('message', data)}"
            )
        return data


async def unsubscribe_app_from_waba(waba_id: str) -> dict:
    """Best-effort — called on disconnect so a removed client stops generating
    webhook traffic we no longer have a connection record for."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.delete(
            f"{GRAPH_BASE}/{waba_id}/subscribed_apps",
            params={"access_token": settings.WHATSAPP_SYSTEM_USER_ACCESS_TOKEN},
        )
        return resp.json()

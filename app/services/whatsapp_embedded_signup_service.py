"""
WhatsApp Business Platform — Embedded Signup (Jane on WhatsApp client onboarding).

Embedded Signup is Meta's self-serve flow for a client to grant our app access to
their own WhatsApp Business Account (WABA). CORRECTION to an earlier assumption in
this module: confirmed live in Meta's App Dashboard that the only "full access"
Embedded Signup configuration template available issues a per-client, 60-DAY-EXPIRY
access token — NOT a permanent asset-grant to our own System User the way URI's own
manually-configured rehearsal number works. So unlike WHATSAPP_SYSTEM_USER_ACCESS_TOKEN
(URI's own number, set up by hand, unaffected by any of this), every client onboarded
through Embedded Signup needs their OWN access_token stored and kept alive — see
refresh_whatsapp_token and run_whatsapp_token_refresh below.

Three calls make up the server-side half of the flow:
1. exchange_embedded_signup_code — completes the code exchange Meta's Embedded
   Signup SDK hands back after the client grants access, returning the client's
   own access_token (to be stored) and its expiry.
2. subscribe_app_to_waba — subscribes our app to receive webhook traffic for the
   client's WABA, using THAT client's own token (the one just exchanged) — not the
   shared System User token, since that token doesn't have standing access to a
   WABA we were only just granted a 60-day-scoped token for. Without this
   succeeding, a connection would look "active" while never actually receiving a
   single inbound message.
3. refresh_whatsapp_token — exchanges a still-valid client token for a fresh one
   before it expires, the standard pattern for Meta long-lived tokens
   (grant_type=fb_exchange_token) — mirrors google_ads_connection.py's
   refresh_access_token/get_valid_access_token shape in this same codebase.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import settings

GRAPH_BASE = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"

# Mirrors google_ads_connection.py's refresh-before-expiry margin, scaled up for a
# token that lives 60 days rather than ~1 hour — the daily cron job (not a per-
# request check) needs a wide enough window that a token never actually expires
# between two consecutive daily runs even if a run is briefly delayed.
TOKEN_REFRESH_MARGIN_SECONDS = 7 * 24 * 60 * 60  # 7 days


class SubscribedAppsFailed(Exception):
    """POST /{waba_id}/subscribed_apps did not succeed. The caller must not mark
    the connection active — surface this as a retryable state instead."""


class WhatsAppTokenRefreshFailed(Exception):
    """The client's token could not be refreshed (revoked, truly expired, etc.) —
    the connection needs the client to redo Embedded Signup, not a silent retry."""


async def exchange_embedded_signup_code(code: str) -> dict:
    """Complete the code exchange Meta's Embedded Signup SDK hands back after the
    client grants access. Unlike the redirect-based OAuth connectors elsewhere in
    this codebase, Embedded Signup's exchange takes no redirect_uri.

    Returns Meta's raw response dict — callers should read data["access_token"]
    and data.get("expires_in") (seconds; Meta may omit this for a non-expiring
    token, but the confirmed "60-day expiry" template should always include it).

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


async def subscribe_app_to_waba(waba_id: str, access_token: str) -> dict:
    """Subscribe our app to receive webhook traffic (messages, statuses) for a
    client's WABA, using that client's own just-exchanged access_token — not the
    shared System User token (see module docstring for why). Mandatory after every
    successful Embedded Signup — a WABA shared as an asset but never subscribed
    sends us nothing."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/{waba_id}/subscribed_apps",
            params={"access_token": access_token},
        )
        data = resp.json()
        if "error" in data or not data.get("success"):
            raise SubscribedAppsFailed(
                f"subscribed_apps failed for waba_id={waba_id}: {data.get('error', {}).get('message', data)}"
            )
        return data


async def unsubscribe_app_from_waba(waba_id: str, access_token: str) -> dict:
    """Best-effort — called on disconnect so a removed client stops generating
    webhook traffic we no longer have a connection record for. Uses the client's
    stored token (same reasoning as subscribe_app_to_waba)."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.delete(
            f"{GRAPH_BASE}/{waba_id}/subscribed_apps",
            params={"access_token": access_token},
        )
        return resp.json()


async def refresh_whatsapp_token(access_token: str) -> dict:
    """Exchanges a still-valid (not yet expired) client token for a fresh one with
    a new ~60-day expiry — the documented Meta pattern for renewing a long-lived
    token without the user redoing the consent flow. Raises
    WhatsAppTokenRefreshFailed if the token has already died (revoked, truly
    expired) rather than merely aging — that case needs reconnection, not retry.

    NOTE: same live-verification caveat as exchange_embedded_signup_code — this
    has not yet been exercised against a real 60-day client token."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{GRAPH_BASE}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": settings.META_APP_ID,
                "client_secret": settings.META_APP_SECRET,
                "fb_exchange_token": access_token,
            },
        )
        data = resp.json()
        if "error" in data or "access_token" not in data:
            raise WhatsAppTokenRefreshFailed(
                f"token refresh failed: {data.get('error', {}).get('message', data)}"
            )
        return data


async def run_whatsapp_token_refresh(db) -> dict:
    """Daily proactive check, same shape as jane_ads/ads_connection.py's
    run_token_health_check: refresh every active whatsapp_business connection's
    token before it dies, and — only on genuine refresh failure — mark the
    connection as needing reconnection and notify the owner once (dedup flag,
    not on every run). A dead WhatsApp token means we simply cannot send on that
    client's behalf at all, so there is no in-band (WhatsApp) way to tell them;
    this is the only notification path."""
    from app.services.NotificationService import notification_service

    checked = refreshed = needs_reconnect = notified = 0
    now = datetime.now(timezone.utc)
    connections = await db["social_connections"].find(
        {"platform": "whatsapp_business", "connection_status": "active"}
    ).to_list(length=500)

    for conn in connections:
        access_token = conn.get("access_token")
        expires_at = conn.get("token_expires_at")
        if not access_token or not isinstance(expires_at, datetime):
            continue
        checked += 1
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        remaining = (expires_at - now).total_seconds()
        if remaining > TOKEN_REFRESH_MARGIN_SECONDS:
            continue  # not close enough to expiry yet

        try:
            data = await refresh_whatsapp_token(access_token)
            new_expires_at = now + timedelta(seconds=int(data.get("expires_in", 60 * 24 * 60 * 60)))
            await db["social_connections"].update_one(
                {"id": conn["id"]},
                {"$set": {
                    "access_token": data["access_token"],
                    "token_expires_at": new_expires_at,
                    "updated_at": now.isoformat(),
                }},
            )
            refreshed += 1
        except WhatsAppTokenRefreshFailed as e:
            print(f"[WhatsAppTokenRefresh] refresh failed for connection={conn.get('id')}: {e}", flush=True)
            await db["social_connections"].update_one(
                {"id": conn["id"]},
                {"$set": {"connection_status": "needs_reconnect", "updated_at": now.isoformat()}},
            )
            needs_reconnect += 1

            if conn.get("token_refresh_failed_notified"):
                continue
            user_id = conn.get("user_id")
            if not user_id:
                continue
            display_number = conn.get("display_phone_number") or "your WhatsApp number"
            await notification_service._log_notification(
                user_id=user_id,
                notification_type="whatsapp_connection_expired",
                channel="email",
                subject="Your WhatsApp connection needs reconnecting",
                status="sent",
                metadata={
                    "brand_id": conn.get("brand_id"),
                    "message": (
                        f"Jane can no longer send messages on {display_number} — the connection's "
                        "access expired and couldn't be automatically renewed. Reconnect it in "
                        "Settings to resume."
                    ),
                },
            )
            await db["social_connections"].update_one(
                {"id": conn["id"]}, {"$set": {"token_refresh_failed_notified": True}},
            )
            notified += 1

    return {"checked": checked, "refreshed": refreshed, "needs_reconnect": needs_reconnect, "notified": notified}

"""
Google Analytics 4 — server-side event tracking via Measurement Protocol.

Events are fired fire-and-forget so they never block API responses.
If GA4 credentials are not configured, all calls are silent no-ops.
"""

import asyncio
import uuid
import httpx

from app.core.config import settings

_GA4_ENDPOINT = "https://www.google-analytics.com/mp/collect"


async def _send(client_id: str, user_id: str | None, events: list[dict]) -> None:
    if not settings.GA4_API_SECRET or not settings.GA4_MEASUREMENT_ID:
        return
    try:
        payload: dict = {
            "client_id": client_id,
            "events": events,
        }
        if user_id:
            payload["user_id"] = user_id

        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(
                _GA4_ENDPOINT,
                params={
                    "measurement_id": settings.GA4_MEASUREMENT_ID,
                    "api_secret": settings.GA4_API_SECRET,
                },
                json=payload,
            )
    except Exception as e:
        print(f"[GA4] event send failed: {e}")


def _fire(client_id: str, user_id: str | None, events: list[dict]) -> None:
    """Schedule a GA4 send without blocking the caller."""
    asyncio.ensure_future(_send(client_id, user_id, events))


# ── Public helpers ────────────────────────────────────────────────────────────


def track_signup(user_id: str, method: str = "email") -> None:
    """Fire a GA4 sign_up event."""
    _fire(
        client_id=user_id,
        user_id=user_id,
        events=[{"name": "sign_up", "params": {"method": method}}],
    )


def track_login(user_id: str, method: str = "email") -> None:
    """Fire a GA4 login event."""
    _fire(
        client_id=user_id,
        user_id=user_id,
        events=[{"name": "login", "params": {"method": method}}],
    )


def track_event(user_id: str, event_name: str, params: dict | None = None) -> None:
    """Fire an arbitrary GA4 event."""
    _fire(
        client_id=user_id,
        user_id=user_id,
        events=[{"name": event_name, "params": params or {}}],
    )

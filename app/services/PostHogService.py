"""
PostHog server-side event tracking.

All calls are fire-and-forget and never block API responses.
If POSTHOG_API_KEY is not set, all calls are silent no-ops.
"""

from __future__ import annotations

import posthog as _ph

from app.core.config import settings

_client: _ph.Client | None = None


def _get_client() -> _ph.Client | None:
    global _client
    if not settings.POSTHOG_API_KEY:
        return None
    if _client is None:
        _client = _ph.Client(
            project_api_key=settings.POSTHOG_API_KEY,
            host=settings.POSTHOG_HOST,
            sync_mode=False,
        )
    return _client


# ── Public helpers ────────────────────────────────────────────────────────────


def track_signup(user_id: str, email: str = "", method: str = "email") -> None:
    c = _get_client()
    if not c:
        return
    c.capture(
        distinct_id=user_id,
        event="user signed up",
        properties={"method": method, "email": email},
    )
    # Build a person profile in PostHog
    c.set(
        distinct_id=user_id,
        properties={"email": email, "signup_method": method},
    )


def track_login(user_id: str, email: str = "", method: str = "email") -> None:
    c = _get_client()
    if not c:
        return
    c.capture(
        distinct_id=user_id,
        event="user logged in",
        properties={"method": method, "email": email},
    )


def track_event(user_id: str, event_name: str, properties: dict | None = None) -> None:
    c = _get_client()
    if not c:
        return
    c.capture(
        distinct_id=user_id,
        event=event_name,
        properties=properties or {},
    )


def shutdown() -> None:
    """Flush remaining events — call on app shutdown."""
    if _client:
        _client.shutdown()

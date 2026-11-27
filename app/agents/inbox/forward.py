import os
from typing import Optional

import httpx

FORWARD_HEADER = "x-uri-forwarded"
PROD_WEBHOOK_URL = "https://api.urisocial.com/inbox/webhooks/meta"
TIMEOUT_SECONDS = 6.0
# Workspaces staging answers itself even when production has the same account connected,
# so the Uri test workspace can exercise code that has not been released.
DEFAULT_STAGING_WORKSPACES = ("brnd_personal_1f38e754-f879-4c57-8faa-9d1da592fee1",)


def staging_workspaces() -> set[str]:
    raw = os.getenv("META_STAGING_WORKSPACES", "")
    listed = [w.strip() for w in raw.split(",") if w.strip()]
    return set(listed or DEFAULT_STAGING_WORKSPACES)


async def claimed_by_staging(events: list, lookup) -> bool:
    """True when any event is for an account that belongs to a staging-owned workspace."""
    owned = staging_workspaces()
    for ev in events:
        account = await lookup(ev.get("external_account_id"))
        if account and str(account.get("workspace_id")) in owned:
            return True
    return False


def forward_url() -> str:
    """Meta has one webhook address per app, and it points at staging. Staging passes
    every delivery on to production, so an account connected in production works without
    anyone re-pointing Meta. Only the development environment forwards."""
    explicit = os.getenv("META_WEBHOOK_FORWARD_URL", "").strip()
    if explicit:
        return explicit
    return PROD_WEBHOOK_URL if os.getenv("APP_ENV", "").lower() == "development" else ""


def owned_by_forward_target(result: Optional[dict]) -> bool:
    """True when the other environment has a connected account for something in the delivery."""
    if not isinstance(result, dict):
        return False
    matched = result.get("matched")
    if matched is None:
        matched = result.get("stored", 0)
    return bool(matched)


async def forward_delivery(raw_body: bytes, signature: str) -> Optional[dict]:
    url = forward_url()
    if not url:
        return None
    headers = {"content-type": "application/json", "x-hub-signature-256": signature, FORWARD_HEADER: "1"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(url, content=raw_body, headers=headers)
        return response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None

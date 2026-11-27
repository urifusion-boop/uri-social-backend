import os
from typing import Optional

import httpx

FORWARD_HEADER = "x-uri-forwarded"
PROD_WEBHOOK_URL = "https://api.urisocial.com/inbox/webhooks/meta"
STAGING_WEBHOOK_URL = "https://api-social-dev.urisocial.com/inbox/webhooks/meta"
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


def environment() -> str:
    return os.getenv("APP_ENV", "").lower()


def forward_url(claimed: bool = False) -> str:
    """Where a delivery goes next, or "" to handle it here.

    Meta has one webhook address per app. Production is the front door: it handles every
    delivery itself except those for a staging-owned workspace, which it passes to staging
    so unreleased code can be tested on real accounts. If Meta still points at staging,
    staging does the reverse and passes everything else to production.
    """
    explicit = os.getenv("META_WEBHOOK_FORWARD_URL", "").strip()
    if explicit:
        return explicit
    if environment() == "production":
        return STAGING_WEBHOOK_URL if claimed else ""
    if environment() == "development":
        return "" if claimed else PROD_WEBHOOK_URL
    return ""


def handled_remotely(remote: Optional[dict]) -> bool:
    """Production only forwards what staging owns, so any answer means staging took it.
    Staging forwards speculatively, so it needs production to say it had a connected account."""
    if environment() == "production":
        return remote is not None
    return owned_by_forward_target(remote)


def owned_by_forward_target(result: Optional[dict]) -> bool:
    """True when the other environment has a connected account for something in the delivery."""
    if not isinstance(result, dict):
        return False
    matched = result.get("matched")
    if matched is None:
        matched = result.get("stored", 0)
    return bool(matched)


async def forward_delivery(url: str, raw_body: bytes, signature: str) -> Optional[dict]:
    headers = {"content-type": "application/json", "x-hub-signature-256": signature, FORWARD_HEADER: "1"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            response = await client.post(url, content=raw_body, headers=headers)
        return response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None

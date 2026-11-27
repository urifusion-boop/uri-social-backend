import os
from typing import Optional

import httpx

FORWARD_HEADER = "x-uri-forwarded"
PROD_WEBHOOK_URL = "https://api.urisocial.com/inbox/webhooks/meta"
TIMEOUT_SECONDS = 6.0


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

"""
Unified Inbox — the one place that talks to Meta when replying.

Kept deliberately thin: it turns a conversation into a Graph call and returns the
provider's message id. Every decision about WHETHER to send lives in send.py, so this
can be swapped for a fake in tests without losing any of that logic.
"""
from __future__ import annotations

import httpx

from app.core.config import settings

from .entities import Kind, Platform
from .send import ProviderTimeout

TIMEOUT = httpx.Timeout(15.0)


def _graph_base() -> str:
    version = getattr(settings, "FACEBOOK_API_VERSION", "") or "v21.0"
    return f"https://graph.facebook.com/{version}"


async def meta_transport(conv: dict, account: dict, text: str) -> str:
    """Send one reply. Returns the provider's message id.

    Raises ProviderTimeout when Meta never answered — that is NOT the same as a
    rejection, and send.py records it differently for exactly that reason.
    """
    token = account.get("access_token") or ""
    if not token:
        raise Exception("no access token for this channel account")

    base = _graph_base()

    if conv.get("kind") == Kind.COMMENT.value:
        target = conv.get("_reply_to") or ""
        if not target:
            raise Exception("no comment to reply to")
        # Instagram replies live under /replies; a Page comment is a child comment.
        path = "replies" if conv.get("platform") == Platform.INSTAGRAM.value else "comments"
        url = f"{base}/{target}/{path}"
        payload = {"message": text, "access_token": token}
    else:
        page_id = account.get("external_account_id") or ""
        recipient = conv.get("external_thread_id") or ""
        if not (page_id and recipient):
            raise Exception("missing page id or recipient for a direct message")
        url = f"{base}/{page_id}/messages"
        payload = {
            "recipient": {"id": recipient},
            "message": {"text": text},
            # RESPONSE marks this as a reply inside the 24-hour window, which is the
            # only kind of free-form send we make.
            "messaging_type": "RESPONSE",
            "access_token": token,
        }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(url, json=payload)
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise ProviderTimeout(str(e)) from e

    if r.status_code >= 400:
        detail = ""
        try:
            detail = (r.json().get("error") or {}).get("message", "")
        except ValueError:
            detail = r.text[:200]
        raise Exception(f"Meta rejected the reply: {detail or r.status_code}")

    body = r.json()
    return body.get("message_id") or body.get("id") or ""

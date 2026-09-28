"""
Unified Inbox — which accounts this workspace actually owns.

Two jobs, and they use DIFFERENT ids, which is the whole reason this file exists:

· **Routing an incoming webhook.** Meta keys `entry.id` on the Instagram professional
  account for Instagram, and on the Page for Messenger and Page comments. Look up the
  wrong one and a real customer message is dropped as "unconnected account".

· **Sending a reply.** Both platforms send through the PAGE with the page token. An
  Instagram reply posted to the Instagram user id fails.

So a channel account records both, and never conflates them. Tokens are reused from
the connection the user already made for publishing — a second OAuth prompt for the
same Page would be asking the user to grant what they have already granted.
"""
from __future__ import annotations

from typing import Any, Optional

from .entities import CHANNEL_ACCOUNTS, Platform, now

SOCIAL_CONNECTIONS = "social_connections"


def _workspace_of(conn: dict, user_id: str) -> str:
    """A personal workspace has no brand_id; the user id stands in for it."""
    return conn.get("brand_id") or user_id


def channel_rows(conn: dict, user_id: str) -> list[dict]:
    """Turn one social connection into the channel accounts it provides.

    An Instagram connection provides TWO: the Instagram account (DMs and comments
    arrive under its id) and the Page behind it (Messenger and Page comments arrive
    under the Page's). Registering only one silently loses half the inbox.
    """
    token = conn.get("page_access_token") or ""
    page_id = str(conn.get("page_id") or "")
    ig_user_id = str(conn.get("ig_user_id") or "")
    workspace_id = _workspace_of(conn, user_id)
    if not token:
        return []

    rows: list[dict] = []
    if ig_user_id:
        rows.append({
            "workspace_id": workspace_id,
            "platform": Platform.INSTAGRAM.value,
            "external_account_id": ig_user_id,   # what webhooks key on
            "page_id": page_id,                  # what replies are sent through
            "access_token": token,
            "name": conn.get("username") or conn.get("account_name") or "",
        })
    if page_id:
        rows.append({
            "workspace_id": workspace_id,
            "platform": Platform.FACEBOOK.value,
            "external_account_id": page_id,
            "page_id": page_id,
            "access_token": token,
            "name": conn.get("page_name") or conn.get("account_name") or "",
        })
    return rows


async def link_workspace_channels(db, user_id: str, workspace_id: str) -> list[dict]:
    """Register this workspace's connected accounts with the inbox. Safe to re-run.

    Re-running is the normal case, not an edge one: tokens are refreshed and Pages are
    reconnected, and the inbox must pick up the new token without creating a second
    row that webhooks might match instead.
    """
    q: dict[str, Any] = {"connection_status": "active",
                         "platform": {"$in": ["instagram", "facebook"]}}
    conns = await db[SOCIAL_CONNECTIONS].find(q).to_list(200)

    linked: list[dict] = []
    for conn in conns:
        if _workspace_of(conn, conn.get("user_id", "")) != workspace_id:
            continue
        for row in channel_rows(conn, user_id):
            key = {"workspace_id": row["workspace_id"],
                   "platform": row["platform"],
                   "external_account_id": row["external_account_id"]}
            await db[CHANNEL_ACCOUNTS].update_one(
                key,
                {"$set": {**row, "updated_at": now()},
                 "$setOnInsert": {"connected_at": now()}},
                upsert=True,
            )
            linked.append(key)
    return linked


async def account_for_event(db, external_account_id: str) -> Optional[dict]:
    """The channel account a webhook belongs to, or None if nobody connected it."""
    if not external_account_id:
        return None
    return await db[CHANNEL_ACCOUNTS].find_one(
        {"external_account_id": str(external_account_id)})

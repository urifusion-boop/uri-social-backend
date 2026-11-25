"""The channel account a conversation should be answered from."""
from __future__ import annotations

from typing import Optional

from bson import ObjectId

from .entities import CHANNEL_ACCOUNTS, CONVERSATIONS


async def account_for(db, conv: Optional[dict]) -> Optional[dict]:
    """The account on the thread, or — when it was reconnected since the thread began, so
    its old record is gone — the workspace's current account on that platform, which the
    thread is then moved to. A customer who writes again after months still gets answered."""
    if not conv:
        return None
    if ObjectId.is_valid(conv.get("channel_account_id", "")):
        found = await db[CHANNEL_ACCOUNTS].find_one({"_id": ObjectId(conv["channel_account_id"])})
        if found:
            return found
    if conv.get("platform") not in ("instagram", "facebook"):
        return None
    found = await db[CHANNEL_ACCOUNTS].find_one(
        {"workspace_id": conv.get("workspace_id"), "platform": conv.get("platform")},
        sort=[("connected_at", -1)])
    if found:
        await db[CONVERSATIONS].update_one({"_id": conv["_id"]}, {"$set": {"channel_account_id": str(found["_id"])}})
    return found

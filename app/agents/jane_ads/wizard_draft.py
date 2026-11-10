"""
Jane + Ads — autosave for an in-progress Campaign Builder wizard run.

The wizard (uri-social-frontend's campaign-wizard/*) collects objective,
platform, audience, budget and creative choices across several steps before a
plan is ever built — none of that belongs to planFromMessage's own shape, and
none of it needs interpreting here. This is a one-slot-per-brand save/fetch/
clear for an opaque JSON blob, so an unfinished run survives a cleared browser
or a different device rather than only the one browser's own localStorage (see
the frontend's campaign-wizard/wizardDraft.ts, which already covers the common
case of just navigating away — this is what backs it up further).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

DRAFTS = "jane_ads_wizard_drafts"


async def load(db, brand_id: str) -> Optional[dict]:
    return await db[DRAFTS].find_one({"brand_id": brand_id}, {"_id": 0})


async def save(db, brand_id: str, payload: dict) -> None:
    await db[DRAFTS].update_one(
        {"brand_id": brand_id},
        {"$set": {"payload": payload, "brand_id": brand_id, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


async def clear(db, brand_id: str) -> None:
    await db[DRAFTS].delete_one({"brand_id": brand_id})

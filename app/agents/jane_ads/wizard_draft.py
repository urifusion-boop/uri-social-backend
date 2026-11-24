"""
Jane + Ads — autosave for in-progress Campaign Builder wizard runs.

The wizard (uri-social-frontend's campaign-wizard/*) collects objective,
platform, audience, budget and creative choices across several steps, then
(once far enough) a generated plan/creative for one or more platforms —
none of that belongs to planFromMessage's own shape, and none of it needs
interpreting here. This is a save/fetch/clear per (brand, draft) pair for an
opaque JSON blob, so an unfinished run survives a cleared browser or a
different device rather than only the one browser's own localStorage (see
the frontend's campaign-wizard/wizardDraft.ts, which already covers the
common case of just navigating away — this is what backs it up further).

Live-reported 2026-11: this used to be one slot per brand, which silently
discarded an earlier unfinished run the moment a second one was started —
"why am I only seeing one, I left more than one". Multiple drafts per brand
now coexist, each addressed by its own client-generated draft_id.
"""
from __future__ import annotations

from datetime import datetime, timezone

DRAFTS = "jane_ads_wizard_drafts"


async def list_drafts(db, brand_id: str) -> list[dict]:
    cursor = db[DRAFTS].find({"brand_id": brand_id}, {"_id": 0}).sort("updated_at", -1)
    return [doc async for doc in cursor]


class DraftOwnedElsewhere(Exception):
    """The draft_id is already saved under a different brand."""


async def save_draft(db, brand_id: str, draft_id: str, payload: dict, user_id: str | None = None) -> None:
    """Upsert one draft for this brand.

    Refuses (DraftOwnedElsewhere) when the same draft_id already belongs to a
    different brand. draft_ids are random and made by the browser, so the only
    way one turns up under a second brand is a browser that still held another
    account's unfinished ad. Live-reported 2026-11: ads started on one account
    appeared in another account signed in on the same browser, and editing one
    copied it into the second account. The browser now keeps its copies per user
    and wipes them on sign-out; this makes the database refuse the copy even if a
    browser ever gets that wrong.
    """
    elsewhere = await db[DRAFTS].find_one(
        {"draft_id": draft_id, "brand_id": {"$ne": brand_id}}, {"_id": 1}
    )
    if elsewhere:
        raise DraftOwnedElsewhere(draft_id)
    fields = {
        "payload": payload,
        "brand_id": brand_id,
        "draft_id": draft_id,
        "updated_at": datetime.now(timezone.utc),
    }
    if user_id:
        fields["user_id"] = user_id   # who last saved it
    await db[DRAFTS].update_one(
        {"brand_id": brand_id, "draft_id": draft_id},
        {"$set": fields},
        upsert=True,
    )


async def delete_draft(db, brand_id: str, draft_id: str) -> None:
    await db[DRAFTS].delete_one({"brand_id": brand_id, "draft_id": draft_id})

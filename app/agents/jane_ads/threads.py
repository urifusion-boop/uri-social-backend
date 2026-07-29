"""
Jane + Ads — campaign threads (Tier E).

Each campaign conversation is its own thread: a brand no longer has one endless chat
but a browsable, resumable list of campaigns ("Ax leak fix", "Mama Kitchen launch"),
each reopenable, and a launched one duplicable into a fresh draft to tweak and relaunch.

A thread is a lightweight record in `jane_ads_threads`; the actual messages live in the
existing `jane_ads_chat_messages` collection, now tagged with `thread_id`. The list view
reads thread records (title/status/preview); opening one reads its tagged messages.

The pure helpers (id shape, preview trimming, title derivation) are unit-tested; the
Mongo read/writes are thin wrappers.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

THREADS_COLLECTION = "jane_ads_threads"
CHAT_COLLECTION = "jane_ads_chat_messages"

_PREVIEW_MAX = 80
_TITLE_MAX = 48


def new_thread_id() -> str:
    return f"thr_{uuid.uuid4().hex[:16]}"


def _preview(text: str) -> str:
    text = " ".join((text or "").split())
    return text[:_PREVIEW_MAX]


def title_from_message(text: str) -> str:
    """A short human title for a thread, taken from the first user message — so the rail
    reads 'Ad for leaking taps' rather than an opaque id."""
    text = " ".join((text or "").split())
    if not text:
        return "New campaign"
    return text[:_TITLE_MAX]


async def create_thread(db, brand_id: str, user_id: Optional[str], title: str = "New campaign") -> dict:
    now = datetime.now(timezone.utc)
    doc = {
        "thread_id": new_thread_id(),
        "brand_id": brand_id,
        "user_id": user_id,
        "title": title or "New campaign",
        "status": "draft",          # draft | planned | launched
        "preview": "",
        "created_at": now,
        "updated_at": now,
    }
    await db[THREADS_COLLECTION].insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


async def list_threads(db, brand_id: str) -> list[dict]:
    """This brand's threads, most-recently-active first."""
    if not brand_id or db is None:
        return []
    cur = (db[THREADS_COLLECTION]
           .find({"brand_id": brand_id}, {"_id": 0, "user_id": 0})
           .sort("updated_at", -1).limit(200))
    return await cur.to_list(length=200)


async def get_thread(db, brand_id: str, thread_id: str) -> Optional[dict]:
    if not (brand_id and thread_id) or db is None:
        return None
    return await db[THREADS_COLLECTION].find_one(
        {"brand_id": brand_id, "thread_id": thread_id}, {"_id": 0})


async def touch_thread(db, brand_id: str, thread_id: str, *,
                       title: Optional[str] = None, status: Optional[str] = None,
                       preview_text: Optional[str] = None) -> None:
    """Update a thread's activity — its status (as the campaign progresses), last-message
    preview, and updated_at (so it floats to the top of the list). Upserts, so a message
    saved against a thread that has no record yet (e.g. the very first turn) still creates
    one. Only ever sets a title when the thread doesn't already have a real one."""
    if not (brand_id and thread_id) or db is None:
        return
    now = datetime.now(timezone.utc)
    sets: dict = {"updated_at": now, "brand_id": brand_id, "thread_id": thread_id}
    if status:
        sets["status"] = status
    if preview_text is not None:
        sets["preview"] = _preview(preview_text)
    on_insert: dict = {"created_at": now, "title": title or "New campaign", "status": status or "draft"}
    if status:
        on_insert.pop("status", None)   # avoid conflict: status is in $set
    update: dict = {"$set": sets, "$setOnInsert": on_insert}
    # Set the title only if provided AND the doc has no meaningful title yet — handled by
    # a separate conditional update so we never clobber a good title with a later message.
    await db[THREADS_COLLECTION].update_one(
        {"brand_id": brand_id, "thread_id": thread_id}, update, upsert=True)
    if title:
        await db[THREADS_COLLECTION].update_one(
            {"brand_id": brand_id, "thread_id": thread_id,
             "title": {"$in": [None, "", "New campaign"]}},
            {"$set": {"title": title[:_TITLE_MAX]}})


async def delete_thread(db, brand_id: str, thread_id: str) -> bool:
    """Remove a thread and its messages from the rail. Never touches
    `jane_ads_meta_campaigns` — a launched campaign keeps running and stays in
    'My Campaigns' regardless of whether its originating conversation is deleted;
    this is purely tidying up the chat history list. Returns False if the thread
    didn't belong to this brand (nothing deleted)."""
    if not (brand_id and thread_id) or db is None:
        return False
    result = await db[THREADS_COLLECTION].delete_one({"brand_id": brand_id, "thread_id": thread_id})
    await db[CHAT_COLLECTION].delete_many({"brand_id": brand_id, "thread_id": thread_id})
    return result.deleted_count > 0


async def thread_history(db, brand_id: str, thread_id: str) -> list[dict]:
    """The messages tagged to one thread, oldest first."""
    if not (brand_id and thread_id) or db is None:
        return []
    cur = (db[CHAT_COLLECTION]
           .find({"brand_id": brand_id, "thread_id": thread_id},
                 {"_id": 0, "brand_id": 0, "user_id": 0})
           .sort("created_at", 1).limit(500))
    return await cur.to_list(length=500)


def seed_message_from_campaign(camp: dict) -> str:
    """Rebuild a plain-English brief from a launched campaign's stored fields, so a
    duplicated thread starts pre-filled and the user only tweaks what they want. Mirrors
    how a user would have typed the original ask."""
    parts = []
    name = camp.get("name") or camp.get("business_name")
    if name:
        parts.append(f"Run another ad for {name}")
    if camp.get("goal"):
        parts.append(f"goal {camp['goal']}")
    if camp.get("budget_ngn"):
        parts.append(f"budget ₦{float(camp['budget_ngn']):,.0f}")
    if camp.get("city"):
        parts.append(f"in {camp['city']}")
    return ", ".join(parts) if parts else "Run another ad like my last one"

"""
The inbox's working desk — what the team does to a conversation, and the things they
reuse across conversations.

Until now "Mark resolved", a lead and every tag lived only in the browser: they were
gone on the next page load, and nobody else on the team ever saw them. Saved replies and
tag presets were the same. Everything here is stored per workspace (the active brand)
and read back by the inbox, so the whole team sees one truth.

- PATCH  /inbox/conversations/{id}/desk   status, assignee, star, tags, lead
- GET    /inbox/conversations/{id}/desk   the above plus the notes
- POST   /inbox/conversations/{id}/notes  add an internal note (never sent to the customer)
- DELETE /inbox/conversations/{id}/notes/{note_id}
- GET/PUT /inbox/desk/prefs               quick replies, their categories, tag presets
- GET    /inbox/audience                  everyone the business has talked to
- GET    /inbox/team                      who a conversation can be assigned to
"""
from __future__ import annotations

import re
import uuid
from datetime import timedelta, timezone
from typing import Any, Optional

from bson import ObjectId
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.dependencies import get_active_brand_context, get_db_dependency

from .entities import AUDIT, CONVERSATIONS, IDENTITIES, Status, now

router = APIRouter(prefix="/inbox", tags=["Unified Inbox — desk"])

PREFS = "inbox_desk_prefs"

LEAD_STAGES = ("", "new", "qualified", "won", "lost")
MAX_TAGS = 20
MAX_TAG_LEN = 40
MAX_NOTE_LEN = 2000
MAX_NOTES = 200
MAX_REPLIES = 200
MAX_REPLY_LEN = 2000
MAX_BUTTONS = 3
# A button under a quick reply is a link to the business's own page (booking, store,
# website) — DMs carry text, so it goes out as a tap-able "Label: address" line under the
# reply. The kind only picks the icon the team sees.
BUTTON_KINDS = ("link", "book", "buy", "learn_more")
DEFAULT_TAG_PRESETS = ["VIP", "Repeat customer", "Refund", "Spam review", "Urgent"]
DEFAULT_CATEGORIES = ["Greetings", "Pricing", "Delivery", "Ads & promos", "Support"]


def _workspace(brand_ctx: dict) -> str:
    ws = brand_ctx.get("brand_id")
    if not ws:
        raise HTTPException(status_code=400, detail="No active brand.")
    return ws


async def _conversation(db, workspace_id: str, conversation_id: str) -> dict:
    if not ObjectId.is_valid(conversation_id):
        raise HTTPException(status_code=404, detail="Conversation not found.")
    conv = await db[CONVERSATIONS].find_one({"_id": ObjectId(conversation_id), "workspace_id": workspace_id})
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return conv


def _clean_tags(tags: Any) -> list[str]:
    if not isinstance(tags, list):
        raise HTTPException(status_code=400, detail="Tags must be a list.")
    out: list[str] = []
    for t in tags:
        t = str(t or "").strip()[:MAX_TAG_LEN]
        if t and t.lower() not in {x.lower() for x in out}:
            out.append(t)
    return out[:MAX_TAGS]


def desk_view(conv: dict) -> dict:
    """The desk fields of a conversation, as the inbox reads them."""
    notes = conv.get("notes") or []
    return {
        "status": conv.get("status") or Status.OPEN.value,
        "assignee_id": conv.get("assignee_id", ""),
        "starred": bool(conv.get("starred")),
        "tags": conv.get("tags") or [],
        "lead_stage": conv.get("lead_stage", ""),
        "lead_value": conv.get("lead_value"),
        "lead_interest": conv.get("lead_interest", ""),
        "notes_count": len(notes),
    }


async def _audit(db, workspace_id: str, actor_id: str, action: str, conversation_id: str, detail: dict) -> None:
    await db[AUDIT].insert_one({
        "workspace_id": workspace_id, "actor_id": actor_id, "action": action,
        "conversation_id": conversation_id, "detail": detail, "at": now(),
    })


@router.patch("/conversations/{conversation_id}/desk")
async def update_desk(
    conversation_id: str,
    body: dict = Body(...),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Change any of: status, assignee_id, starred, tags, lead_stage, lead_value,
    lead_interest. Only the keys sent are changed."""
    ws = _workspace(brand_ctx)
    conv = await _conversation(db, ws, conversation_id)
    changes: dict[str, Any] = {}
    if "status" in body:
        status = str(body["status"] or "")
        if status not in {s.value for s in Status}:
            raise HTTPException(status_code=400, detail="Unknown status.")
        changes["status"] = status
        if status == Status.RESOLVED.value:
            changes["resolved_at"] = now()
    if "assignee_id" in body:
        assignee = str(body["assignee_id"] or "")
        if assignee and assignee not in {m["user_id"] for m in await _team(db, brand_ctx)}:
            raise HTTPException(status_code=400, detail="That person isn't on this brand's team.")
        changes["assignee_id"] = assignee
    if "starred" in body:
        changes["starred"] = bool(body["starred"])
    if "tags" in body:
        changes["tags"] = _clean_tags(body["tags"])
    if "lead_stage" in body:
        stage = str(body["lead_stage"] or "")
        if stage not in LEAD_STAGES:
            raise HTTPException(status_code=400, detail="Unknown lead stage.")
        changes["lead_stage"] = stage
    if "lead_value" in body:
        value = body["lead_value"]
        if value in (None, ""):
            changes["lead_value"] = None
        else:
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Lead value must be a number.")
            if value < 0:
                raise HTTPException(status_code=400, detail="Lead value can't be negative.")
            changes["lead_value"] = value
    if "lead_interest" in body:
        changes["lead_interest"] = str(body["lead_interest"] or "").strip()[:200]
    if not changes:
        return desk_view(conv)
    await db[CONVERSATIONS].update_one({"_id": conv["_id"]}, {"$set": changes})
    await _audit(db, ws, brand_ctx.get("user_id", ""), "inbox.desk", conversation_id,
                 {k: v for k, v in changes.items() if k != "resolved_at"})
    return desk_view({**conv, **changes})


@router.get("/conversations/{conversation_id}/desk")
async def get_desk(
    conversation_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    ws = _workspace(brand_ctx)
    conv = await _conversation(db, ws, conversation_id)
    notes = sorted(conv.get("notes") or [], key=lambda n: n.get("at") or now(), reverse=True)
    return {**desk_view(conv), "notes": notes}


@router.post("/conversations/{conversation_id}/notes")
async def add_note(
    conversation_id: str,
    text: str = Body(..., embed=True),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """An internal note on the conversation — for the team only, never sent."""
    ws = _workspace(brand_ctx)
    conv = await _conversation(db, ws, conversation_id)
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="A note can't be empty.")
    if len(conv.get("notes") or []) >= MAX_NOTES:
        raise HTTPException(status_code=400, detail="This conversation has too many notes.")
    user_id = brand_ctx.get("user_id", "")
    note = {"id": uuid.uuid4().hex, "text": text[:MAX_NOTE_LEN], "author_id": user_id,
            "author_name": await _name_of(db, user_id), "at": now()}
    await db[CONVERSATIONS].update_one({"_id": conv["_id"]}, {"$push": {"notes": note}})
    await _audit(db, ws, user_id, "inbox.note", conversation_id, {"note_id": note["id"]})
    return note


@router.delete("/conversations/{conversation_id}/notes/{note_id}")
async def delete_note(
    conversation_id: str,
    note_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    ws = _workspace(brand_ctx)
    conv = await _conversation(db, ws, conversation_id)
    res = await db[CONVERSATIONS].update_one({"_id": conv["_id"]}, {"$pull": {"notes": {"id": note_id}}})
    if not res.modified_count:
        raise HTTPException(status_code=404, detail="Note not found.")
    await _audit(db, ws, brand_ctx.get("user_id", ""), "inbox.note_deleted", conversation_id, {"note_id": note_id})
    return {"deleted": True}


# ── Quick replies, categories, tag presets ───────────────────────────────────

_SHORTCUT = re.compile(r"^/[a-z0-9][a-z0-9_-]{0,23}$")


def _clean_replies(replies: Any, categories: list[str]) -> list[dict]:
    if not isinstance(replies, list):
        raise HTTPException(status_code=400, detail="Quick replies must be a list.")
    out, shortcuts = [], set()
    for r in replies[:MAX_REPLIES]:
        if not isinstance(r, dict):
            continue
        title = str(r.get("title") or "").strip()[:60]
        text = str(r.get("text") or "").strip()[:MAX_REPLY_LEN]
        if not title or not text:
            raise HTTPException(status_code=400, detail="Every quick reply needs a title and a message.")
        shortcut = str(r.get("shortcut") or "").strip().lower()
        if shortcut:
            if not shortcut.startswith("/"):
                shortcut = "/" + shortcut
            if not _SHORTCUT.match(shortcut):
                raise HTTPException(status_code=400, detail=f"Shortcut {shortcut!r}: use / then letters or numbers.")
            if shortcut in shortcuts:
                raise HTTPException(status_code=400, detail=f"Two quick replies use the shortcut {shortcut}.")
            shortcuts.add(shortcut)
        category = str(r.get("category") or "").strip()
        if category and category not in categories:
            category = ""
        buttons = []
        for b in (r.get("buttons") or [])[:MAX_BUTTONS]:
            label = str((b or {}).get("label") or "").strip()[:30]
            kind = str((b or {}).get("kind") or "link")
            url = str((b or {}).get("url") or "").strip()
            if not label or kind not in BUTTON_KINDS:
                continue
            if not re.match(r"^https?://\S+$", url):
                raise HTTPException(status_code=400, detail=f"The button {label!r} needs a full web address.")
            buttons.append({"label": label, "kind": kind, "url": url})
        out.append({"id": str(r.get("id") or uuid.uuid4().hex), "title": title, "text": text,
                    "shortcut": shortcut, "category": category, "buttons": buttons,
                    "uses": int(r.get("uses") or 0)})
    return out


async def _prefs(db, ws: str) -> dict:
    doc = await db[PREFS].find_one({"workspace_id": ws}) or {}
    return {
        "quick_replies": doc.get("quick_replies") or [],
        "categories": doc.get("categories") or list(DEFAULT_CATEGORIES),
        "tag_presets": doc.get("tag_presets") if doc.get("tag_presets") is not None else list(DEFAULT_TAG_PRESETS),
    }


@router.get("/desk/prefs")
async def get_prefs(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    return await _prefs(db, _workspace(brand_ctx))


@router.put("/desk/prefs")
async def put_prefs(
    body: dict = Body(...),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Replace any of quick_replies, categories, tag_presets (keys not sent are kept)."""
    ws = _workspace(brand_ctx)
    current = await _prefs(db, ws)
    changes: dict[str, Any] = {}
    if "categories" in body:
        if not isinstance(body["categories"], list):
            raise HTTPException(status_code=400, detail="Categories must be a list.")
        cats: list[str] = []
        for c in body["categories"][:30]:
            c = str(c or "").strip()[:30]
            if c and c not in cats:
                cats.append(c)
        changes["categories"] = cats
    categories = changes.get("categories", current["categories"])
    if "quick_replies" in body:
        changes["quick_replies"] = _clean_replies(body["quick_replies"], categories)
    elif "categories" in changes:
        # A removed category leaves its replies uncategorised, not deleted.
        changes["quick_replies"] = [{**r, "category": r["category"] if r.get("category") in categories else ""}
                                    for r in current["quick_replies"]]
    if "tag_presets" in body:
        changes["tag_presets"] = _clean_tags(body["tag_presets"])
    if changes:
        await db[PREFS].update_one({"workspace_id": ws},
                                   {"$set": {**changes, "workspace_id": ws, "updated_at": now()}}, upsert=True)
    return {**current, **changes}


@router.post("/desk/prefs/quick-replies/{reply_id}/used")
async def quick_reply_used(
    reply_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Counts a use, so the most-used replies can be shown first."""
    ws = _workspace(brand_ctx)
    await db[PREFS].update_one({"workspace_id": ws, "quick_replies.id": reply_id},
                               {"$inc": {"quick_replies.$.uses": 1}})
    return {"ok": True}


# ── Audience and team ────────────────────────────────────────────────────────

SEGMENTS = ("all", "new", "leads", "customers", "ads", "follow_up")


def _aware(dt):
    """Mongo hands dates back without a timezone; they were stored as UTC."""
    if dt is not None and getattr(dt, "tzinfo", None) is None and hasattr(dt, "replace"):
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _stage(conv: dict) -> str:
    stage = conv.get("lead_stage") or ""
    if stage == "won":
        return "customer"
    if stage in ("new", "qualified"):
        return "lead"
    return "contact"


@router.get("/audience")
async def audience(
    segment: str = Query("all"),
    q: str = Query(""),
    channel: str = Query(""),
    limit: int = Query(200, le=500),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Everyone the business has talked to — one row per person per platform — with
    where they came from, their lead stage and tags, built from the inbox itself."""
    ws = _workspace(brand_ctx)
    if segment not in SEGMENTS:
        raise HTTPException(status_code=400, detail="Unknown segment.")
    query: dict = {"workspace_id": ws}
    if channel:
        query["platform"] = channel
    convs = await db[CONVERSATIONS].find(query).sort("last_activity_at", -1).to_list(2000)

    people: dict[str, dict] = {}
    for c in convs:
        key = c.get("contact_identity_id") or f"conv:{c['_id']}"
        p = people.get(key)
        if not p:
            p = people[key] = {
                "contact_id": key, "platform": c.get("platform"), "name": "", "avatar_url": "",
                "conversations": 0, "conversation_ids": [], "last_activity_at": _aware(c.get("last_activity_at")),
                "first_seen_at": _aware(c.get("last_activity_at")), "tags": [], "stage": "contact",
                "lead_value": None, "from_ad": False, "source": "", "status": c.get("status") or "open",
                "needs_follow_up": False,
            }
        p["conversations"] += 1
        p["conversation_ids"].append(str(c["_id"]))
        for t in c.get("tags") or []:
            if t not in p["tags"]:
                p["tags"].append(t)
        stage = _stage(c)
        if stage == "customer" or (stage == "lead" and p["stage"] == "contact"):
            p["stage"] = stage
        if c.get("lead_value") is not None:
            p["lead_value"] = max(p["lead_value"] or 0, c["lead_value"])
        if c.get("source_ad_id"):
            p["from_ad"] = True
            p["source"] = "ad"
        elif not p["source"]:
            p["source"] = f"{c.get('platform')}_{c.get('kind') or 'dm'}"
        if c.get("needs_human") and c.get("status") != Status.RESOLVED.value:
            p["needs_follow_up"] = True
        last = first = _aware(c.get("last_activity_at"))
        if last and (not p["last_activity_at"] or last > p["last_activity_at"]):
            p["last_activity_at"] = last
        if first and (not p["first_seen_at"] or first < p["first_seen_at"]):
            p["first_seen_at"] = first

    ids = [ObjectId(k) for k in people if ObjectId.is_valid(k)]
    if ids:
        async for ident in db[IDENTITIES].find({"_id": {"$in": ids}}):
            p = people.get(str(ident["_id"]))
            if p:
                p["name"] = ident.get("display_name") or ""
                p["avatar_url"] = ident.get("avatar_url") or ""
                if ident.get("first_seen_at"):
                    p["first_seen_at"] = _aware(ident["first_seen_at"])

    rows = list(people.values())
    counts = {
        "all": len(rows),
        "new": sum(1 for p in rows if p["first_seen_at"] and p["first_seen_at"] >= now() - timedelta(days=7)),
        "leads": sum(1 for p in rows if p["stage"] == "lead"),
        "customers": sum(1 for p in rows if p["stage"] == "customer"),
        "ads": sum(1 for p in rows if p["from_ad"]),
        "follow_up": sum(1 for p in rows if p["needs_follow_up"]),
    }
    channels: dict[str, int] = {}
    for p in rows:
        channels[p["platform"]] = channels.get(p["platform"], 0) + 1
    pick = {
        "all": lambda p: True,
        "new": lambda p: bool(p["first_seen_at"] and p["first_seen_at"] >= now() - timedelta(days=7)),
        "leads": lambda p: p["stage"] == "lead",
        "customers": lambda p: p["stage"] == "customer",
        "ads": lambda p: p["from_ad"],
        "follow_up": lambda p: p["needs_follow_up"],
    }[segment]
    needle = q.strip().lower()
    rows = [p for p in rows if pick(p) and (not needle or needle in (p["name"] or "").lower()
                                            or any(needle in t.lower() for t in p["tags"]))]
    return {"people": rows[:limit], "counts": counts, "channels": channels}


async def _name_of(db, user_id: str) -> str:
    if not user_id:
        return ""
    u = await db["users"].find_one({"userId": user_id}, {"fullName": 1, "firstName": 1, "lastName": 1,
                                                        "name": 1, "email": 1}) or {}
    full = u.get("fullName") or u.get("name") or " ".join(x for x in (u.get("firstName"), u.get("lastName")) if x)
    return (full or u.get("email") or "").strip()


async def _team(db, brand_ctx: dict) -> list[dict]:
    """The signed-in user plus the active members of this brand's workspace."""
    me = brand_ctx.get("user_id", "")
    ids: list[str] = [me] if me else []
    ws = brand_ctx.get("brand_id")
    if ws:
        async for m in db["workspace_members"].find({"workspace_id": ws, "status": "active"}, {"user_id": 1}):
            if m.get("user_id") and m["user_id"] not in ids:
                ids.append(m["user_id"])
    return [{"user_id": uid, "name": await _name_of(db, uid) or "Team member", "is_me": uid == me} for uid in ids]


@router.get("/team")
async def team(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    return {"members": await _team(db, brand_ctx)}

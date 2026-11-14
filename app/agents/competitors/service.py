"""
A brand's competitors: discovered (suggested until the owner keeps them), added by hand, or
removed with a reason. Kept ones are written into the Brand Playbook's competitor list, which
content rules and market listening already read, so every module sees the same competitors.

Discovery runs as a background job and is cached per industry, products and area for a week,
so many similar brands cost one search. Removed competitors are never suggested again.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.agents.brand_intel.html_read import read_page
from app.agents.brand_intel.safe_fetch import FetchError, fetch, normalize_url
from app.jobs import queue
from app.jobs.worker import JobError

from . import discovery as D

COLLECTION = "brand_competitors"
CACHE = "competitor_discovery_cache"
JOB_TYPE = "competitor_discovery"
CACHE_TTL_SECONDS = 7 * 24 * 3600
DISCOVERIES_PER_BRAND_PER_DAY = 5
SUGGESTED, ACTIVE, REMOVED = "suggested", "active", "removed"
REMOVE_REASONS = {
    "not_a_competitor": "Not a competitor",
    "different_customers": "Different customers",
    "different_area": "Different area",
    "closed": "Closed down",
    "other": "Something else",
}


async def ensure_indexes(db) -> None:
    await db[COLLECTION].create_index([("brand_id", 1), ("status", 1)], name="brand_status")
    await db[COLLECTION].create_index([("brand_id", 1), ("key", 1)], unique=True, name="brand_key_unique")
    await db[CACHE].create_index("key", unique=True, name="key_unique")
    await db[CACHE].create_index("at", expireAfterSeconds=CACHE_TTL_SECONDS, name="at_ttl")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _public(doc: dict) -> dict:
    return {**D.summary(doc), "id": doc["competitor_id"], "status": doc["status"], "source": doc.get("source", ""),
            "removed_reason": doc.get("removed_reason", "")}


async def _profile(db, user_id: str, brand_id: str) -> dict:
    from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService
    res = await BrandProfileService.get(user_id, db, brand_id=brand_id)
    return (res.get("responseData") or {}) if isinstance(res, dict) and res.get("status") else {}


# ── listing ────────────────────────────────────────────────────────────────────

async def list_for(db, brand_id: str) -> dict:
    docs = await db[COLLECTION].find({"brand_id": brand_id}, {"_id": 0}).sort("created_at", 1).to_list(length=200)
    by = {SUGGESTED: [], ACTIVE: [], REMOVED: []}
    for d in docs:
        by.setdefault(d["status"], []).append(_public(d))
    for k in (SUGGESTED, ACTIVE):
        by[k].sort(key=lambda c: 0 if c["kind"] == "direct" else 1)
    job = await db[queue.COLLECTION].find_one({"brand_id": brand_id, "type": JOB_TYPE}, {"_id": 0},
                                             sort=[("created_at", -1)])
    return {"suggested": by[SUGGESTED], "active": by[ACTIVE], "removed_count": len(by[REMOVED]),
            "discovery": queue.public_view(job) if job else None,
            "reasons": [{"value": k, "label": v} for k, v in REMOVE_REASONS.items()]}


# ── discovery ──────────────────────────────────────────────────────────────────

async def start_discovery(db, *, brand_id: str, user_id: str, force: bool = False) -> dict:
    since = _now() - timedelta(days=1)
    recent = await db[queue.COLLECTION].count_documents({"brand_id": brand_id, "type": JOB_TYPE,
                                                         "created_at": {"$gte": since}})
    if recent >= DISCOVERIES_PER_BRAND_PER_DAY:
        raise ValueError("Jane has looked for competitors a lot today. Try again tomorrow.")
    return await queue.enqueue(db, JOB_TYPE, {"force": force}, brand_id=brand_id, user_id=user_id,
                               key=brand_id, max_attempts=2)


async def discovery_job(db, job: dict, ctx) -> dict:
    brand_id, user_id = job["brand_id"], job["user_id"]
    profile = await _profile(db, user_id, brand_id)
    brief = D.brief_from_profile(profile)
    if not D.enough_to_search(brief):
        raise JobError("Jane needs to know what you sell and where before she can find competitors. "
                       "Add your products and the areas you serve to your Brand Playbook, then try again.",
                       code="playbook_thin")

    existing = await db[COLLECTION].find({"brand_id": brand_id}, {"_id": 0, "key": 1, "name": 1}).to_list(length=500)
    known_keys = {e["key"] for e in existing}
    exclude = [e["name"] for e in existing]

    key = D.cache_key(brief)
    cached = None if job["payload"].get("force") else await db[CACHE].find_one({"key": key}, {"_id": 0})
    if cached:
        await ctx.step("search", "Looked for businesses like yours recently", "done")
        verified = cached["candidates"]
    else:
        await ctx.step("search", "Searching the web for businesses like yours")
        try:
            candidates = await D.search(brief, exclude)
        except D.DiscoveryUnavailable as e:
            print(f"[competitors] search failed for {brand_id}: {e}", flush=True)
            raise JobError("Jane couldn't search the web just now. Try again in a few minutes.",
                           code="search_unavailable", retry=True)
        await ctx.step("search", "Searching the web for businesses like yours", "done",
                       f"{len(candidates)} possible matches")
        await ctx.step("check", "Checking each one is a real, open business")
        verified = await D.verify(candidates, own_site=brief["website"], own_name=brief["name"])
        await ctx.step("check", "Checking each one is a real, open business", "done",
                       f"{len(verified)} of {len(candidates)} checked out")
        await db[CACHE].update_one({"key": key}, {"$set": {"key": key, "candidates": verified, "at": _now()}},
                                   upsert=True)

    fresh = [c for c in verified if D.candidate_key(c) not in known_keys
             and c["name"].lower() not in {n.lower() for n in exclude}]
    now = _now()
    for c in fresh:
        await db[COLLECTION].update_one(
            {"brand_id": brand_id, "key": D.candidate_key(c)},
            {"$setOnInsert": {**D.summary(c), "competitor_id": f"cmp_{uuid.uuid4().hex[:16]}", "brand_id": brand_id,
                              "key": D.candidate_key(c), "status": SUGGESTED, "source": "discovered",
                              "created_at": now, "updated_at": now}},
            upsert=True)
    await ctx.found([c["name"] for c in fresh])
    if fresh:
        await _tell_owner(db, user_id, brand_id, len(fresh))
    return {"suggested": len(fresh), "checked": len(verified)}


async def _tell_owner(db, user_id: str, brand_id: str, n: int) -> None:
    try:
        from app.services import brand_notifier
        await brand_notifier.notify_in_app(
            db, user_id, brand_id, "competitors_found",
            title=f"Jane found {n} business{'es' if n != 1 else ''} like yours",
            message="Check them: keep the real competitors, and tell Jane which ones aren't.",
            link="/workspace/?tab=campaign-competitors")
    except Exception as e:
        print(f"[competitors] notification failed: {e!r}", flush=True)


# ── the owner's decisions ──────────────────────────────────────────────────────

async def _get(db, brand_id: str, competitor_id: str) -> dict:
    doc = await db[COLLECTION].find_one({"brand_id": brand_id, "competitor_id": competitor_id}, {"_id": 0})
    if not doc:
        raise LookupError("That competitor wasn't found.")
    return doc


async def _sync_playbook(db, user_id: str, brand_id: str, add: Optional[str] = None, remove: Optional[str] = None) -> None:
    """Keep the Playbook's competitor list in step with what the owner kept or removed. Entries
    the owner typed into the Playbook themselves are never touched."""
    from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService
    profile = await _profile(db, user_id, brand_id)
    handles = [h for h in (profile.get("competitor_handles") or []) if h]
    lower = [h.lower() for h in handles]
    if add and add.lower() not in lower:
        handles.append(add)
    if remove and remove.lower() in lower:
        handles = [h for h in handles if h.lower() != remove.lower()]
    if handles != [h for h in (profile.get("competitor_handles") or []) if h]:
        await BrandProfileService.save(user_id, {"competitor_handles": handles}, db, brand_id=brand_id)


async def keep(db, *, brand_id: str, user_id: str, competitor_id: str) -> dict:
    doc = await _get(db, brand_id, competitor_id)
    await db[COLLECTION].update_one({"brand_id": brand_id, "competitor_id": competitor_id},
                                    {"$set": {"status": ACTIVE, "decided_at": _now(), "decided_by": user_id,
                                              "updated_at": _now()}})
    await _sync_playbook(db, user_id, brand_id, add=D.handle_for_playbook(doc))
    return _public({**doc, "status": ACTIVE})


async def remove(db, *, brand_id: str, user_id: str, competitor_id: str, reason: str) -> dict:
    if reason not in REMOVE_REASONS:
        reason = "other"
    doc = await _get(db, brand_id, competitor_id)
    await db[COLLECTION].update_one({"brand_id": brand_id, "competitor_id": competitor_id},
                                    {"$set": {"status": REMOVED, "removed_reason": reason, "decided_at": _now(),
                                              "updated_at": _now()}})
    if doc["status"] == ACTIVE:
        await _sync_playbook(db, user_id, brand_id, remove=D.handle_for_playbook(doc))
    return _public({**doc, "status": REMOVED, "removed_reason": reason})


async def set_kind(db, *, brand_id: str, competitor_id: str, kind: str) -> dict:
    kind = "direct" if kind == "direct" else "similar"
    doc = await _get(db, brand_id, competitor_id)
    await db[COLLECTION].update_one({"brand_id": brand_id, "competitor_id": competitor_id},
                                    {"$set": {"kind": kind, "updated_at": _now()}})
    return _public({**doc, "kind": kind})


async def add(db, *, brand_id: str, user_id: str, name: str, website: str = "", instagram: str = "") -> dict:
    """A competitor the owner names. Their website, if given, fills in a description."""
    name = " ".join((name or "").split())[:80]
    if not name:
        raise ValueError("Add the business's name.")
    c: dict[str, Any] = {"name": name, "website": "", "instagram": D._clean_handle(instagram), "kind": "direct",
                         "location": "", "what_they_sell": "", "why": "", "source_url": "", "description": ""}
    if website.strip():
        try:
            page = await fetch(normalize_url(website), max_bytes=400_000)
            read = read_page(page.text, page.url)
            c["website"], c["description"] = page.url, read.description[:240]
            if not c["instagram"] and read.socials.get("instagram"):
                c["instagram"] = D._clean_handle(read.socials["instagram"])
        except FetchError:
            c["website"] = normalize_url(website) if website.strip() else ""
    key = D.candidate_key(c)
    now = _now()
    existing = await db[COLLECTION].find_one({"brand_id": brand_id, "key": key}, {"_id": 0})
    if existing:
        await db[COLLECTION].update_one({"brand_id": brand_id, "key": key},
                                        {"$set": {"status": ACTIVE, "updated_at": now, "decided_at": now,
                                                  "decided_by": user_id}})
        doc = {**existing, "status": ACTIVE}
    else:
        doc = {**c, "competitor_id": f"cmp_{uuid.uuid4().hex[:16]}", "brand_id": brand_id, "key": key,
               "status": ACTIVE, "source": "added", "created_at": now, "updated_at": now, "decided_at": now,
               "decided_by": user_id}
        await db[COLLECTION].insert_one(dict(doc))
    await _sync_playbook(db, user_id, brand_id, add=D.handle_for_playbook(doc))
    return _public(doc)


REFRESH_AFTER = timedelta(days=7)


async def refresh_due(db, now: Optional[datetime] = None, limit: int = 200) -> int:
    """Weekly: look again for brands that keep a competitor list and haven't been searched for a
    week. Queued as ordinary jobs (shared cache, limits and dedupe apply), a batch at a time, so a
    large number of brands is spread across runs instead of hitting the search all at once."""
    now = now or _now()
    brands = await db[COLLECTION].distinct("brand_id", {"status": ACTIVE})
    queued = 0
    for brand_id in brands:
        if queued >= limit:
            break
        last = await db[queue.COLLECTION].find_one({"brand_id": brand_id, "type": JOB_TYPE}, {"created_at": 1},
                                                   sort=[("created_at", -1)])
        at = (last or {}).get("created_at")
        if at and (now - (at if at.tzinfo else at.replace(tzinfo=timezone.utc))) < REFRESH_AFTER:
            continue
        owner = await db[COLLECTION].find_one({"brand_id": brand_id, "status": ACTIVE, "decided_by": {"$exists": True}},
                                              {"decided_by": 1})
        user_id = (owner or {}).get("decided_by") or ""
        if not user_id:
            continue
        await queue.enqueue(db, JOB_TYPE, {"force": False, "scheduled": True}, brand_id=brand_id, user_id=user_id,
                            key=brand_id, max_attempts=2)
        queued += 1
    return queued

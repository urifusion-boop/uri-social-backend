"""
Listening API (mounted under /market-intelligence/listening).

Never exposes other businesses: a market's subscriber list, other brands'
private terms and their cards are not returned anywhere.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.dependencies import get_active_brand_context, get_db_dependency
from app.domain.responses.uri_response import UriResponse

from ..access import require_mi_write_access
from . import collector, profile as profile_mod
from .config import get_config
from .cycle import run_cycle

router = APIRouter(prefix="/listening", tags=["Market Intelligence · Listening"])

MANUAL_RUN_MIN_MINUTES = 30
CARD_ACTIONS = {"saved", "dismissed", "snoozed", "acted", "created", "shown"}


def _clean(doc: Optional[dict]) -> Optional[dict]:
    if doc:
        doc.pop("_id", None)
    return doc


async def _playbook(db, user_id: str, brand_id: str) -> dict:
    from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService
    res = await BrandProfileService.get(user_id, db, brand_id=brand_id)
    return (res.get("responseData") or {}) if res.get("status") else {}


@router.post("/enable")
async def enable_listening(ctx: dict = Depends(require_mi_write_access),
                           db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """Build (or rebuild) this business's Listening Profile from its Brand
    Playbook and subscribe it to its markets."""
    prof = await profile_mod.build(db, ctx["user_id"], ctx["brand_id"], await _playbook(db, ctx["user_id"], ctx["brand_id"]),
                                   force=True)
    if not prof:
        return UriResponse.error_response("Your Brand Playbook doesn't say what you sell yet — add your products there first.")
    await db["mi_listening_profiles"].update_one({"brand_id": ctx["brand_id"]}, {"$set": {"status": "active"}})
    return UriResponse.get_single_data_response("profile", _public_profile(prof))


@router.post("/disable")
async def disable_listening(ctx: dict = Depends(require_mi_write_access),
                            db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    await db["mi_listening_profiles"].update_one({"brand_id": ctx["brand_id"]}, {"$set": {"status": "paused"}})
    await db["mi_markets"].update_many({"subscribers": ctx["brand_id"]}, {"$pull": {"subscribers": ctx["brand_id"]}})
    return UriResponse.get_single_data_response("profile", {"status": "paused"})


def _public_profile(p: dict) -> dict:
    p = dict(p)
    p.pop("_id", None)
    return {k: p.get(k) for k in ("brand_id", "markets", "country", "private_terms", "status", "version",
                                  "updated_at", "field_origins", "facts")}


@router.get("/status")
async def listening_status(ctx: dict = Depends(get_active_brand_context),
                           db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    cfg = await get_config(db)
    prof = _clean(await db["mi_listening_profiles"].find_one({"brand_id": ctx["brand_id"]}))
    keys = (prof or {}).get("markets") or []
    out_markets = []
    for key in keys:
        m = _clean(await db["mi_markets"].find_one({"key": key})) or {"key": key}
        last = _clean(await db["mi_listen_runs"].find_one({"market": key}, sort=[("started_at", -1)]))
        signals = await db["mi_listen_signals"].count_documents({"market": key, "status": "active",
                                                                 "expires_at": {"$gt": datetime.utcnow()}})
        topics = await db["mi_market_topics"].count_documents({"market": key})
        out_markets.append({
            "key": key, "label": m.get("label"), "last_collected_at": m.get("last_collected_at"),
            "learned_terms": m.get("learned_terms") or [], "topics": topics, "active_signals": signals,
            "last_run": {k: (last or {}).get(k) for k in ("status", "started_at", "finished_at", "items_returned",
                                                          "new_items", "covered_from", "note")} if last else None,
        })
    return UriResponse.get_single_data_response("listening", {
        "profile": _public_profile(prof) if prof else None,
        "markets": out_markets,
        "cadence_hours": cfg["cadence_hours"],
        "budget": {"monthly_usd": cfg["monthly_budget_usd"], "spent_usd": round(await collector.spent_this_month(db), 4)},
    })


@router.get("/pulse")
async def market_pulse(days: int = 7, ctx: dict = Depends(get_active_brand_context),
                       db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """What people in this business's markets are talking about right now:
    top topics by distinct accounts (only covered hours count), with what
    they want. Context, not alerts — signals and cards still need evidence."""
    from collections import Counter, defaultdict
    from .detect import covered_hours

    days = max(1, min(days, 30))
    prof = await db["mi_listening_profiles"].find_one({"brand_id": ctx["brand_id"]})
    now = datetime.utcnow()
    start = now - timedelta(days=days)
    out = []
    for key in (prof or {}).get("markets") or []:
        cov = await covered_hours(db, key, start, now)
        agg: dict = defaultdict(lambda: {"authors": set(), "posts": 0, "intents": Counter(), "needs": Counter()})
        async for r in db["mi_listen_readings"].find({"market": key, "on_topic": True, "published_at": {"$gte": start}}):
            if r["published_at"].replace(minute=0, second=0, microsecond=0) not in cov:
                continue
            for t in r.get("topics") or []:
                a = agg[t]
                a["authors"].add(r.get("author") or r["source_id"])
                a["posts"] += 1
                a["intents"][r["intent"]] += 1
                if r.get("need"):
                    a["needs"][r["need"].strip().lower()] += 1
        top = sorted(agg.items(), key=lambda kv: (-len(kv[1]["authors"]), -kv[1]["posts"]))[:15]
        m = await db["mi_markets"].find_one({"key": key}) or {}
        out.append({"market": key, "label": m.get("label"), "covered_hours": len(cov), "window_days": days,
                    "topics": [{"topic": t, "accounts": len(v["authors"]), "posts": v["posts"],
                                "intents": dict(v["intents"]), "needs": [n for n, _ in v["needs"].most_common(2)]}
                               for t, v in top]})
    return UriResponse.get_single_data_response("pulse", out)


@router.post("/run")
async def run_now(background_tasks: BackgroundTasks, ctx: dict = Depends(require_mi_write_access),
                  db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """Collect and analyse this business's markets now (normally automatic)."""
    prof = await db["mi_listening_profiles"].find_one({"brand_id": ctx["brand_id"], "status": "active"})
    if not prof:
        raise HTTPException(status_code=400, detail="Turn on listening first.")
    recent = await db["mi_markets"].find_one({"key": {"$in": prof.get("markets") or []},
                                              "last_collected_at": {"$gt": datetime.utcnow() - timedelta(minutes=MANUAL_RUN_MIN_MINUTES)}})
    if recent:
        raise HTTPException(status_code=429, detail=f"Your market was checked in the last {MANUAL_RUN_MIN_MINUTES} minutes — "
                                                    "new findings will appear automatically.")
    background_tasks.add_task(run_cycle, db, None, [ctx["brand_id"]])
    return UriResponse.get_single_data_response("run", {"started": True, "markets": prof.get("markets")})


@router.get("/cards")
async def list_cards(ctx: dict = Depends(get_active_brand_context),
                     db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    now = datetime.utcnow()
    cards = [_clean(c) async for c in db["mi_listen_cards"].find(
        {"brand_id": ctx["brand_id"], "status": {"$in": ["new", "shown", "saved"]}, "expires_at": {"$gt": now},
         "$or": [{"snoozed_until": None}, {"snoozed_until": {"$lt": now}}, {"snoozed_until": {"$exists": False}}]}
    ).sort([("scores.total", -1)]).limit(20)]
    new_ids = [c["id"] for c in cards if c.get("status") == "new"]
    if new_ids:
        await db["mi_listen_cards"].update_many({"id": {"$in": new_ids}}, {"$set": {"status": "shown", "shown_at": now}})
    return UriResponse.get_single_data_response("cards", cards)


class CardAction(BaseModel):
    action: str
    reason: Optional[str] = None
    content_id: Optional[str] = None


@router.post("/cards/{card_id}/action")
async def card_action(card_id: str, body: CardAction, ctx: dict = Depends(require_mi_write_access),
                      db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    if body.action not in CARD_ACTIONS:
        raise HTTPException(status_code=400, detail=f"action must be one of {sorted(CARD_ACTIONS)}")
    card = await db["mi_listen_cards"].find_one({"id": card_id, "brand_id": ctx["brand_id"]})
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    now = datetime.utcnow()
    update: dict = {"updated_at": now}
    if body.action == "snoozed":
        update["snoozed_until"] = now + timedelta(days=3)
    elif body.action in ("dismissed", "saved", "acted"):
        update["status"] = body.action
    elif body.action == "created":
        update["status"] = "acted"
        update["content_id"] = body.content_id
    await db["mi_listen_cards"].update_one({"id": card_id}, {"$set": update})
    await db["mi_listen_card_actions"].insert_one({
        "card_id": card_id, "brand_id": ctx["brand_id"], "user_id": ctx["user_id"], "action": body.action,
        "reason": (body.reason or "")[:300] or None, "content_id": body.content_id,
        "market": card["market"], "topic": card["topic"], "type": card["type"],
        "scores": card.get("scores"), "at": now,
    })
    return UriResponse.get_single_data_response("card", {"id": card_id, **update})

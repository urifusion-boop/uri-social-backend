"""
Listening API (mounted under /market-intelligence/listening).

Never exposes other businesses: a market's subscriber list, other brands'
private terms and their cards are not returned anywhere.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.dependencies import get_active_brand_context, get_db_dependency
from app.domain.responses.uri_response import UriResponse
from app.routers.admin_router import verify_admin as _verify_admin

from ..access import require_mi_write_access
from . import collector, discovery, golden, metrics, permissions, profile as profile_mod, replay, review
from .config import _merge, get_config
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
    if not prof.get("markets"):
        return UriResponse.error_response("Uri couldn't work out which market you sell in yet — add your products and "
                                          "industry to your Brand Playbook, then try again.")
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
        topic_statuses: dict = {}
        async for row in db["mi_market_topics"].aggregate([
            {"$match": {"market": key}}, {"$group": {"_id": "$status", "n": {"$sum": 1}}},
        ]):
            topic_statuses[row["_id"]] = row["n"]
        out_markets.append({
            "key": key, "label": m.get("label"), "last_collected_at": m.get("last_collected_at"),
            "learned_terms": m.get("learned_terms") or [], "topics": topics, "topic_statuses": topic_statuses,
            "active_signals": signals, "cadence_hours": m.get("cadence_hours") or cfg["cadence_hours"],
            "last_run": {k: (last or {}).get(k) for k in ("status", "started_at", "finished_at", "items_returned",
                                                          "new_items", "covered_from", "note", "per_query")} if last else None,
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


@router.post("/cards/{card_id}/create")
async def create_from_card(card_id: str, ctx: dict = Depends(require_mi_write_access),
                           db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """§9.3's "Create" deep-link: pass a card's evidence-backed brief straight
    into the content generator instead of the owner re-typing it by hand.

    Scope for now: text content only, one draft per platform in the brief —
    no AI image generation and no credit deduction, both of which the normal
    Create Content flow already owns and which this doesn't try to
    duplicate or silently re-decide the billing for. Generated drafts land
    in the normal drafts list exactly like any other Create Content run."""
    card = await db["mi_listen_cards"].find_one({"id": card_id, "brand_id": ctx["brand_id"]})
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    if card.get("content_id"):
        raise HTTPException(status_code=409, detail="Content was already created from this card")

    action = card.get("action") or {}
    platforms = [p for p in (action.get("platforms") or []) if p]
    if not platforms:
        raise HTTPException(status_code=400, detail="This card has no platforms to create content for")

    seed_parts = [action.get("hook"), *(action.get("outline") or []), action.get("cta")]
    seed_content = ". ".join(p for p in seed_parts if p) or card.get("why") or card.get("observation") or ""
    if not seed_content:
        raise HTTPException(status_code=400, detail="This card has no brief to create content from")

    from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService
    from app.agents.social_media_manager.services.content_generation_service import ContentGenerationService

    playbook = await _playbook(db, ctx["user_id"], ctx["brand_id"])
    brand_context = BrandProfileService.to_brand_context(playbook)
    result = await ContentGenerationService.generate_multi_platform_content(
        user_id=ctx["user_id"], seed_content=seed_content, platforms=platforms,
        seed_type="text", brand_context=brand_context, db=db,
    )
    if not result.get("status"):
        raise HTTPException(status_code=502, detail=result.get("responseMessage") or "Content generation failed")

    drafts = (result.get("responseData") or {}).get("drafts") or []
    draft_ids = [d["id"] for d in drafts if d.get("id")]
    now = datetime.utcnow()
    primary_id = draft_ids[0] if draft_ids else None
    await db["mi_listen_cards"].update_one(
        {"id": card_id},
        {"$set": {"status": "acted", "content_id": primary_id, "created_draft_ids": draft_ids, "updated_at": now}},
    )
    await db["mi_listen_card_actions"].insert_one({
        "card_id": card_id, "brand_id": ctx["brand_id"], "user_id": ctx["user_id"], "action": "created",
        "content_id": primary_id, "market": card["market"], "topic": card["topic"], "type": card["type"],
        "scores": card.get("scores"), "at": now,
    })
    return UriResponse.get_single_data_response("created", {
        "card_id": card_id, "draft_ids": draft_ids,
        "request_id": (result.get("responseData") or {}).get("request_id"),
    })


# ── Ontology curation (PRD §6.3's curation console, §17's human review) ──────
# Admin-only: a market and its topics are shared across every subscribed
# business, not any one tenant's own data, so this isn't gated by
# require_mi_write_access (brand-scoped) like everything above it.

def _clean_topic(t: dict) -> dict:
    t = dict(t)
    t.pop("_id", None)
    return t


@router.get("/curation/topics")
async def curation_topics(status: Optional[str] = None, market: Optional[str] = None, limit: int = 50,
                          admin: dict = Depends(_verify_admin), db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """Topics waiting on a human decision: by default, 'observed' and
    'candidate' — the two states update_market() can't promote out of on
    its own without more evidence, which is exactly what a curator looks
    at and decides about instead of waiting."""
    statuses = [status] if status else ["observed", "candidate"]
    bad = [s for s in statuses if s not in discovery.VALID_STATUSES]
    if bad:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(discovery.VALID_STATUSES)}")
    q: dict = {"status": {"$in": statuses}}
    if market:
        q["market"] = market
    topics = [_clean_topic(t) async for t in db["mi_market_topics"].find(q).sort([("last_seen", -1)]).limit(min(limit, 200))]
    return UriResponse.get_single_data_response("topics", topics)


class CurationDecision(BaseModel):
    status: str
    reason: Optional[str] = None


@router.post("/curation/topics/{market_key}/{topic_key}/decide")
async def curation_decide(market_key: str, topic_key: str, body: CurationDecision,
                          admin: dict = Depends(_verify_admin), db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """A curator's manual override — promote straight to monitored (added
    to the market's own searches) or demote/retire, bypassing (or
    confirming ahead of) the automatic evidence thresholds."""
    if body.status not in discovery.VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(discovery.VALID_STATUSES)}")
    actor = (admin.get("claims") or {}).get("email") or "curator"
    result = await discovery.set_topic_status(db, market_key, topic_key, body.status, body.reason, actor=actor)
    if not result["found"]:
        raise HTTPException(status_code=404, detail="Topic not found")
    return UriResponse.get_single_data_response("topic", result)


# ── Source permission records (PRD §15) — admin-only ─────────────────────────

@router.get("/curation/sources")
async def list_source_permissions(admin: dict = Depends(_verify_admin), db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    await permissions.ensure_seeded(db)
    out = [doc async for doc in db["mi_source_permissions"].find({})]
    for doc in out:
        doc.pop("_id", None)
    return UriResponse.get_single_data_response("sources", out)


class SourceApproval(BaseModel):
    approved: bool
    notes: Optional[str] = None


@router.post("/curation/sources/{source}/approval")
async def set_source_approval(source: str, body: SourceApproval, admin: dict = Depends(_verify_admin),
                              db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """Approve or revoke a source's permission record. A revoked source
    stops collecting on its very next run — collect_market checks this
    before doing anything else."""
    actor = (admin.get("claims") or {}).get("email") or "admin"
    result = await permissions.set_approval(db, source, body.approved, actor, body.notes)
    return UriResponse.get_single_data_response("source", result)


# ── Human review queue (PRD §17) — admin-only ─────────────────────────────────

@router.get("/curation/reviews")
async def list_pending_reviews(market: Optional[str] = None, limit: int = 50,
                               admin: dict = Depends(_verify_admin), db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """Low-confidence readings waiting on a human judgment call, most
    uncertain first."""
    cfg = await get_config(db)
    items = await review.pending_reviews(db, cfg, market, limit)
    return UriResponse.get_single_data_response("reviews", items)


class ReviewResolution(BaseModel):
    on_topic: Optional[bool] = None
    intent: Optional[str] = None
    topics: Optional[List[str]] = None
    note: Optional[str] = None


@router.post("/curation/reviews/{market_key}/{source_id}/resolve")
async def resolve_pending_review(market_key: str, source_id: str, body: ReviewResolution,
                                 admin: dict = Depends(_verify_admin), db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """Records a curator's correction against the reading and logs it for
    training data (PRD §17); does not rewrite counts already computed from
    the original reading — see review.py's own docstring for why."""
    actor = (admin.get("claims") or {}).get("email") or "curator"
    result = await review.resolve_review(db, source_id, market_key, actor, body.on_topic, body.intent,
                                         body.topics, body.note)
    if not result["found"]:
        raise HTTPException(status_code=404, detail="Reading not found")
    return UriResponse.get_single_data_response("review", result)


# ── Offline replay (PRD §17) — admin-only ─────────────────────────────────────

class ReplayRequest(BaseModel):
    market: str
    at: List[str]            # ISO timestamps to replay as of — matches a historical cycle run's own `now`
    candidate_overrides: dict = {}


@router.post("/curation/replay")
async def replay_detection(body: ReplayRequest, admin: dict = Depends(_verify_admin),
                           db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """Runs a candidate config (e.g. different detection thresholds or
    score weights) against already-stored history and compares it with
    what the live config actually produced and stored at the time — PRD
    §17's offline replay, before a threshold/weight change ships. Nothing
    here is persisted to mi_listen_signals."""
    market = await db["mi_markets"].find_one({"key": body.market})
    if not market:
        raise HTTPException(status_code=404, detail="Market not found")
    market.pop("_id", None)

    if not body.at:
        raise HTTPException(status_code=400, detail="At least one timestamp in 'at' is required")
    try:
        timestamps = [datetime.fromisoformat(t) for t in body.at]
    except ValueError:
        raise HTTPException(status_code=400, detail="'at' entries must be ISO timestamps")

    candidate_cfg = _merge(await get_config(db), body.candidate_overrides)
    results = await replay.replay_range(db, market, candidate_cfg, timestamps)
    return UriResponse.get_single_data_response("replay", results)


# ── Golden datasets (PRD §17) — admin-only ────────────────────────────────────

@router.get("/curation/golden")
async def list_golden_examples(market: Optional[str] = None, limit: int = 200,
                               admin: dict = Depends(_verify_admin), db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    examples = await golden.list_golden(db, market, limit)
    return UriResponse.get_single_data_response("golden", examples)


@router.post("/curation/golden/{market_key}/{source_id}")
async def mark_golden_example(market_key: str, source_id: str, admin: dict = Depends(_verify_admin),
                              db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    actor = (admin.get("claims") or {}).get("email") or "curator"
    result = await golden.mark_golden(db, source_id, market_key, actor)
    if not result["found"]:
        raise HTTPException(status_code=404, detail="Reading not found")
    return UriResponse.get_single_data_response("golden", result)


@router.delete("/curation/golden/{market_key}/{source_id}")
async def unmark_golden_example(market_key: str, source_id: str, admin: dict = Depends(_verify_admin),
                                db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    result = await golden.unmark_golden(db, source_id, market_key)
    if not result["removed"]:
        raise HTTPException(status_code=404, detail="Not in the golden set")
    return UriResponse.get_single_data_response("golden", result)


# ── Product metrics (PRD §17) — admin-only ────────────────────────────────────

@router.get("/curation/metrics")
async def product_metrics_endpoint(market: Optional[str] = None, window_days: int = 30,
                                   admin: dict = Depends(_verify_admin), db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    result = await metrics.product_metrics(db, market, window_days)
    return UriResponse.get_single_data_response("metrics", result)

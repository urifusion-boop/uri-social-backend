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
from . import collector, discovery, geo, golden, metrics, ontology, permissions, profile as profile_mod, replay, review
from .geo import LocationScope
from .config import _merge, get_config, unknown_top_level_keys, update_config
from .cycle import run_cycle

router = APIRouter(prefix="/listening", tags=["Market Intelligence · Listening"])

# Matches _Action.platforms' own documented list in personalize.py — a
# connected account isn't necessarily one of these (facebook_ads/google_ads
# are real connections, not postable platforms).
CONTENT_PLATFORMS = {"instagram", "tiktok", "facebook", "x", "linkedin", "whatsapp_status"}

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
    return {k: p.get(k) for k in ("brand_id", "markets", "country", "location_scope", "location_countries",
                                  "private_terms", "status", "version", "updated_at", "field_origins", "facts")}


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


@router.get("/locations")
async def list_locations(db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """The known-place registry (Phase F) for the frontend's location
    picker: continents, countries, and seeded cities/regions. Read-only,
    no brand context needed — this is the same catalog for everyone."""
    await geo.ensure_seeded(db)
    places = await geo.list_places(db)
    return UriResponse.get_single_data_response("locations", {
        "continents": [{"code": code, "label": v["label"], "countries": v["countries"]}
                       for code, v in geo.CONTINENTS.items()],
        "countries": [{"code": code, "name": v["name"], "continent": v["continent"]}
                      for code, v in geo.COUNTRIES.items() if code != "GLOBAL"],
        "places": places,
    })


@router.put("/location-scope")
async def put_location_scope(body: LocationScope, ctx: dict = Depends(require_mi_write_access),
                             db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """Explicit override of this business's listening location scope —
    replaces today's fully-automatic, AI-inferred single country. See
    profile.set_location_scope's docstring for exactly what it changes."""
    try:
        updated = await profile_mod.set_location_scope(db, ctx["brand_id"], body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not updated:
        raise HTTPException(status_code=404, detail="No listening profile for this brand yet — enable listening first.")
    return UriResponse.get_single_data_response("profile", _public_profile(updated))


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


@router.get("/run/active")
async def active_runs(ctx: dict = Depends(get_active_brand_context), db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """Real, in-progress collection state for this business's markets — not
    a simulated percentage. Each query in a run's per_query list flips from
    "pending" to "done" the moment collector._one_query personally finishes,
    independently of the others still running alongside it, so a client
    polling this sees genuine per-source progress (spec §16). Works whether
    the run was started by this user's own "check now" or the scheduled
    cycle, and survives a page refresh — the run doc is the source of
    truth, not any client-side state."""
    prof = await db["mi_listening_profiles"].find_one({"brand_id": ctx["brand_id"], "status": "active"})
    if not prof:
        return UriResponse.get_single_data_response("active_runs", [])
    runs = [r async for r in db["mi_listen_runs"].find(
        {"market": {"$in": prof.get("markets") or []}, "status": "running"}
    ).sort([("started_at", -1)])]
    for r in runs:
        r.pop("_id", None)
    return UriResponse.get_single_data_response("active_runs", runs)


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


class CreateFromCardOverrides(BaseModel):
    """The AI's choice of platform/format is a starting point, never the
    only option — see the card_action docstring below for why this exists.
    Both are optional; omitted fields fall back to the card's own action."""
    platforms: Optional[List[str]] = None
    content_type: Optional[str] = None  # "post" | "carousel" — overrides action.type's post/carousel split


@router.post("/cards/{card_id}/create")
async def create_from_card(card_id: str, overrides: CreateFromCardOverrides = CreateFromCardOverrides(),
                           ctx: dict = Depends(require_mi_write_access),
                           db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """§9.3's "Create" deep-link: pass a card's evidence-backed brief straight
    into the content generator instead of the owner re-typing it by hand.

    For post/carousel/faq cards this now runs the SAME pipeline the normal
    Create Content screen uses (credit gate, real carousel slide generation,
    credit deduction, background AI image generation via _generate_image_bg)
    instead of a stripped-down text-only shortcut — confirmed live: a
    "Format: carousel" card produced a bare-text, non-carousel draft with no
    image at all, because the old shortcut called neither
    CarouselGenerationService nor any image-generation code path. It defaults
    to no reference image and the brand's own library style rotation (no
    custom guide/override) — exactly what a plain Create Content submission
    gets when every optional toggle is left untouched.

    Live-reported bug: every card suggested Instagram only, with no way to
    pick a different platform before content (and a credit) was spent on
    one the owner didn't want — confirmed this wasn't a connection limit
    (the test account had Facebook, Instagram, LinkedIn and TikTok all
    connected; Jane simply defaults to Instagram when the Playbook states no
    platform preference, every time). `overrides` lets the owner pick
    different platforms — validated against what's actually connected, so
    this can never queue content for a platform with nothing to post
    through — and/or swap a carousel suggestion for a single post or back,
    before anything is generated or any credit is spent. A human always has
    final say over the AI's first guess here, the same principle already
    applied to ad tests and videos elsewhere in this module."""
    card = await db["mi_listen_cards"].find_one({"id": card_id, "brand_id": ctx["brand_id"]})
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    if card.get("content_id"):
        raise HTTPException(status_code=409, detail="Content was already created from this card")

    action = card.get("action") or {}
    action_type = action.get("type")
    if overrides.content_type in ("post", "carousel") and action_type in ("post", "carousel", "faq"):
        # A format swap never changes which pipeline branch runs below
        # (video/ad_test stay whatever the card itself is) — it only
        # decides, for an ordinary content card, whether that branch builds
        # a carousel or a single post from the same brief.
        action_type = "carousel" if overrides.content_type == "carousel" else "post"

    if action_type == "video":
        # The real video pipeline (VideoStoryboardService.generate_storyboard)
        # has a hard requirement this endpoint cannot satisfy on its own: it
        # needs real brand images as its creative source, not just text — a
        # listening card carries no images, and inventing or auto-selecting
        # some would hand the owner a video they never chose the visuals for.
        # So this doesn't try to produce a finished video; it hands back the
        # evidence-backed brief as creative-direction text, pre-sized for the
        # storyboard screen's own `optional_text`/platform/duration fields —
        # the owner still picks images there, exactly like every other video,
        # just without retyping what Uri already worked out (PRD §9.3: "pass
        # the brief into the content generator, the video creator... — no
        # copy-pasting"). No draft exists yet, so nothing here is idempotent
        # to guard — re-opening this brief later is fine.
        brief_parts = [action.get("hook"), *(action.get("outline") or []), action.get("cta")]
        creative_direction = ". ".join(p for p in brief_parts if p) or card.get("why") or card.get("observation") or ""
        if not creative_direction:
            raise HTTPException(status_code=400, detail="This card has no brief to build a video from")
        platform = next((p for p in (overrides.platforms or action.get("platforms") or []) if p), "instagram_reels")
        now = datetime.utcnow()
        await db["mi_listen_card_actions"].insert_one({
            "card_id": card_id, "brand_id": ctx["brand_id"], "user_id": ctx["user_id"], "action": "created",
            "market": card["market"], "topic": card["topic"], "type": card["type"], "scores": card.get("scores"),
            "at": now, "note": "video brief handed to Create Video — no draft created, images still required",
        })
        return UriResponse.get_single_data_response("video_brief", {
            "card_id": card_id, "optional_text": creative_direction, "target_platform": platform,
            "target_duration_seconds": 20, "video_style": "clean_commercial",
        })

    if action_type == "ad_test":
        # Jane Ads is a conversational planner (app/agents/jane_ads) — there
        # is no single "create a draft campaign" call to make. The
        # established pattern for starting a thread pre-filled with a brief
        # is threads.seed_message_from_campaign (used by the "duplicate a
        # launched campaign" flow): build the same shape of plain-English
        # seed from this card instead, open a fresh draft thread, and return
        # both so the frontend sends the seed message through the normal
        # chat flow — exactly like duplicate_thread already does. Nothing
        # here calls /plan, /creative or any launch step: the thread starts
        # in "draft" status and stays there until the owner drives it
        # through Jane Ads themselves, same approval gate every other
        # campaign goes through (PRD §9.3: "draft only, requires approval").
        from app.agents.jane_ads.threads import create_thread, seed_message_from_card, title_from_message

        seed = seed_message_from_card(card)
        thread = await create_thread(db, ctx["brand_id"], ctx["user_id"], title_from_message(seed))
        now = datetime.utcnow()
        await db["mi_listen_cards"].update_one(
            {"id": card_id}, {"$set": {"status": "acted", "content_id": thread["thread_id"], "updated_at": now}})
        await db["mi_listen_card_actions"].insert_one({
            "card_id": card_id, "brand_id": ctx["brand_id"], "user_id": ctx["user_id"], "action": "created",
            "content_id": thread["thread_id"], "market": card["market"], "topic": card["topic"],
            "type": card["type"], "scores": card.get("scores"), "at": now,
        })
        return UriResponse.get_single_data_response("ad_test_thread", {
            "card_id": card_id, "thread": thread, "seed_message": seed,
        })

    from app.agents.social_media_manager.services.social_account_service import SocialAccountService

    platforms = [p for p in (overrides.platforms or action.get("platforms") or []) if p]
    if not platforms:
        raise HTTPException(status_code=400, detail="This card has no platforms to create content for")
    if overrides.platforms:
        # Content-postable platforms only — a connected account can also be
        # an ad account (facebook_ads, google_ads), which is a real
        # connection but not something content generation accepts, so it's
        # rejected here the same way an unconnected platform is rather than
        # failing confusingly two steps later.
        not_postable = [p for p in platforms if p not in CONTENT_PLATFORMS]
        if not_postable:
            raise HTTPException(status_code=400, detail=f"Not a postable platform: {', '.join(not_postable)}.")
        conn_result = await SocialAccountService.get_user_connections(db, ctx["user_id"], ctx["brand_id"])
        connected = set((conn_result.get("responseData") or {}).get("connected_platforms") or [])
        not_connected = [p for p in platforms if p not in connected]
        if not_connected:
            raise HTTPException(
                status_code=400,
                detail=f"Not connected: {', '.join(not_connected)}. Connect it first, or pick a different platform.",
            )

    seed_parts = [action.get("hook"), *(action.get("outline") or []), action.get("cta")]
    seed_content = ". ".join(p for p in seed_parts if p) or card.get("why") or card.get("observation") or ""
    if not seed_content:
        raise HTTPException(status_code=400, detail="This card has no brief to create content from")

    import asyncio

    from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService
    from app.agents.social_media_manager.services.carousel_generation_service import CarouselGenerationService
    from app.agents.social_media_manager.services.content_generation_service import ContentGenerationService
    from app.agents.social_media_manager.routers.complete_social_manager import (
        _BG_IMAGE_TASKS, _apply_intelligent_fallbacks, _generate_image_bg, _resolve_carousel_shared_context,
    )
    from app.services.CreditService import credit_service
    from app.services.TrialService import trial_service

    # Same credit gate the normal Create Content screen uses — checked BEFORE
    # generating anything, exactly like generate_content does, instead of
    # silently generating content for free the way this shortcut used to.
    is_trial_user = await trial_service.has_active_trial(ctx["user_id"])
    if not is_trial_user and not await credit_service.check_sufficient_credits(ctx["user_id"]):
        raise HTTPException(status_code=402, detail="You've run out of credits. Upgrade to continue.")

    profile_result = await BrandProfileService.get(ctx["user_id"], db, brand_id=ctx["brand_id"])
    profile_data = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}
    missing_fields = [k for k in ("brand_name", "brand_colors", "industry") if not profile_data.get(k)]
    # A card click has no dialog to ask "continue with defaults?" the way the
    # Create Content form does — it always proceeds with the same safe
    # fallbacks that path uses once a user acknowledges, since a one-click
    # action has no other UI moment to ask that question at all.
    profile_data = _apply_intelligent_fallbacks(profile_data, missing_fields)
    brand_context = BrandProfileService.to_brand_context(profile_data)
    brand_context["user_id"] = ctx["user_id"]
    brand_context["brand_id"] = ctx["brand_id"]

    post_type = "carousel" if action_type == "carousel" else "feed"
    if post_type == "carousel":
        num_slides = max(2, min(10, len(action.get("outline") or []) or 3))
        result = await CarouselGenerationService.generate_multi_platform(
            user_id=ctx["user_id"], seed_content=seed_content, platforms=platforms,
            brand_context=brand_context, num_slides=num_slides, db=db, force_num_slides=True,
        )
    else:
        result = await ContentGenerationService.generate_multi_platform_content(
            user_id=ctx["user_id"], seed_content=seed_content, platforms=platforms,
            seed_type="text", brand_context=brand_context, db=db,
        )
    if not result.get("status"):
        raise HTTPException(status_code=502, detail=result.get("responseMessage") or "Content generation failed")

    drafts = (result.get("responseData") or {}).get("drafts") or []
    draft_ids = [d["id"] for d in drafts if d.get("id")]
    request_id = (result.get("responseData") or {}).get("request_id")

    # Same credit deduction generate_content does — 1 for a plain post/faq,
    # 1 per carousel draft (the drafts list is per-platform; a known,
    # pre-existing quirk in that same formula upstream, not something this
    # endpoint should silently diverge from).
    credits_to_deduct = len(drafts) if post_type == "carousel" and drafts else 1
    if request_id:
        if is_trial_user:
            await trial_service.deduct_trial_credit(
                user_id=ctx["user_id"], campaign_id=request_id, reason="campaign_generation",
                amount=credits_to_deduct,
            )
        else:
            await credit_service.deduct_credit(
                user_id=ctx["user_id"], campaign_id=request_id, reason="campaign_generation",
                retry_count=0, amount=credits_to_deduct,
            )

    # Real AI images, same as a normal Create Content submission with no
    # advanced options touched: no reference image, no custom guide/style
    # override — just the brand's own library style rotation. Fired as
    # tracked asyncio tasks (not FastAPI BackgroundTasks) so several
    # draft/slide images generate concurrently, same as generate_content.
    if draft_ids:
        await db["content_drafts"].update_many(
            {"id": {"$in": draft_ids}}, {"$set": {"has_image": True, "image_failed": False}})
        for d in drafts:
            d["has_image"] = True
    # Structural fix, not a one-off: see the identical comment on this same
    # pattern in content_calendar_v2_router.py and complete_social_manager.py
    # — anything that throws between the optimistic has_image=True write
    # above and the asyncio.create_task calls below leaves the draft stuck
    # "generating" forever, since the one task that would ever flip
    # image_failed never got created. Wrap setup so a failure here marks the
    # batch failed instead of leaving it stuck.
    bg_tasks: list[asyncio.Task] = []
    try:
        for draft in drafts:
            if post_type == "carousel":
                slides = draft.get("slides") or []
                carousel_id = draft["id"]
                carousel_brand_context = await _resolve_carousel_shared_context(
                    db=db, brand_context=brand_context, style_override=None,
                    carousel_id=carousel_id, platform=draft["platform"],
                )
                for slide_index, slide in enumerate(slides):
                    slide_content = f"{slide.get('headline', '')} {slide.get('body', '')}".strip()
                    enriched_seed = f"{seed_content}. This slide: {slide_content}"
                    bg_tasks.append(asyncio.create_task(_generate_image_bg(
                        draft_id=draft["id"], platform=draft["platform"], content=slide_content or draft["content"],
                        seed_content=enriched_seed, brand_context=carousel_brand_context, db=db,
                        post_type=post_type, slide_index=slide_index, total_slides=len(slides), carousel_id=carousel_id,
                    )))
            else:
                bg_tasks.append(asyncio.create_task(_generate_image_bg(
                    draft_id=draft["id"], platform=draft["platform"], content=draft["content"],
                    seed_content=seed_content, brand_context=brand_context, db=db, post_type=post_type,
                )))
    except Exception as image_setup_error:
        print(f"❌ [Listening] image task setup failed before any background task started "
              f"for card {card_id}: {image_setup_error}")
        import traceback as _tb
        print(_tb.format_exc())
        if draft_ids:
            await db["content_drafts"].update_many(
                {"id": {"$in": draft_ids}},
                {"$set": {"image_failed": True, "updated_at": datetime.utcnow()}},
            )
        bg_tasks = []

    _BG_IMAGE_TASKS.update(bg_tasks)
    for t in bg_tasks:
        t.add_done_callback(_BG_IMAGE_TASKS.discard)

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
        "card_id": card_id, "draft_ids": draft_ids, "request_id": request_id,
    })


# ── Ontology curation (PRD §6.3's curation console, §17's human review) ──────
# Admin-only: a market and its topics are shared across every subscribed
# business, not any one tenant's own data, so this isn't gated by
# require_mi_write_access (brand-scoped) like everything above it.

def _clean_topic(t: dict) -> dict:
    t = dict(t)
    t.pop("_id", None)
    t.pop("embedding", None)  # a 1536-float vector has no place in an admin list payload
    return t


@router.get("/curation/markets")
async def curation_markets(admin: dict = Depends(_verify_admin), db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """Every market that exists, for the curation console's market picker —
    nothing else in this API lists markets across brands (the brand-scoped
    /listening/status only ever returns the caller's own)."""
    out = []
    async for m in db["mi_markets"].find({}).sort([("key", 1)]):
        m.pop("_id", None)
        m["subscriber_count"] = len(m.get("subscribers") or [])
        m.pop("subscribers", None)
        out.append(m)
    return UriResponse.get_single_data_response("markets", out)


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


@router.get("/curation/topics/{market_key}/{topic_key}/history")
async def curation_topic_history(market_key: str, topic_key: str, admin: dict = Depends(_verify_admin),
                                 db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """Appendix A's term_history — every lifecycle transition this topic has
    ever made, most recent first. The topic doc itself only holds current
    state, so this is the only place a reverted or repeated decision is
    still visible."""
    history = await discovery.term_history(db, market_key, topic_key)
    return UriResponse.get_single_data_response("history", history)


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


# ── Settings: budgets, cadence, thresholds, experiments ─────────────────────

@router.get("/curation/config")
async def curation_get_config(admin: dict = Depends(_verify_admin), db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """The live, merged config (DEFAULTS + whatever's been overridden) —
    exactly what every scheduled cycle actually reads, not a copy of the
    defaults in code."""
    return UriResponse.get_single_data_response("config", await get_config(db))


class ConfigUpdate(BaseModel):
    overrides: dict


@router.put("/curation/config")
async def curation_update_config(body: ConfigUpdate, admin: dict = Depends(_verify_admin),
                                 db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """Change budgets/cadence/thresholds/experiments live, no deploy. Only
    top-level sections that already exist in DEFAULTS are accepted — a typo
    ("buget" for "budget") is rejected loudly here rather than silently
    stored as dead config nothing ever reads."""
    bad = unknown_top_level_keys(body.overrides)
    if bad:
        raise HTTPException(status_code=400, detail=f"unknown config section(s): {bad}")
    updated = await update_config(db, body.overrides)
    return UriResponse.get_single_data_response("config", updated)


# ── Settings: ontology terms ──────────────────────────────────────────────

@router.get("/curation/ontology")
async def curation_list_ontology(admin: dict = Depends(_verify_admin), db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    await ontology.ensure_seeded(db)
    return UriResponse.get_single_data_response("categories", await ontology.categories(db))


class OntologyTerm(BaseModel):
    term: str
    lang: Optional[str] = None  # None = core_terms; a language code = that language's local_terms


@router.post("/curation/ontology/{category_id}/terms")
async def curation_add_ontology_term(category_id: str, body: OntologyTerm, admin: dict = Depends(_verify_admin),
                                     db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    """A curator typing a term straight in (PRD §6.1), without waiting for
    the discovery pipeline to surface it as a candidate first."""
    result = await ontology.add_term(db, category_id, body.term, body.lang)
    if result is None:
        raise HTTPException(status_code=404, detail="Category not found")
    return UriResponse.get_single_data_response("category", result)


@router.delete("/curation/ontology/{category_id}/terms/{term}")
async def curation_remove_ontology_term(category_id: str, term: str, lang: Optional[str] = None,
                                        admin: dict = Depends(_verify_admin),
                                        db: AsyncIOMotorDatabase = Depends(get_db_dependency)):
    result = await ontology.remove_term(db, category_id, term, lang)
    if result is None:
        raise HTTPException(status_code=404, detail="Category not found")
    return UriResponse.get_single_data_response("category", result)


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
    # Cross-tenant, de-identified (PRD §15) — intentionally ignores the
    # `market` filter above, which is this one tenant's own view; a
    # minimum-group threshold below which a group is simply absent, never
    # shown with too little support to mean anything (metrics.py's own
    # docstring on this function).
    cfg = await get_config(db)
    result["category_content_performance"] = await metrics.category_content_performance(db, cfg)
    return UriResponse.get_single_data_response("metrics", result)

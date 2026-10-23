# app/agents/content_calendar_v2/routers/content_calendar_v2_router.py
"""
Content Calendar V2 — API endpoints.

Registered in app/main.py with prefix "/social-media/content-calendar-v2"
(mirrors visual_engine_v2_router's own-package + own-prefix isolation
pattern). Never touches v1's /content-calendar/* routes, collection, or
service — see the plan this was built against for the full isolation
rationale: /Users/macintoshhd/.claude/plans/enchanted-wiggling-treehouse.md
"""
import asyncio
from datetime import datetime
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.dependencies import get_db_dependency, get_active_brand_context
from app.domain.responses.uri_response import UriResponse
from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService
from app.agents.social_media_manager.services.content_generation_service import ContentGenerationService

from ..models import PlanGenerateRequestV2, CreateDraftRequestV2, RegenerateItemRequestV2
from ..services import content_calendar_v2_service as cal_v2_svc

router = APIRouter(tags=["Content Calendar V2"])


def _build_draft_seed_content(item: dict) -> str:
    """The plan item's full brief, folded into one seed string for the real
    draft generator — pulled out of create_draft_from_item_v2 as its own
    pure function so it's directly testable without mocking credits/DB.

    Leads with the strategic framing that justified this item in the first
    place — confirmed missing (Oct 2026 consistency audit): the plan's own
    territory/content_pillar/audience_segment never reached the real draft
    generator, which only ever saw the brand's WHOLE content_pillars list
    generically via brand_context, not which one THIS item was actually
    planned under. The title/hook already carry the topic, but not the
    strategic intent behind it — put that first, so the downstream writer
    executes the same brief the plan committed to instead of re-deriving a
    plausible-but-different one from the topic alone.
    """
    seed_parts: list = []
    _pillar = item.get("content_pillar")
    _territory = item.get("territory")
    _audience = item.get("audience_segment")
    if _pillar or _territory or _audience:
        framing = []
        if _pillar:
            framing.append(f"content pillar: {_pillar}")
        if _territory:
            framing.append(f"creative territory: {_territory}")
        if _audience:
            framing.append(f"audience segment: {_audience}")
        seed_parts.append("This post was planned as part of the calendar's " + ", ".join(framing) + ".")
    seed_parts.append(f"{item.get('title', '')}. {item.get('description', '')}")
    if item.get("hook"):
        seed_parts.append(f"Opening hook to use: {item['hook']}")
    if item.get("key_points"):
        seed_parts.append("Key points to cover: " + "; ".join(str(p) for p in item["key_points"] if p))
    if item.get("caption_direction"):
        seed_parts.append(f"Caption direction: {item['caption_direction']}")
    if item.get("cta"):
        seed_parts.append(f"Call to action: {item['cta']}")
    exact_copy = item.get("exact_copy") or {}
    if exact_copy.get("caption"):
        seed_parts.append(f"Publish-ready caption already written for this idea (use as the strong starting point): {exact_copy['caption']}")
    # Richer creative direction the rewritten engine now produces (no v1
    # equivalent) — folded into the seed so downstream generation gets
    # the same visual intent the calendar item already committed to.
    central_visual_idea = (item.get("creative_direction") or {}).get("central_visual_idea")
    if central_visual_idea:
        seed_parts.append(f"Central visual idea: {central_visual_idea}")
    if item.get("designer_execution_notes"):
        seed_parts.append(f"Production notes: {item['designer_execution_notes']}")
    return "\n".join(seed_parts)


@router.get("/plan")
async def get_plan_v2(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_active_brand_context),
):
    """Return the most recent 30-day V2 plan (active, still generating, or
    failed), or 404 if none exists. The frontend polls this while a plan
    has status 'generating'."""
    plan = await cal_v2_svc.get_latest_plan(ctx["user_id"], db, brand_id=ctx["brand_id"])
    if not plan:
        raise HTTPException(status_code=404, detail="No active Content Calendar V2 plan")
    return UriResponse.get_single_data_response("calendar_plan_v2", plan)


@router.post("/plan/generate")
async def generate_plan_v2_endpoint(
    request: PlanGenerateRequestV2,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_active_brand_context),
):
    """Kick off generation of the 30-day V2 content plan and return
    immediately with either the existing active plan or a status='generating'
    placeholder. The real multi-minute pipeline runs as a background task
    (the synchronous path reliably exceeds the API gateway timeout); the
    frontend polls GET /plan until status flips to 'active' or 'failed'."""
    user_id = ctx["user_id"]
    brand_id = ctx["brand_id"]
    try:
        profile_result = await BrandProfileService.get(user_id, db, brand_id=brand_id)
        raw_profile = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}
        brand = BrandProfileService.to_brand_context(raw_profile) if raw_profile else {}
        plan = await cal_v2_svc.start_plan_generation(
            user_id=user_id, platforms=request.platforms, brand=brand, db=db,
            force=request.force_regenerate, brand_id=brand_id,
        )
        print(f"[CalendarV2] plan_id={plan.get('plan_id')} status={plan.get('status')} items={len(plan.get('items', []))}")
        return UriResponse.get_single_data_response("calendar_plan_v2", plan)
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/plan/{plan_id}/item/{item_index}/regenerate")
async def regenerate_item_v2_endpoint(
    plan_id: str,
    item_index: int,
    request: RegenerateItemRequestV2,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_active_brand_context),
):
    """Regenerate a single item — versioning-aware: the prior content is
    pushed to that item's version_history before being overwritten."""
    try:
        updated = await cal_v2_svc.regenerate_item_v2(
            plan_id, item_index, ctx["user_id"], db, brand_id=ctx["brand_id"], reason=request.reason,
            regen_scope=request.scope, new_format=request.new_format,
        )
        return UriResponse.get_single_data_response("calendar_plan_v2", updated)
    except ValueError as e:
        # Bad scope/new_format is a client input error (400), not a missing
        # plan/item (404) — regenerate_item_v2 validates scope before ever
        # looking either up, so the message itself tells them apart.
        msg = str(e)
        status = 400 if ("scope" in msg or "new_format" in msg) else 404
        raise HTTPException(status_code=status, detail=msg)
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/plan/{plan_id}/item/{item_index}/versions")
async def get_item_versions_endpoint(
    plan_id: str,
    item_index: int,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_active_brand_context),
):
    try:
        versions = await cal_v2_svc.get_item_versions(plan_id, item_index, ctx["user_id"], db, brand_id=ctx["brand_id"])
        return UriResponse.get_single_data_response("calendar_v2_versions", {"versions": versions})
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/plan/{plan_id}/item/{item_index}/approve")
async def approve_item_v2_endpoint(
    plan_id: str,
    item_index: int,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_active_brand_context),
):
    updated = await cal_v2_svc.approve_item_v2(plan_id, item_index, ctx["user_id"], db, brand_id=ctx["brand_id"])
    return UriResponse.get_single_data_response("calendar_plan_v2", updated)


def _single_image_seed_with_headline(image_seed_content: str, exact_copy: dict) -> str:
    """image_seed_content always ends with the upstream ai_image_prompt's own
    "no text, no logos" instruction (content_calendar_v2_service.py) — the
    carousel branch of create_draft_from_item_v2 overrides that by appending
    each slide's real headline/body AFTER it, since the image model treats
    the most-recent, most-specific instruction as the one that wins. This
    single-image path never did the equivalent, so the model inconsistently
    took "no text, no logos" literally instead of the generic "render a
    headline" style guidance (confirmed live: single-image posts
    intermittently came back as a bare photo + logo; carousel slides never
    did, because they always have this override). Pure function — no I/O —
    so it's unit-tested directly rather than through the whole endpoint.
    """
    item_headline = exact_copy.get("headline")
    return f"{image_seed_content}. Headline: {item_headline}" if item_headline else image_seed_content


@router.post("/plan/{plan_id}/item/{item_index}/create-draft")
async def create_draft_from_item_v2(
    plan_id: str,
    item_index: int,
    request: CreateDraftRequestV2,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_active_brand_context),
):
    """Create a real content draft from a V2 plan item — near-identical to
    v1's create_draft_from_calendar_day (complete_social_manager.py) against
    the v2 collection, reusing the same downstream generation services."""
    user_id = ctx["user_id"]
    brand_id = ctx["brand_id"]
    try:
        from app.services.CreditService import credit_service
        from app.services.TrialService import trial_service

        scope = cal_v2_svc._cal_v2_scope(user_id, brand_id)
        plan = await db[cal_v2_svc.COLLECTION].find_one({**scope, "plan_id": plan_id}, {"_id": 0})
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")
        item = next((it for it in plan["items"] if it["day_index"] == item_index), None)
        if not item:
            raise HTTPException(status_code=404, detail=f"Item {item_index} not found")

        # No auto-draft path for video ideas — there's no video render pipeline
        # wired to the calendar (that's the separate Video tab / storyboard job
        # flow). The item carries a full video_idea script; the frontend hides
        # Create Draft for these formats and shows the plan instead, but guard
        # server-side too so a direct API call can't silently produce a wrong
        # text/image draft from what's meant to be a video script.
        if item.get("format") in ("video", "product_video", "ai_video"):
            raise HTTPException(
                status_code=400,
                detail="This is a video idea, not a draft-ready post. Use the plan (hook/talking points/scenes/cta) "
                       "to build the video yourself in the Video tab.",
            )

        # Computed early (moved ahead of generation) so the sufficiency check
        # below can use the REAL cost, not a flat "has at least 1 credit"
        # guess — closes the gap where the pre-check passed on 1 credit but
        # the actual charge (per-slide for a carousel) needed more, and the
        # unchecked deduct_credit()/deduct_trial_credit() return value let
        # the draft through as a silent free ride. See PRD 7.2 / the credit
        # cost rule this endpoint documents further down at credits_to_deduct.
        is_carousel = item.get("format") == "carousel"
        planned_slides = (
            len(((item.get("carousel") or {}).get("slides")) or [])
            or int(item.get("carousel_slide_count") or 3)
        ) if is_carousel else 1
        if is_carousel and request.num_slides:
            # The user picked their own slide count — it used to win outright
            # up to Instagram's 10-slide platform limit, which let a calendar
            # carousel ship with far more slides than the plan's own "never
            # more than 3" rule (VALIDATION_RULES["carousel_slide_range"] in
            # creative_framework.py) — the plan's suggested count already
            # respects that cap; only this user-override path didn't.
            # Confirmed live: a manually-picked slide count produced a
            # published carousel well past 3 slides. Clamped to the same
            # business rule here too, so it can't be bypassed by picking a
            # bigger number.
            planned_slides = max(2, min(3, int(request.num_slides)))
        estimated_credits = planned_slides if is_carousel else 1

        is_trial_user = await trial_service.has_active_trial(user_id)
        if is_trial_user:
            # TrialService has no amount-aware sufficiency check (only
            # has_active_trial(), which is the same "≥1 credit" shape as the
            # paid path's old bug) — read the trial doc directly rather than
            # add a new shared-service method for a V2-only change.
            trial_doc = await trial_service.trials_collection.find_one({"user_id": user_id})
            if not trial_doc or trial_doc.get("credits_remaining", 0) < estimated_credits:
                return JSONResponse(
                    status_code=402,
                    content={
                        "status": False, "responseCode": 402,
                        "responseMessage": f"This needs {estimated_credits} trial credit(s) — you don't have enough left.",
                        "responseData": {"credits_remaining": (trial_doc or {}).get("credits_remaining", 0), "upgrade_url": "/pricing"},
                    },
                )
        else:
            has_credits = await credit_service.check_sufficient_credits(user_id, required=estimated_credits)
            if not has_credits:
                return JSONResponse(
                    status_code=402,
                    content={
                        "status": False, "responseCode": 402,
                        "responseMessage": f"This needs {estimated_credits} credit(s) — you don't have enough. Upgrade to continue.",
                        "responseData": {"credits_remaining": 0, "upgrade_url": "/pricing"},
                    },
                )

        seed_content = _build_draft_seed_content(item)

        profile_result = await BrandProfileService.get(user_id, db, brand_id=brand_id)
        raw_profile = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}
        brand_context = BrandProfileService.to_brand_context(raw_profile) if raw_profile else {}
        brand_context["brand_id"] = brand_id

        if is_carousel:
            from app.agents.social_media_manager.services.carousel_generation_service import CarouselGenerationService
            # planned_slides was computed above (before the credit check) —
            # the V2 pipeline's planned count (2-5, PRD rule) unless the user
            # chose their own (2-10). Pass it through with
            # force_num_slides so the draft doesn't re-run content detection
            # and expand a 3-slide idea into 7 (confirmed live bug).
            result = await CarouselGenerationService.generate_multi_platform(
                user_id=user_id, seed_content=seed_content, platforms=request.platforms,
                brand_context=brand_context, db=db,
                num_slides=planned_slides, force_num_slides=True,
            )
        else:
            result = await ContentGenerationService.generate_multi_platform_content(
                user_id=user_id, seed_content=seed_content, platforms=request.platforms,
                seed_type="calendar_v2_idea", brand_context=brand_context, db=db,
            )

        if result.get("status"):
            drafts = result.get("responseData", {}).get("drafts", [])
            draft_ids = [d.get("draft_id") or d.get("id") for d in drafts if d]
            await cal_v2_svc.mark_acted_on_v2(plan_id, item_index, draft_ids, user_id, db, brand_id=brand_id)

            # This item started life as a real Listening opportunity card
            # (see _listening_candidate_concepts) — now that it's actually
            # become a draft, close the loop back onto that card the same
            # way the direct /listening/cards/{id}/create path does: same
            # field shape as that endpoint's own mi_listen_card_actions
            # insert (market/type are load-bearing — personalize.py's
            # relevance-adjustment learning signal queries on them, not
            # just card_id), so the learning loop (Listening PRD §11:
            # "content created -> links insight -> asset -> post") sees
            # this regardless of which of the two routes was used.
            source_card_id = item.get("source_card_id")
            if source_card_id and draft_ids:
                now = datetime.utcnow()
                source_card = await db["mi_listen_cards"].find_one(
                    {"id": source_card_id, "brand_id": brand_id, "content_id": None}
                )
                if source_card:
                    await db["mi_listen_cards"].update_one(
                        {"id": source_card_id},
                        {"$set": {"status": "acted", "content_id": draft_ids[0], "created_draft_ids": draft_ids,
                                 "updated_at": now}},
                    )
                    await db["mi_listen_card_actions"].insert_one({
                        "card_id": source_card_id, "brand_id": brand_id, "user_id": user_id, "action": "created",
                        "content_id": draft_ids[0], "market": source_card["market"], "topic": source_card["topic"],
                        "type": source_card["type"], "scores": source_card.get("scores"), "at": now,
                        "via": "content_calendar_v2",
                    })

            request_id = result.get("responseData", {}).get("request_id")
            # V2-only cost rule (deliberately diverges from v1's create_draft_from_calendar_day,
            # which charges 1 credit PER PLATFORM draft): 1 credit per SLIDE, flat across every
            # platform — a 3-slide carousel is 3 credits whether it's posted to 1 platform or 4.
            credits_to_deduct = planned_slides if (is_carousel and drafts) else 1
            if request_id:
                if is_trial_user:
                    deducted = await trial_service.deduct_trial_credit(
                        user_id=user_id, campaign_id=request_id, reason="campaign_generation", amount=credits_to_deduct,
                    )
                else:
                    deducted = await credit_service.deduct_credit(
                        user_id=user_id, campaign_id=request_id, reason="campaign_generation",
                        retry_count=0, amount=credits_to_deduct,
                    )
                # The pre-generation sufficiency check above already confirmed enough
                # balance for credits_to_deduct, so a False here means a genuine race
                # (another request drained the balance in between) — rare, but the old
                # code printed "deducted" unconditionally either way, silently letting
                # the draft through uncharged. Content is already generated (real AI
                # cost already spent) so there's nothing to roll back; at minimum this
                # makes the failure visible instead of misreporting success.
                if deducted:
                    print(f"[CalendarV2] deducted {credits_to_deduct} credit(s) for draft {request_id}")
                else:
                    print(f"⚠️ [CalendarV2] credit deduction of {credits_to_deduct} FAILED for draft {request_id} "
                          f"(user_id={user_id}) — draft was created but balance was not charged")

            # Mirrors v1's create_draft_from_calendar_day include_images block
            # (complete_social_manager.py) — confirmed missing here entirely:
            # CreateDraftRequestV2.include_images was accepted by the model and
            # sent by the frontend's "Include AI-generated image" checkbox, but
            # never once read in this endpoint, so no image ever got generated
            # regardless of the checkbox. V2 also has its own ai_image_prompt
            # per item (v1 has no equivalent — an LLM-written, ready-to-use
            # image-gen description), folded into the seed here for a stronger
            # signal than v1's generic seed_content alone.
            if request.include_images and drafts:
                from app.agents.social_media_manager.routers.complete_social_manager import (
                    _generate_image_bg, _resolve_carousel_shared_context, _BG_IMAGE_TASKS,
                )
                if draft_ids:
                    await db["content_drafts"].update_many(
                        {"id": {"$in": draft_ids}},
                        {"$set": {"has_image": True, "image_failed": False}},
                    )
                    for d in drafts:
                        d["has_image"] = True

                image_seed_content = seed_content
                if item.get("ai_image_prompt"):
                    image_seed_content = f"{seed_content}\nImage direction: {item['ai_image_prompt']}"

                # Carousel drafts need ONE _generate_image_bg call PER SLIDE
                # (post_type="carousel", slide_index, total_slides, carousel_id)
                # — that's how the image lands on slides[i].image_url, which is
                # what the frontend actually polls. Calling it once per draft the
                # way the non-carousel branch does leaves every slide's
                # image_url null forever (confirmed live: "Generating slide
                # image…" never resolves) — this mirrors the main Create-tab
                # flow's carousel loop (complete_social_manager.py) that v1's
                # own calendar create-draft endpoint is missing too.
                #
                # Each slide's task used to receive the same raw brand_context
                # and resolve its own custom-guide/library-style/CTA choice
                # independently — the exact bug _resolve_carousel_shared_context
                # was built to eliminate on the main Create-tab flow, but this
                # endpoint never called it, so calendar-generated carousels kept
                # the slide-0-writes-others-read race AND the per-slide library
                # style drift (confirmed live via a client-reported 3-slide
                # carousel with 3 different visual styles). Resolve once per
                # carousel draft here too, before any slide's task is created.
                _bg_image_tasks: list = []
                if item.get("format") == "carousel":
                    for d in drafts:
                        draft_id = d.get("draft_id") or d.get("id")
                        slides = d.get("slides") or []
                        total_slides = len(slides)
                        carousel_brand_context = await _resolve_carousel_shared_context(
                            db=db,
                            brand_context=brand_context,
                            style_override=None,
                            carousel_id=draft_id,
                            platform=d.get("platform", "facebook"),
                        )
                        for slide_index, slide in enumerate(slides):
                            slide_content = f"{slide.get('headline', '')} {slide.get('body', '')}".strip()
                            enriched_seed = f"{image_seed_content}. This slide: {slide_content}"
                            _bg_image_tasks.append(asyncio.create_task(_generate_image_bg(
                                draft_id=draft_id,
                                platform=d.get("platform", "facebook"),
                                content=slide_content or d.get("content", image_seed_content),
                                seed_content=enriched_seed,
                                brand_context=carousel_brand_context,
                                db=db,
                                reference_image=None,
                                post_type="carousel",
                                slide_index=slide_index,
                                total_slides=total_slides,
                                carousel_id=draft_id,
                            )))
                else:
                    enriched_seed = _single_image_seed_with_headline(image_seed_content, exact_copy)
                    _bg_image_tasks = [
                        asyncio.create_task(_generate_image_bg(
                            draft_id=d.get("draft_id") or d.get("id"),
                            platform=d.get("platform", "facebook"),
                            content=d.get("content", image_seed_content),
                            seed_content=enriched_seed,
                            brand_context=brand_context,
                            db=db,
                            reference_image=None,
                        ))
                        for d in drafts
                    ]
                _BG_IMAGE_TASKS.update(_bg_image_tasks)
                for t in _bg_image_tasks:
                    t.add_done_callback(_BG_IMAGE_TASKS.discard)

        return result
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/plan/{plan_id}/sync-performance")
async def sync_performance_v2_endpoint(
    plan_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_active_brand_context),
):
    """Manual-trigger performance feedback sync (PRD §12/§48) — not a live
    webhook, see the plan's out-of-scope list."""
    try:
        synced = await cal_v2_svc.sync_item_performance(plan_id, ctx["user_id"], db, brand_id=ctx["brand_id"])
        return UriResponse.get_single_data_response("calendar_v2_sync", {"synced_items": synced})
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

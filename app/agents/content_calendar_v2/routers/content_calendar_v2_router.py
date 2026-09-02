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


@router.get("/plan")
async def get_plan_v2(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_active_brand_context),
):
    """Return the active 30-day V2 plan, or 404 if none exists."""
    plan = await cal_v2_svc.get_active_plan(ctx["user_id"], db, brand_id=ctx["brand_id"])
    if not plan:
        raise HTTPException(status_code=404, detail="No active Content Calendar V2 plan")
    return UriResponse.get_single_data_response("calendar_plan_v2", plan)


@router.post("/plan/generate")
async def generate_plan_v2_endpoint(
    request: PlanGenerateRequestV2,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    ctx: dict = Depends(get_active_brand_context),
):
    """Generate (or force-regenerate) the 30-day V2 content plan."""
    user_id = ctx["user_id"]
    brand_id = ctx["brand_id"]
    try:
        profile_result = await BrandProfileService.get(user_id, db, brand_id=brand_id)
        raw_profile = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}
        brand = BrandProfileService.to_brand_context(raw_profile) if raw_profile else {}
        plan = await cal_v2_svc.generate_plan_v2(
            user_id=user_id, platforms=request.platforms, brand=brand, db=db,
            force=request.force_regenerate, brand_id=brand_id,
        )
        print(f"[CalendarV2] plan_id={plan.get('plan_id')} generation_method={plan.get('generation_method')} items={len(plan.get('items', []))}")
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
        )
        return UriResponse.get_single_data_response("calendar_plan_v2", updated)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
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

        is_trial_user = await trial_service.has_active_trial(user_id)
        if not is_trial_user:
            has_credits = await credit_service.check_sufficient_credits(user_id)
            if not has_credits:
                return JSONResponse(
                    status_code=402,
                    content={
                        "status": False, "responseCode": 402,
                        "responseMessage": "You've run out of credits. Upgrade to continue.",
                        "responseData": {"credits_remaining": 0, "upgrade_url": "/pricing"},
                    },
                )

        scope = cal_v2_svc._cal_v2_scope(user_id, brand_id)
        plan = await db[cal_v2_svc.COLLECTION].find_one({**scope, "plan_id": plan_id}, {"_id": 0})
        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")
        item = next((it for it in plan["items"] if it["day_index"] == item_index), None)
        if not item:
            raise HTTPException(status_code=404, detail=f"Item {item_index} not found")

        seed_parts = [f"{item.get('title', '')}. {item.get('description', '')}"]
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
        seed_content = "\n".join(seed_parts)

        profile_result = await BrandProfileService.get(user_id, db, brand_id=brand_id)
        raw_profile = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}
        brand_context = BrandProfileService.to_brand_context(raw_profile) if raw_profile else {}
        brand_context["brand_id"] = brand_id

        if item.get("format") == "carousel":
            from app.agents.social_media_manager.services.carousel_generation_service import CarouselGenerationService
            result = await CarouselGenerationService.generate_multi_platform(
                user_id=user_id, seed_content=seed_content, platforms=request.platforms,
                brand_context=brand_context, db=db,
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

            request_id = result.get("responseData", {}).get("request_id")
            credits_to_deduct = len(drafts) if (item.get("format") == "carousel" and drafts) else 1
            if request_id:
                if is_trial_user:
                    await trial_service.deduct_trial_credit(
                        user_id=user_id, campaign_id=request_id, reason="campaign_generation", amount=credits_to_deduct,
                    )
                else:
                    await credit_service.deduct_credit(
                        user_id=user_id, campaign_id=request_id, reason="campaign_generation",
                        retry_count=0, amount=credits_to_deduct,
                    )
                print(f"[CalendarV2] deducted {credits_to_deduct} credit(s) for draft {request_id}")

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

"""
Jane + Ads — demo router.

Two endpoints, no auth (internal evidence UI):
  POST /jane-ads/plan   — run the real decision engine + a mock end-to-end
  GET  /jane-ads/demo   — a self-contained HTML page to click through it

The HTML page is served from the backend so it calls /jane-ads/plan same-origin
(no CORS). It uses the ACTUAL decision engine — nothing is duplicated in JS.
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.core.auth_bearer import JWTBearer
from app.dependencies import get_active_brand_context, get_db_dependency

from . import constants as C
from .adapters.mock import MockAdPlatformAdapter
from .decision_engine import apply_platform_override, plan_campaign
from .instrumentation import InstrumentationService, MongoInstrumentationStore
from .models import (
    CampaignPlan,
    CampaignRequest,
    CreativeContext,
    CreativeKind,
    Goal,
    PlanDecision,
    PlanVariant,
    Platform,
    PurchaseBehaviour,
)
from .payments import JaneAdsPayments
from .store import InMemoryWalletStore, MongoWalletStore
from .wallet import InsufficientFundsError, MinimumTopUpError, WalletService

router = APIRouter(prefix="/jane-ads", tags=["Jane + Ads (demo)"])

# Meta's "Deleted campaigns can't be edited" rejection (code=100). Confirmed live
# against a real stale record — see set_meta_campaign_status, which self-heals on it.
META_DELETED_CAMPAIGN_SUBCODE = 1487566

# Shown (via HTTP 503) whenever the AI backend Jane depends on is unreachable —
# OpenAI over quota, timing out, or down. A plain "try again later", never a
# follow-up question, so the UI can't loop pretending it just needs more info.
_AI_DIFFICULTIES = "We're experiencing some difficulties on our end — please try again in a little while."


def _raise_http_for_meta_error(e: "MetaAPIError") -> None:
    """Meta's ad-account-level rate limit ("too many calls to this ad-account")
    is shared across every caller of that account — heavy testing/usage can trip
    it — and is temporary, unlike a real failure. Surface it as a distinct 429
    with a plain-language message instead of a generic 502, so the caller knows
    to wait rather than assume something is broken."""
    if e.is_rate_limited:
        raise HTTPException(
            status_code=429,
            detail="Meta is briefly rate-limiting this ad account from heavy usage — please wait a few minutes and try again.",
        )
    raise HTTPException(status_code=502, detail=str(e))


def _raise_http_for_tiktok_error(e: "TikTokAdsAPIError") -> None:
    """Mirrors _raise_http_for_meta_error's shape. No live-verified rate-limit
    signal exists yet for TikTok's Marketing API the way Meta's is_rate_limited
    flag does — every failure surfaces as a plain 502 until a real account shows
    which `code` values deserve the same special-cased treatment."""
    raise HTTPException(status_code=502, detail=str(e))


class PlanRequestBody(BaseModel):
    business_name: str = "My Business"
    category: str = ""
    description: str = ""
    goal: Goal = Goal.MESSAGES
    budget_ngn: float = Field(10_000, gt=0)
    has_video: bool = False
    stated_behaviour: Optional[PurchaseBehaviour] = None
    is_new_thing: bool = False
    has_existing_demand: bool = False
    geo: str = ""
    city: str = ""                    # e.g. "Surulere" — enables pin-and-pocket geo
    conversation_cost_ngn: float = Field(500.0, gt=0)
    override_platforms: Optional[list[Platform]] = None   # reject Jane's pick, choose your own
    override_reason: str = ""


async def _plan_and_simulate(
    req: CampaignRequest,
    city: str,
    conversation_cost_ngn: float,
    db: AsyncIOMotorDatabase,
    override_platforms: Optional[list[Platform]] = None,
    override_reason: str = "",
) -> dict:
    """Run the decision engine → geo refinement → a real-wallet/mock-adapter end-to-end.
    Shared by /plan (form) and /understand (natural language). Every call is logged
    (PRD §1.8); an explicit `override_platforms` also logs and applies an override."""
    instrumentation = InstrumentationService(MongoInstrumentationStore(db))
    result = plan_campaign(req, funded_amount_ngn=req.budget_ngn,
                           total_funded_wallets_ngn=req.budget_ngn)
    if result.decision == PlanDecision.ADVISE:
        await instrumentation.record_decision(req.business_id, result)
        return {"decision": "advise", "advice": result.advice.model_dump(),
                "trace": result.advice.trace}

    plan_obj = result.plan
    jane_platforms = [p.platform for p in plan_obj.platforms]
    if override_platforms:
        plan_obj = apply_platform_override(plan_obj, override_platforms)
        await instrumentation.record_override(
            req.business_id, jane_platforms=jane_platforms,
            user_platforms=override_platforms, reason=override_reason,
        )
    await instrumentation.record_decision(
        req.business_id, result, final_platforms=[p.platform for p in plan_obj.platforms],
    )

    # Geo refinement — pin-and-pocket targeting within the chosen platform.
    geo_dump = None
    if city:
        from .geo import geo_for_request
        geo_plan = await geo_for_request(req.business_name, req.category, city,
                                         req.goal, req.description)
        plan_obj.geo = geo_plan
        geo_dump = geo_plan.model_dump()

    # Real wallet + mock adapter: fund, launch, charge each conversation (prepaid-first).
    wallet = WalletService(InMemoryWalletStore())
    await wallet.top_up(req.business_id, req.budget_ngn, reference="demo-topup")
    adapter = MockAdPlatformAdapter(conversation_cost_ngn=conversation_cost_ngn)
    auth = await wallet.authorization_for(req.business_id, req.budget_ngn)
    launch = await adapter.launch_campaign(plan_obj, auth)
    delivered = await adapter.poll_conversations(launch.campaign_id)

    charged, prices = 0, []
    for conv in delivered:
        try:
            txn = await wallet.charge_conversation(
                req.business_id, campaign_id=launch.campaign_id, ad_id=conv.ad_id,
                actual_platform_cost_ngn=conversation_cost_ngn,
            )
            charged += 1
            prices.append(-txn.amount_ngn)
        except InsufficientFundsError:
            break
    balance_after = await wallet.get_balance(req.business_id)
    spent = round(req.budget_ngn - balance_after, 2)

    return {
        "decision": "plan",
        "goal": plan_obj.goal.value,
        "behaviour": plan_obj.behaviour.value,
        "explanation": plan_obj.explanation,
        "trace": plan_obj.trace,
        "per_business_cap_ngn": plan_obj.per_business_cap_ngn,
        "account_cap_ngn": plan_obj.account_cap_ngn,
        "geo": geo_dump,
        "platforms": [p.model_dump() for p in plan_obj.platforms],
        "overridden": bool(override_platforms),
        "jane_recommended_platforms": [p.value for p in jane_platforms] if override_platforms else None,
        "simulation": {
            "conversations_delivered": len(delivered),
            "conversations_charged": charged,
            "prepaid_stopped": charged < len(delivered),
            "price_min_ngn": min(prices) if prices else 0,
            "price_max_ngn": max(prices) if prices else 0,
            "wallet_before_ngn": req.budget_ngn,
            "wallet_after_ngn": balance_after,
            "spent_ngn": spent,
            "cap_respected": spent <= plan_obj.per_business_cap_ngn,
        },
    }



async def _retrieve_for_plan_generation(
    db, parsed, business_id: Optional[str] = None
) -> Optional["RetrievalResult"]:
    """ASC-SPEC-01 v2 §9.2 — retrieval fires AFTER platform selection, so it is scoped
    to the platforms this build will actually use; retrieving earlier returns records
    for platforms that will never run.

    Best-effort by design: the corpus informs plans, it does not generate them (§9.1),
    so a corpus outage must degrade to today's model-prior behaviour rather than block
    a client mid-conversation. An empty result is NOT swallowed — it is logged as a
    coverage gap, because which stage/tier/platform combinations return nothing is the
    seeding roadmap (§8.4).
    """
    try:
        from .entities import ConsumedBy, StrategyPlatform
        from .retrieval import (
            BudgetContext, BusinessProfile, RetrievalRequest, gap_record, retrieve,
        )
        from .store import MongoCoverageGapStore, MongoStrategyStore

        budget_ngn = float(getattr(parsed, "budget_ngn", 0) or 0)
        if budget_ngn <= 0:
            return None
        duration = int(getattr(parsed, "duration_days", 0) or C.DEFAULT_CAMPAIGN_DAYS)
        daily = budget_ngn / (1 + C.VAT_RATE) / max(duration, 1)

        # Sustained capacity is a SEPARATE question from what this campaign can
        # spend (§5.2). Unknown fails closed: every record needing more than one
        # day of continuous spend is excluded rather than optimistically allowed.
        sustained_ngn, sustained_known = None, False
        if business_id:
            from .store import MongoWalletStore
            from .wallet import WalletService
            sustained_ngn, sustained_known = await WalletService(
                MongoWalletStore(db)
            ).sustained_daily_ngn(business_id)

        req = RetrievalRequest(
            stage=ConsumedBy.PLAN_GENERATION,
            # Jane Ads runs on the pooled Meta account; TikTok/Google adapters exist
            # but no live ads path reaches them yet. Sourced here rather than assumed
            # downstream so the day a second platform ships, this is the one edit.
            platforms=[StrategyPlatform.META],
            budget=BudgetContext(
                daily_spend_ngn=daily,
                budget_tier=_budget_tier(budget_ngn),
                sustained_daily_ngn=sustained_ngn,
                sustained_known=sustained_known,
            ),
            profile=BusinessProfile(
                has_video_asset=bool(getattr(parsed, "has_video", False)),
                # outcome capture is ENG step 9, unbuilt — records requiring it
                # correctly stay excluded until it exists.
                records_outcomes=False,
            ),
        )
        result = retrieve(await MongoStrategyStore(db).fetch_approved(), req)
        if not result.records:
            await MongoCoverageGapStore(db).log_gap(gap_record(req))
        return result
    except Exception as e:                       # noqa: BLE001 — never block a build
        print(f"[oneshot] corpus retrieval skipped: {e}", flush=True)
        return None



async def _structure_notes(db, budget_ngn: float, duration_days: int = 0) -> list[dict]:
    """Corpus findings for campaign structure (spec §12), attached to the stored plan
    as auditable notes — NOT fed into the build.

    §12 is explicit that tier rules take precedence over corpus records: a record
    proposing a structure the tier forbids should never have passed the filter, and
    one reaching this point that violates tier rules is a filter defect. The
    structure decisions themselves (ABO/CBO, ad-set count, pacing) are deterministic
    arithmetic in decision_engine, so the corpus records here are review material for
    the operator, not an input the engine should follow.
    """
    try:
        from .entities import ConsumedBy, StrategyPlatform
        from .retrieval import BudgetContext, BusinessProfile, RetrievalRequest, retrieve
        from .store import MongoStrategyStore
        if not budget_ngn or budget_ngn <= 0:
            return []
        days = duration_days or C.DEFAULT_CAMPAIGN_DAYS
        daily = budget_ngn / (1 + C.VAT_RATE) / max(days, 1)
        req = RetrievalRequest(
            stage=ConsumedBy.CAMPAIGN_STRUCTURE,
            platforms=[StrategyPlatform.META],
            budget=BudgetContext(daily_spend_ngn=daily, budget_tier=_budget_tier(budget_ngn)),
            profile=BusinessProfile(),
        )
        res = retrieve(await MongoStrategyStore(db).fetch_approved(), req)
        return [
            {"record_id": r.strategy_id, "version": r.version,
             "claim": r.claim, "score": res.scores.get(r.strategy_id, 0.0)}
            for r in res.records
        ]
    except Exception as e:                       # noqa: BLE001
        print(f"[oneshot] structure notes skipped: {e}", flush=True)
        return []


def _budget_tier(budget_ngn: float) -> int:
    """Spec §5.4 — tier gates structure, separately from the daily-spend floor."""
    if budget_ngn < 15_000:
        return 1
    if budget_ngn < 50_000:
        return 2
    if budget_ngn < 250_000:
        return 3
    return 4


@router.post("/plan")
async def plan(
    body: PlanRequestBody,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
) -> dict:
    """Form path: structured inputs → plan + geo + wallet simulation."""
    req = CampaignRequest(
        business_id="demo",
        business_name=body.business_name,
        category=body.category,
        description=body.description,
        goal=body.goal,
        budget_ngn=body.budget_ngn,
        creative=CreativeContext(
            kind=CreativeKind.VIDEO if body.has_video else CreativeKind.IMAGE,
            has_video=body.has_video,
        ),
        stated_behaviour=body.stated_behaviour,
        is_new_thing=body.is_new_thing,
        has_existing_demand=body.has_existing_demand,
        geo=body.geo,
    )
    return await _plan_and_simulate(
        req, body.city, body.conversation_cost_ngn, db,
        override_platforms=body.override_platforms, override_reason=body.override_reason,
    )


class UnderstandBody(BaseModel):
    message: str
    business_name: str = ""
    category: str = ""
    conversation_cost_ngn: float = Field(500.0, gt=0)


@router.post("/understand")
async def understand(
    body: UnderstandBody,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
) -> dict:
    """Natural-language path: Jane reads a plain-English message, extracts the goal/
    budget/behaviour/city herself, then runs the same plan. Asks a follow-up if the
    budget is missing rather than guessing."""
    from .nl import parse_message, to_campaign_request, NlUnavailableError
    try:
        parsed = await parse_message(body.message, body.business_name, body.category)
    except NlUnavailableError:
        raise HTTPException(status_code=503, detail=_AI_DIFFICULTIES)
    req = to_campaign_request(parsed, business_id="demo")
    if req is None:
        return {
            "decision": "need_more",
            "understood": parsed.model_dump(),
            "question": parsed.clarify or "How much would you like to spend?",
        }
    result = await _plan_and_simulate(req, parsed.city, body.conversation_cost_ngn, db)
    result["understood"] = parsed.model_dump()
    return result


class CreativeBody(BaseModel):
    business_name: str = ""
    category: str = ""
    goal: str = "messages"
    description: str = ""
    city: str = ""     # grounds the image in the real place — else a generic look


@router.post("/creative")
async def creative(body: CreativeBody) -> dict:
    """Anonymous demo path: Jane writes copy + generates a generic (no-brand) image
    and attaches the WhatsApp CTA. Falls back to copy-only if generation fails.
    Real, brand-aware generation is the authenticated /creative/for-brand below."""
    from .creative import generate_ad_creative
    ad = await generate_ad_creative(body.business_name, body.category, body.goal,
                                    body.description, city=body.city)
    return ad.model_dump()


# ── Authenticated ad creative — the brand playbook, uploads, and drafts ───────
# Mirrors how normal content creation already works on the platform (PRD Part D2):
# Jane generates via the SAME brand-aware engine, the user can upload their own
# media, or reuse an existing draft they already liked. Always writes fresh copy
# and always attaches the WhatsApp CTA.

class CreativeForBrandBody(BaseModel):
    business_name: str = ""
    category: str = ""
    goal: str = "messages"
    description: str = ""
    city: str = ""                     # grounds the GENERATE image in the real place
    source: str = "generate"           # generate | upload | draft | recomposite
    reference_image_url: str = ""      # required for source=upload/recomposite
    is_video: bool = False             # is reference_image_url a video? (from /creative/upload)
    draft_id: str = ""                 # required for source=draft


@router.post("/creative/for-brand")
async def creative_for_brand(
    body: CreativeForBrandBody,
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
) -> dict:
    """Generate/assemble an ad creative for the caller's real brand — pulls the brand
    playbook (colours, voice, fonts) so ads look like the brand, not a template."""
    from .creative import creative_from_draft, creative_from_recomposite, creative_from_upload, generate_ad_creative
    user_id = brand_ctx["user_id"]
    brand_id = brand_ctx.get("brand_id")

    if body.source == "upload":
        if not body.reference_image_url:
            raise HTTPException(status_code=400, detail="reference_image_url is required for source=upload")
        ad = await creative_from_upload(
            body.business_name, body.category, body.reference_image_url, body.goal,
            body.description, user_id=user_id, db=db, brand_id=brand_id,
            is_video=body.is_video, city=body.city,
        )
    elif body.source == "recomposite":
        if not body.reference_image_url:
            raise HTTPException(status_code=400, detail="reference_image_url is required for source=recomposite")
        ad = await creative_from_recomposite(
            body.business_name, body.category, body.reference_image_url, body.goal,
            body.description, user_id=user_id, db=db, brand_id=brand_id, city=body.city,
        )
    elif body.source == "draft":
        if not body.draft_id:
            raise HTTPException(status_code=400, detail="draft_id is required for source=draft")
        ad = await creative_from_draft(
            body.business_name, body.category, body.draft_id, user_id, db,
            goal=body.goal, brand_id=brand_id, city=body.city,
        )
        if ad is None:
            raise HTTPException(status_code=404, detail="Draft not found or has no image")
    else:
        ad = await generate_ad_creative(
            body.business_name, body.category, body.goal, body.description,
            user_id=user_id, db=db, brand_id=brand_id, city=body.city,
        )
    return ad.model_dump()


@router.get("/creative/drafts")
async def creative_drafts(
    brand_ctx: dict = Depends(get_active_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    limit: int = 10,
) -> dict:
    """List the caller's recent drafts (with images) to pick from — 'maybe the user
    saw something they liked there' — for the source=draft ad creative path."""
    from .creative import list_recent_drafts
    drafts = await list_recent_drafts(brand_ctx["user_id"], db, brand_ctx.get("brand_id"), limit)
    return {"drafts": drafts}


_UPLOAD_IMAGE_TYPES = {
    "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp",
}
_UPLOAD_VIDEO_TYPES = {
    "video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm", "video/x-m4v": "m4v",
}
_MAX_UPLOAD_IMAGE_BYTES = 8 * 1024 * 1024     # 8 MB
_MAX_UPLOAD_VIDEO_BYTES = 100 * 1024 * 1024   # 100 MB — short vertical ad clips


@router.post("/creative/upload")
async def creative_upload(
    file: UploadFile = File(...),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Upload the user's own photo OR video for the source=upload ad creative path.
    Returns a hosted URL (+ is_video) to pass to /creative/for-brand."""
    is_video = file.content_type in _UPLOAD_VIDEO_TYPES
    ext = _UPLOAD_VIDEO_TYPES.get(file.content_type) or _UPLOAD_IMAGE_TYPES.get(file.content_type)
    if not ext:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {file.content_type}. Use PNG/JPG/WEBP or MP4/MOV/WEBM.",
        )
    contents = await file.read()
    max_bytes = _MAX_UPLOAD_VIDEO_BYTES if is_video else _MAX_UPLOAD_IMAGE_BYTES
    if len(contents) > max_bytes:
        raise HTTPException(status_code=400, detail=f"File must be under {max_bytes // (1024*1024)} MB.")

    from .creative import _upload_bytes_to_cloudinary
    import uuid as _uuid
    url = await _upload_bytes_to_cloudinary(
        contents, f"upload-{_uuid.uuid4().hex[:12]}",
        resource_type="video" if is_video else "image",
        ext=ext, content_type=file.content_type,
    )
    if not url:
        raise HTTPException(status_code=502, detail="Upload failed, please try again.")
    return {"url": url, "is_video": is_video}


# ── Real wallet funding via Squad ─────────────────────────────────────────────

class TopUpBody(BaseModel):
    business_id: str
    amount_ngn: float = Field(..., gt=0)
    email: str


@router.post("/wallet/topup")
async def wallet_topup(
    body: TopUpBody,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    _token: dict = Depends(JWTBearer()),
) -> dict:
    """Start a real Squad checkout to fund a business's ad wallet. Returns the
    checkout URL the customer opens to pay. Nothing is credited until Squad confirms."""
    try:
        result = await JaneAdsPayments(db).initialize_topup(
            body.business_id, body.amount_ngn, body.email
        )
    except MinimumTopUpError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not start payment: {e}")
    return {"status": "checkout_created", **result}


@router.get("/wallet/topup/{reference}/verify")
async def wallet_topup_verify(
    reference: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    _token: dict = Depends(JWTBearer()),
) -> dict:
    """Verify a top-up with Squad and credit the wallet if it succeeded (idempotent)."""
    return await JaneAdsPayments(db).confirm_topup(reference)


@router.post("/wallet/webhook")
async def wallet_webhook(
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
) -> dict:
    """Squad → us. Credits the wallet on a successful top-up (idempotent). No JWT —
    Squad calls this directly; only references we created are acted on."""
    payload = await request.json()
    return await JaneAdsPayments(db).handle_webhook(payload)


@router.get("/wallet/{business_id}/balance")
async def wallet_balance(
    business_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    _token: dict = Depends(JWTBearer()),
) -> dict:
    """Current balance + recent ledger entries for a business's ad wallet."""
    wallet = WalletService(MongoWalletStore(db))
    balance = await wallet.get_balance(business_id)
    txns = await wallet.list_transactions(business_id)
    return {
        "business_id": business_id,
        "balance_ngn": balance,
        "transactions": [t.model_dump(mode="json") for t in txns[-20:]],
    }


# ── Chat history (per brand) ──────────────────────────────────────────────────
# The Campaigns chat is entirely client-orchestrated (the frontend calls plan/launch
# endpoints itself and renders its own message list) — there's no single backend call
# that "is" a chat turn to hook persistence into. So the frontend explicitly saves each
# message as it's added, and loads the transcript back on mount. Keyed by brand, not
# user, so it matches how everything else in Jane Ads is scoped (a shared brand inbox,
# not a personal one) and survives a user switching devices.

CHAT_HISTORY_COLLECTION = "jane_ads_chat_messages"


@router.get("/chat/history")
async def jane_chat_history(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """The active brand's saved Campaigns chat transcript, oldest first."""
    brand_id = brand_ctx.get("brand_id")
    if not brand_id:
        return {"messages": []}
    docs = await (db[CHAT_HISTORY_COLLECTION]
                  .find({"brand_id": brand_id}, {"_id": 0, "brand_id": 0, "user_id": 0})
                  .sort("created_at", 1).to_list(length=500))
    return {"messages": docs}


class ChatMessageBody(BaseModel):
    message_id: str
    role: str            # "user" | "jane"
    kind: str             # "text" | "result"
    text: str = ""
    result: Optional[dict] = None   # the full LaunchFromMessageResult, for kind="result"
    thread_id: str = ""             # which campaign thread this belongs to (Tier E)


@router.put("/chat/history/{message_id}")
async def jane_chat_save_message(
    message_id: str,
    body: ChatMessageBody,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Save (or update) one chat message. PUT + upsert-by-id so this covers both a
    brand-new message AND the existing message being edited in place — e.g. a plan
    card that flips from 'planned' to 'launched' once the user confirms it reuses the
    SAME message_id, so saving it again here just updates that one saved row instead
    of creating a duplicate. When a thread_id is given (Tier E), the message is tagged
    to that thread and the thread's preview/status/title are kept current."""
    from datetime import datetime, timezone
    from .threads import touch_thread, title_from_message

    brand_id = brand_ctx.get("brand_id")
    if not brand_id:
        raise HTTPException(status_code=400, detail="No active brand to save chat history for.")
    now = datetime.now(timezone.utc)
    await db[CHAT_HISTORY_COLLECTION].update_one(
        {"brand_id": brand_id, "message_id": message_id},
        {"$set": {
            "message_id": message_id, "brand_id": brand_id, "user_id": brand_ctx.get("user_id"),
            "role": body.role, "kind": body.kind, "text": body.text, "result": body.result,
            "thread_id": body.thread_id, "updated_at": now,
        }, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    if body.thread_id:
        # Keep the thread record fresh: title from the first user line, status as the
        # campaign progresses, preview from the latest text.
        stage = (body.result or {}).get("stage") if body.kind == "result" else None
        status = "launched" if stage == "launched" else "planned" if stage == "planned" else None
        title = title_from_message(body.text) if body.role == "user" else None
        preview = body.text or (f"Plan: {stage}" if stage else None)
        await touch_thread(db, brand_id, body.thread_id, title=title, status=status, preview_text=preview)
    return {"ok": True}


# ── Campaign threads (Tier E) ─────────────────────────────────────────────────
# Each campaign conversation is its own resumable thread. The rail lists them; opening
# one loads its messages; a launched one can be duplicated into a fresh draft.

class NewThreadBody(BaseModel):
    title: str = "New campaign"


@router.get("/threads")
async def jane_list_threads(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """This brand's campaign threads, most-recently-active first."""
    from .threads import list_threads
    return {"threads": await list_threads(db, brand_ctx.get("brand_id"))}


@router.post("/threads")
async def jane_create_thread(
    body: NewThreadBody,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Start a fresh campaign thread ('+ New')."""
    from .threads import create_thread
    brand_id = brand_ctx.get("brand_id")
    if not brand_id:
        raise HTTPException(status_code=400, detail="No active brand.")
    return await create_thread(db, brand_id, brand_ctx.get("user_id"), body.title)


@router.get("/threads/{thread_id}/history")
async def jane_thread_history(
    thread_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """The messages in one thread, oldest first."""
    from .threads import thread_history
    return {"messages": await thread_history(db, brand_ctx.get("brand_id"), thread_id)}


@router.post("/threads/{thread_id}/duplicate")
async def jane_duplicate_thread(
    thread_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Clone a thread's launched campaign into a NEW draft thread, pre-filled with a
    plain-English brief the user can tweak and relaunch. Returns the new thread plus the
    seed message the frontend sends to rebuild the plan."""
    from .threads import create_thread, seed_message_from_campaign, title_from_message
    brand_id = brand_ctx.get("brand_id")
    if not brand_id:
        raise HTTPException(status_code=400, detail="No active brand.")
    camp = await db["jane_ads_meta_campaigns"].find_one(
        {"brand_id": brand_id, "thread_id": thread_id}, sort=[("created_at", -1)])
    if not camp:
        # Fall back to the most recent campaign for this brand if the thread isn't tagged
        # (legacy campaigns created before threads existed).
        camp = await db["jane_ads_meta_campaigns"].find_one(
            {"$or": [{"brand_id": brand_id}, {"business_id": brand_id}]}, sort=[("created_at", -1)])
    if not camp:
        raise HTTPException(status_code=404, detail="No launched campaign found to duplicate.")
    seed = seed_message_from_campaign(camp)
    thread = await create_thread(db, brand_id, brand_ctx.get("user_id"), title_from_message(seed))
    return {"thread": thread, "seed_message": seed}


@router.delete("/threads/{thread_id}")
async def jane_delete_thread(
    thread_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Remove a conversation from the thread rail. Never touches the brand's actual
    launched campaigns (jane_ads_meta_campaigns) — those keep running in 'My Campaigns'
    regardless of whether their originating chat is deleted; this only clears clutter."""
    from .threads import delete_thread
    brand_id = brand_ctx.get("brand_id")
    if not brand_id:
        raise HTTPException(status_code=400, detail="No active brand.")
    deleted = await delete_thread(db, brand_id, thread_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Thread not found.")
    return {"deleted": True}


# ── Brand WhatsApp number (where leads route) ─────────────────────────────────
# Ads link to wa.me/<this number>, so chats land in the brand's own WhatsApp, not the
# shared Page's inbox. Stored once per brand and reused on every campaign.

class WhatsAppBody(BaseModel):
    whatsapp_number: str


@router.get("/whatsapp")
async def jane_get_whatsapp(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """The active brand's saved WhatsApp number (leads route here), or '' if unset."""
    from .whatsapp import get_brand_whatsapp
    return {"whatsapp_number": await get_brand_whatsapp(db, brand_ctx.get("brand_id"))}


@router.put("/whatsapp")
async def jane_set_whatsapp(
    body: WhatsAppBody,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Set the active brand's WhatsApp number. Normalizes to the wa.me digits form and
    rejects anything that can't be a real number."""
    from .whatsapp import normalize_wa_number, set_brand_whatsapp
    brand_id = brand_ctx.get("brand_id")
    if not brand_id:
        raise HTTPException(status_code=400, detail="No active brand to save a WhatsApp number for.")
    number = normalize_wa_number(body.whatsapp_number)
    if not number:
        raise HTTPException(status_code=400, detail="That WhatsApp number doesn't look right — please type it in full, e.g. 0803 123 4567.")
    await set_brand_whatsapp(db, brand_id, number)
    return {"whatsapp_number": number}


# ── Per-brand Meta ads connection (Per-Brand Page Connection plan) ───────────
# Distinct from the /whatsapp pair above, which is the WhatsApp number Jane's own
# lead-notification copy references — this is the ads-specific connection gating
# campaign launch: which Facebook Page, ads permission health, and whether THAT
# page's WhatsApp is linked (required for a real Click-to-WhatsApp destination).

@router.get("/meta-connection/status")
async def jane_meta_connection_status(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """The active brand's connection state — one of the six explicit states
    (never inferred from a single boolean), so the frontend can render the exact
    matching prompt. `connect_url` is only meaningful for states that need the
    OAuth grant (NONE/CONTENT_ONLY/EXPIRED/NO_PAGE)."""
    from .ads_connection import resolve_connection_state

    state, ads = await resolve_connection_state(db, brand_ctx.get("user_id"), brand_ctx.get("brand_id"))
    return {
        "state": state.value,
        "page_name": (ads or {}).get("account_name", ""),
        "whatsapp_number": (ads or {}).get("whatsapp_number", ""),
        "connect_url": "/social-media/connect/facebook-ads/initiate",
        # Only meaningful when state == "expired" — which specific ads permissions
        # weren't granted, so the client knows exactly what to re-check on Facebook's
        # consent screen instead of just "reconnect and hope" (confirmed live: a token
        # can be valid but still missing ads_management/pages_manage_ads etc.).
        "missing_scopes": (ads or {}).get("_missing_scopes", []),
        # Also only meaningful when state == "expired": set instead of missing_scopes
        # when the OAuth scopes/token are fine but URI's Business Manager never
        # actually got ADVERTISE access to the page (e.g. Meta rejects the share as
        # a "duplicated asset") — a different failure that needs a different fix
        # (resolving the asset conflict in Meta Business Settings), not just
        # re-consenting to the same scopes.
        "business_manager_error": (ads or {}).get("_business_manager_error", ""),
    }


class MetaConnectionWhatsAppBody(BaseModel):
    whatsapp_number: str


@router.put("/meta-connection/whatsapp")
async def jane_meta_connection_set_whatsapp(
    body: MetaConnectionWhatsAppBody,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Link the brand's WhatsApp number to their ads connection — the step that
    moves ADS_NO_WHATSAPP to READY. Does not itself prove the number is linked
    to the Page in Meta (that's a manual OTP step in Meta's own Page Settings);
    the next real ad-set create surfaces Meta's own rejection if it isn't."""
    from .ads_connection import AdsConnectionRequired, set_whatsapp_number

    try:
        number = await set_whatsapp_number(db, brand_ctx.get("user_id"), brand_ctx.get("brand_id"), body.whatsapp_number)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except AdsConnectionRequired as e:
        raise HTTPException(status_code=409, detail=f"meta_connection_{e.state.value}")
    return {"whatsapp_number": number}


# ── Brand-scoped wallet (what the app UI calls) ───────────────────────────────
# The two endpoints above take an explicit business_id (internal/demo use). The UI
# instead uses these, which derive the wallet key from the active brand context —
# the SAME id _build_campaign_plan spends against — so a user can only ever see and
# fund their own brand's wallet (no business_id in the request to tamper with), and
# the balance shown here is guaranteed to be the one a launch will actually check.

@router.get("/wallet")
async def jane_wallet(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """The active brand's ad-wallet balance + recent ledger, for the wallet view."""
    business_id = brand_ctx.get("brand_id")
    if not business_id:
        return {"balance_ngn": 0.0, "currency": "NGN",
                "min_topup_ngn": C.MIN_TOPUP_NGN, "transactions": []}
    wallet = WalletService(MongoWalletStore(db))
    balance = await wallet.get_balance(business_id)
    txns = await wallet.list_transactions(business_id)
    return {
        "balance_ngn": balance,
        "currency": "NGN",
        "min_topup_ngn": C.MIN_TOPUP_NGN,
        # Newest first — the ledger reads top-down like a bank statement.
        "transactions": [t.model_dump(mode="json") for t in reversed(txns[-20:])],
    }


class JaneWalletFundBody(BaseModel):
    amount_ngn: float = Field(..., gt=0)


@router.post("/wallet/fund")
async def jane_wallet_fund(
    body: JaneWalletFundBody,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
    token: dict = Depends(JWTBearer()),
) -> dict:
    """Start a Squad checkout to fund the active brand's ad wallet. Returns the
    checkout URL for the client to open. Nothing is credited until Squad confirms
    (via the callback's verify call, or the webhook — both idempotent)."""
    business_id = brand_ctx.get("brand_id")
    if not business_id:
        raise HTTPException(status_code=400, detail="No active brand to fund a wallet for.")
    email = (token.get("claims", {}) or {}).get("email", "")
    if not email:
        raise HTTPException(status_code=400, detail="No billing email on your account.")
    try:
        result = await JaneAdsPayments(db).initialize_topup(business_id, body.amount_ngn, email)
    except MinimumTopUpError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not start payment: {e}")
    return {"status": "checkout_created", **result}


# ── Google Ads connection (additive — does not touch the Meta/force-to-Meta code
# below, or any of the /meta/* endpoints) ──────────────────────────────────────
# Deliberately under /jane-ads/google/*, never generalizing the existing /jane-ads/
# meta/* paths (those assume one platform on purpose — see the naming convention
# note in the Google adapter design doc). Google Ads has two connection paths (link
# an existing account under URI's MCC, or create a fresh one) — see
# google_ads_connection.py for the full state machine.

@router.get("/google/admin/connect/initiate")
async def jane_google_ads_admin_connect_initiate():
    """One-time, ops-only: redirects to Google's OAuth consent page so URI's own
    admin identity (whoever administers GOOGLE_ADS_MCC_CUSTOMER_ID) can grant Ads-API
    access. Not brand-scoped, not linked from the app UI — visit this URL directly,
    logged into the Google account that has admin access on the MCC. Every real
    Google Ads REST call authenticates as whichever identity does this once (see
    google_ads_connection.py's module docstring for why: Google's agency model
    requires the calling identity to already be a user on the manager account, which
    a brand's own personal Google login never is)."""
    import urllib.parse
    from app.core.config import settings

    client_id = settings.GOOGLE_ADS_CLIENT_ID
    if not client_id:
        raise HTTPException(status_code=500, detail="GOOGLE_ADS_CLIENT_ID not configured")

    _base = (settings.PUBLIC_API_URL or settings.URI_GATEWAY_BASE_API_URL).rstrip("/")
    redirect_uri = f"{_base}/jane-ads/google/admin/connect/callback"
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "https://www.googleapis.com/auth/adwords",
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return RedirectResponse(auth_url)


@router.get("/google/admin/connect/callback")
async def jane_google_ads_admin_connect_callback(
    code: Optional[str] = None,
    error: Optional[str] = None,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Exchanges the code and stores it as THE single admin connection (upsert on a
    fixed id — there is only ever one). Plain HTML response, not a redirect back into
    the app: this isn't part of any brand-facing flow, there's nowhere in the UI to
    land."""
    from app.core.config import settings
    from .google_ads_connection import exchange_code_for_tokens, save_admin_connection, GoogleAdsConnectionError

    if error:
        return HTMLResponse(f"<p>Google Ads admin connect failed: {error}</p>", status_code=400)
    if not code:
        return HTMLResponse("<p>Google Ads admin connect failed: missing code.</p>", status_code=400)

    _base = (settings.PUBLIC_API_URL or settings.URI_GATEWAY_BASE_API_URL).rstrip("/")
    redirect_uri = f"{_base}/jane-ads/google/admin/connect/callback"

    try:
        tokens = await exchange_code_for_tokens(code, redirect_uri)
    except GoogleAdsConnectionError as e:
        return HTMLResponse(f"<p>Google Ads admin connect failed: {e}</p>", status_code=502)

    if not tokens.get("refresh_token"):
        # Google only returns this on first consent (or with prompt=consent, always
        # set above) — without it every call fails again the moment this access
        # token expires, so this is a hard failure, not a soft warning.
        return HTMLResponse(
            "<p>Google didn't return a refresh_token — this Google account may have "
            "already granted this app access before. Revoke access at "
            "myaccount.google.com/permissions and try again.</p>",
            status_code=502,
        )

    await save_admin_connection(db, tokens)
    return HTMLResponse("<p>Google Ads admin account connected. You can close this tab.</p>")


@router.post("/google/connect")
async def jane_google_ads_connect(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Marks this brand as wanting Google Ads. No OAuth roundtrip and nothing for the
    brand to personally authorize — Google Ads API calls authenticate as URI's own
    admin identity, never the brand's own Google login (see
    google_ads_connection.py's module docstring). Idempotent: safe to call again if a
    connection already exists for this brand."""
    import uuid
    from datetime import datetime, timezone
    from .google_ads_connection import get_any_google_ads_connection

    if await get_any_google_ads_connection(db, brand_ctx.get("user_id"), brand_ctx.get("brand_id")):
        return {"status": "already_connected"}

    now = datetime.now(timezone.utc)
    await db["social_connections"].insert_one({
        "id": f"gads_{uuid.uuid4().hex[:12]}",
        "user_id": brand_ctx.get("user_id"),
        "brand_id": brand_ctx.get("brand_id"),
        "platform": "google_ads",
        "connected_via": "google_ads_direct",
        "customer_id": "",
        "manager_link_status": "none",
        "account_name": "",
        "created_account_by_uri": False,
        "connection_status": "active",
        "connected_at": now,
        "updated_at": now,
    })
    return {"status": "connected"}


class GoogleAdsLinkExistingBody(BaseModel):
    customer_id: str


@router.post("/google/connect/link-existing-account")
async def jane_google_ads_link_existing(
    body: GoogleAdsLinkExistingBody,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Path (a): client already has a Google Ads account — send a manager-link
    invitation. On the known 'already linked to another manager' friction, returns
    a specific, actionable message instead of a generic failure."""
    from .google_ads_connection import AdsConnectionRequired, GoogleAdsConnectionError, request_manager_link

    try:
        result = await request_manager_link(
            db, brand_ctx.get("user_id"), brand_ctx.get("brand_id"), body.customer_id,
        )
    except AdsConnectionRequired as e:
        raise HTTPException(status_code=409, detail=f"google_ads_connection_{e.state.value}")
    except GoogleAdsConnectionError as e:
        print(f"⚠️  Google Ads REST failure: {e}")
        raise HTTPException(status_code=502, detail=str(e))

    if result.get("manager_link_status") == "refused":
        return {
            "manager_link_status": "refused",
            "detail": (
                "This Google Ads account is already linked to another manager. "
                "Ask the client to remove that link in their account "
                "(Admin → Access and security → Managers) before reconnecting."
            ),
        }
    return result


class GoogleAdsCreateAccountBody(BaseModel):
    account_name: str


@router.post("/google/connect/create-account")
async def jane_google_ads_create_account(
    body: GoogleAdsCreateAccountBody,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Path (b): client has no Google Ads account — create one fresh under URI's
    MCC (auto-linked, no accept step needed)."""
    from .google_ads_connection import AdsConnectionRequired, GoogleAdsConnectionError, create_client_account_under_mcc

    try:
        return await create_client_account_under_mcc(
            db, brand_ctx.get("user_id"), brand_ctx.get("brand_id"), body.account_name,
        )
    except AdsConnectionRequired as e:
        raise HTTPException(status_code=409, detail=f"google_ads_connection_{e.state.value}")
    except GoogleAdsConnectionError as e:
        print(f"⚠️  Google Ads REST failure: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@router.get("/google/connection/status")
async def jane_google_ads_connection_status(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """The active brand's Google Ads connection state. Always returns 200 with the
    state in the body — this is a pure status read, not a pre-flight gate inside a
    build flow, so there's never a reason to raise here (unlike
    resolve_customer_id_for_launch, which does)."""
    from .google_ads_connection import resolve_connection_state
    from .whatsapp import get_brand_whatsapp

    state, conn = await resolve_connection_state(
        db, brand_ctx.get("user_id"), brand_ctx.get("brand_id"),
    )
    wa_number = await get_brand_whatsapp(db, brand_ctx.get("brand_id"))
    return {
        "state": state.value,
        "account_name": (conn or {}).get("account_name", ""),
        "customer_id": (conn or {}).get("customer_id", ""),
        "whatsapp_number": wa_number,
    }


@router.get("/instrumentation/{business_id}")
async def instrumentation_log(
    business_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    _token: dict = Depends(JWTBearer()),
    limit: int = 100,
) -> dict:
    """Decision + override history for a business (PRD §1.8) — to measure and
    improve Jane: how often she's overridden, and on what kind of call."""
    instrumentation = InstrumentationService(MongoInstrumentationStore(db))
    decisions = await instrumentation.decisions_for(business_id, limit)
    overrides = await instrumentation.overrides_for(business_id, limit)
    return {
        "business_id": business_id,
        "decisions": [d.model_dump(mode="json") for d in decisions],
        "overrides": [o.model_dump(mode="json") for o in overrides],
    }


def _is_ads_admin(token: dict) -> bool:
    """True if the caller's email is on the billing-report allowlist
    (config JANE_ADS_ADMIN_EMAILS). Empty allowlist = nobody (report disabled)."""
    from app.core.config import settings

    allowed = {e.strip().lower() for e in (settings.JANE_ADS_ADMIN_EMAILS or "").split(",") if e.strip()}
    if not allowed:
        return False
    email = ((token.get("claims", {}) or {}).get("email") or "").lower()
    return email in allowed


def _require_ads_admin(token: dict) -> None:
    """Gate the all-customers billing report — 503 if unconfigured, 403 if not allowed,
    so it can never leak every customer's financials by default."""
    from app.core.config import settings

    if not (settings.JANE_ADS_ADMIN_EMAILS or "").strip():
        raise HTTPException(status_code=503, detail="Billing report is not configured.")
    if not _is_ads_admin(token):
        raise HTTPException(status_code=403, detail="Not authorized for the billing report.")


@router.get("/admin/access")
async def admin_access(token: dict = Depends(JWTBearer())) -> dict:
    """Whether the logged-in user may see the billing report — so the UI can show or
    hide the admin view without a hardcoded email list of its own."""
    return {"allowed": _is_ads_admin(token)}


@router.get("/admin/billing-summary")
async def admin_billing_summary(
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    format: str = "json",
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer()),
):
    """Per-customer ad spend vs. what we billed them, plus grand totals — the finance
    view. For each customer: real ad spend (what Meta charged us), billed (what we
    charged their wallet), and margin (our service fee earned). Optional `from_date`/
    `to_date` (YYYY-MM-DD) window; `format=csv` returns a spreadsheet download.
    Admin-only (JANE_ADS_ADMIN_EMAILS)."""
    from datetime import datetime, timezone

    from .reporting import summarize_billing, to_csv

    _require_ads_admin(token)

    query: dict = {"type": "ad_spend"}
    date_range: dict = {}
    for key, raw in (("$gte", from_date), ("$lte", to_date)):
        if raw:
            try:
                dt = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"Bad date '{raw}' — use YYYY-MM-DD.")
            date_range[key] = dt
    if date_range:
        query["created_at"] = date_range

    txns = await db["jane_ads_transactions"].find(query, {"_id": 0}).to_list(length=100000)
    summary = summarize_billing(txns)

    # Best-effort human label per customer (email of the owning personal brand), so
    # the report isn't just opaque ids. Never fails the report if a lookup misses.
    for row in summary["per_user"]:
        bid = row["business_id"]
        label = ""
        try:
            if bid.startswith("brnd_personal_"):
                user = await db["users"].find_one({"userId": bid[len("brnd_personal_"):]}, {"email": 1})
                label = (user or {}).get("email", "")
        except Exception:
            label = ""
        row["label"] = label

    if format == "csv":
        return Response(
            content=to_csv(summary),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=jane-ads-billing.csv"},
        )
    return {"from_date": from_date, "to_date": to_date, **summary}


class MetaTestLaunchBody(BaseModel):
    business_name: str = "Test Business"
    budget_ngn: float = Field(15_000, gt=0)
    days: int = Field(7, gt=0)
    image_url: str
    headline: str = "Chat With Us"
    primary_text: str = "Chat with us on WhatsApp!"
    page_id: str   # no shared-page fallback exists (Per-Brand Page Connection plan §7)
                    # — pass the specific Page id you're testing against explicitly.


@router.post("/meta/test-launch")
async def meta_test_launch(
    body: MetaTestLaunchBody,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    _token: dict = Depends(JWTBearer()),
) -> dict:
    """Launches a REAL Meta campaign (created PAUSED — zero spend until a human
    activates it in Ads Manager) against the configured ad account. Lets anyone test
    the live Meta adapter directly rather than trusting a one-off script. Requires
    META_AD_ACCOUNT_ID and META_ADS_ACCESS_TOKEN to be configured, plus an explicit
    page_id — there is no shared-page fallback (Per-Brand Page Connection plan §7)."""
    import uuid
    from app.core.config import settings
    from .adapters.meta import MetaAdPlatformAdapter, MetaAPIError
    from .models import (
        ABTestScope, AdCreative, CampaignPlan, CampaignObjective, Goal, PlatformPlan,
        PurchaseBehaviour, SpendAuthorization,
    )

    if not (settings.META_AD_ACCOUNT_ID and settings.META_ADS_ACCESS_TOKEN):
        raise HTTPException(
            status_code=400,
            detail="Meta ads not configured — need META_AD_ACCOUNT_ID and META_ADS_ACCESS_TOKEN",
        )

    business_id = f"demo_meta_test_{uuid.uuid4().hex[:8]}"
    plan = CampaignPlan(
        business_id=business_id,
        goal=Goal.MESSAGES,
        behaviour=PurchaseBehaviour.DISCOVER,
        platforms=[PlatformPlan(
            platform=Platform.META, budget_ngn=body.budget_ngn, days=body.days,
            variants=1, test_scope=ABTestScope.NONE, objective=CampaignObjective.CONVERSATIONS,
        )],
        per_business_cap_ngn=body.budget_ngn,
        account_cap_ngn=body.budget_ngn,
        page_id=body.page_id,
        creative=AdCreative(image_url=body.image_url, headline=body.headline, primary_text=body.primary_text),
        explanation=f"Real Meta ads test launch for {body.business_name}",
    )
    auth = SpendAuthorization(business_id=business_id, funded_amount_ngn=body.budget_ngn, account_cap_ngn=body.budget_ngn)

    adapter = MetaAdPlatformAdapter(db, access_token=settings.META_ADS_ACCESS_TOKEN)
    try:
        result = await adapter.launch_campaign(plan, auth)
    except MetaAPIError as e:
        _raise_http_for_meta_error(e)
    except (ValueError, NotImplementedError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "campaign_id": result.campaign_id,
        "ad_ids": result.ad_ids,
        "note": "Created PAUSED — zero spend. Review and activate it yourself in Ads Manager if you want it live.",
        "ads_manager_url": (
            f"https://adsmanager.facebook.com/adsmanager/manage/campaigns"
            f"?act={settings.META_AD_ACCOUNT_ID}&selected_campaign_ids={result.campaign_id}"
        ),
    }


class TikTokTestLaunchBody(BaseModel):
    business_name: str = "Test Business"
    budget_ngn: float = Field(50_000, gt=0)   # PRD: only route ₦50,000+ wallets to TikTok
    days: int = Field(7, gt=0)
    video_url: str    # TikTok is video-only — a hosted .mp4 TikTok can fetch by URL
    headline: str = "Chat With Us"
    primary_text: str = "Chat with us on WhatsApp!"
    whatsapp_number: str   # TikTok routes to wa.me/<this> — no shared-page fallback exists


@router.post("/tiktok/test-launch")
async def tiktok_test_launch(
    body: TikTokTestLaunchBody,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    _token: dict = Depends(JWTBearer()),
) -> dict:
    """Launches a REAL TikTok campaign (created DISABLE — TikTok's paused
    equivalent, zero spend until a human activates it in TikTok Ads Manager)
    against the configured advertiser account. Mirrors /meta/test-launch's shape —
    lets anyone exercise the live TikTok adapter directly once real
    TIKTOK_ADS_ADVERTISER_ID/TIKTOK_ADS_ACCESS_TOKEN exist, the same way that
    endpoint was used to first prove the Meta adapter against a real account.
    This is the intended next verification step once Ibukun has real TikTok
    Marketing API credentials — the adapter itself is unit-tested but has never
    run against a live account."""
    import uuid
    from app.core.config import settings
    from .adapters.tiktok import TikTokAdsAdapter, TikTokAdsAPIError
    from .models import (
        ABTestScope, AdCreative, CampaignPlan, CampaignObjective, Goal, PlatformPlan,
        PurchaseBehaviour, SpendAuthorization,
    )

    if not (settings.TIKTOK_ADS_ADVERTISER_ID and settings.TIKTOK_ADS_ACCESS_TOKEN):
        raise HTTPException(
            status_code=400,
            detail="TikTok ads not configured — need TIKTOK_ADS_ADVERTISER_ID and TIKTOK_ADS_ACCESS_TOKEN",
        )

    business_id = f"demo_tiktok_test_{uuid.uuid4().hex[:8]}"
    plan = CampaignPlan(
        business_id=business_id,
        goal=Goal.MESSAGES,
        behaviour=PurchaseBehaviour.DISCOVER,
        platforms=[PlatformPlan(
            platform=Platform.TIKTOK, budget_ngn=body.budget_ngn, days=body.days,
            variants=1, test_scope=ABTestScope.NONE, objective=CampaignObjective.CONVERSATIONS,
        )],
        per_business_cap_ngn=body.budget_ngn,
        account_cap_ngn=body.budget_ngn,
        whatsapp_number=body.whatsapp_number,
        creative=AdCreative(image_url=body.video_url, is_video=True, headline=body.headline, primary_text=body.primary_text),
        explanation=f"Real TikTok ads test launch for {body.business_name}",
    )
    auth = SpendAuthorization(business_id=business_id, funded_amount_ngn=body.budget_ngn, account_cap_ngn=body.budget_ngn)

    adapter = TikTokAdsAdapter(db, advertiser_id=settings.TIKTOK_ADS_ADVERTISER_ID, access_token=settings.TIKTOK_ADS_ACCESS_TOKEN)
    try:
        result = await adapter.launch_campaign(plan, auth)
    except TikTokAdsAPIError as e:
        _raise_http_for_tiktok_error(e)
    except (ValueError, NotImplementedError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "campaign_id": result.campaign_id,
        "ad_ids": result.ad_ids,
        "note": "Created DISABLE (paused) — zero spend. Review and activate it yourself in TikTok Ads Manager if you want it live.",
        "ads_manager_url": f"https://ads.tiktok.com/i18n/perf/campaign?aadvid={settings.TIKTOK_ADS_ADVERTISER_ID}",
    }


class MetaLaunchFromMessageBody(BaseModel):
    message: str                          # plain-English ask, e.g. "get me lunch customers in Surulere, ₦15k"
    business_name: str = ""
    category: str = ""
    conversation_cost_ngn: float = Field(500.0, gt=0)
    creative_source: str = "generate"     # generate | upload | draft | recomposite | ask
    reference_image_url: str = ""         # required for creative_source=upload/recomposite (from
                                          # /creative/upload)
    is_video: bool = False                # is reference_image_url a video?
    draft_id: str = ""                    # required for creative_source=draft (from /creative/drafts)
    reuse_image_url: str = ""             # a refinement — keep the prior plan's image instead of
                                          # regenerating (a targeting/budget tweak shouldn't burn a
                                          # credit or swap the visual). Ignored for upload/draft.
    thread_id: str = ""                   # the campaign thread this plan belongs to (Tier E),
                                          # tagged onto the pending plan + launched campaign
    ignore_creative_fit_pushback: bool = False  # user already saw the creative_fit_pushback
                                          # early-return (upload/draft/recomposite that Jane
                                          # judges would do better as video) and chose to
                                          # proceed anyway — skips the pushback on resubmit
    selected_plan_variant: Optional[dict] = None  # Multi-Plan Audience Variants (spec
                                          # v1.0.0) — the client resends the EXACT
                                          # PlanVariant dict it was shown in the
                                          # choose_plan_variant stage, so this build uses
                                          # that specific audience rather than the one
                                          # Jane would've picked silently. None → present
                                          # the ranked options instead of proceeding.
    variant_group_id: str = ""           # ties multiple builds together when the client
                                          # picked more than one variant (one call per
                                          # selected variant, spec §7 "one creative per
                                          # plan") — set by the server on the first
                                          # choose_plan_variant response, echoed back on
                                          # each follow-up selection call


class _PlanBuildResult(BaseModel):
    """Everything both the one-shot endpoint and the plan-then-launch endpoints need,
    after Jane has understood the message and worked out what to do — but before
    either of them decides whether/when to actually touch Meta. `plan` already has
    `page_id` and `creative` attached, so callers can persist or launch it as-is."""
    business_id: str
    req: CampaignRequest
    plan: CampaignPlan
    jane_platforms: list[str]
    forced_to_meta: bool
    geo_dump: Optional[dict] = None
    understood: dict
    budget_estimate: Optional[dict] = None   # set when budget_ngn was computed from a stated
                                              # customer-count rather than a stated Naira amount
    summary: Optional[dict] = None            # Tier C — the structured "Jane Campaign Summary"
                                              # (each choice + its why, plus reach/click estimates)
    thread_id: str = ""                       # Tier E — the campaign thread this belongs to
    variant_group_id: str = ""                # Multi-Plan Audience Variants — ties this
                                              # build to sibling builds from the same
                                              # variant-choice session, if any (spec §7)
    selected_plan_variant: Optional[dict] = None  # the PlanVariant this build actually
                                              # used, if any — surfaced back to the
                                              # client so the plan card can show which
                                              # audience it's for


def _last_known_understood(saved: list[dict]) -> dict:
    """Fold every prior turn's `understood` into one running snapshot, oldest to
    newest — the most recent non-empty value per field wins. See the regression note
    where this is used in _build_campaign_plan: a single re-parse missing a field
    already established earlier in this thread must not silently wipe it."""
    merged: dict = {}
    for m in saved:
        if m.get("kind") == "result" and m.get("role") == "jane":
            for k, v in ((m.get("result") or {}).get("understood") or {}).items():
                if v not in (None, "", [], 0):
                    merged[k] = v
    return merged


_BARE_NUMBER_RE = re.compile(
    r"^[₦naNGN\s]*([\d,]+(?:\.\d+)?)\s*(k)?[₦\s]*$", re.IGNORECASE,
)


def _extract_trailing_bare_budget(message: str, last_assistant_turn: str) -> Optional[float]:
    """Live-confirmed real bug: a client answered a budget question with a bare
    number ("10,000") and the consultant's OWN structured budget_ngn field came
    back None — even though its own next question's prose explicitly said "your
    past spending was ₦10,000", proving it understood the number in context but
    never committed it to the field the rest of the pipeline actually reads. This
    repeated on every later turn (nothing to backfill from — see
    _last_known_understood above), forcing the client to type the same number
    twice before a plan would build.

    Deterministic fallback, independent of the model: `message` is the frontend's
    accumulated brief (each reply appended as its own ". "-separated segment — see
    send() in CampaignsPage.tsx), so the LAST segment is always the client's newest
    raw reply. If that segment is nothing but a bare number (optionally with ₦/
    commas/a "k" thousands suffix) AND the question that prompted it was actually
    about budget/spend (never fires on a bare number answering something else,
    e.g. a phone number or a headcount), treat it as the stated budget."""
    if "budget" not in last_assistant_turn.lower() and "spend" not in last_assistant_turn.lower():
        return None
    segments = [s.strip() for s in message.split(". ") if s.strip()]
    if not segments:
        return None
    m = _BARE_NUMBER_RE.match(segments[-1])
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    if m.group(2):  # "10k" -> 10,000
        value *= 1000
    return value if value > 0 else None


async def _build_campaign_plan(
    body: MetaLaunchFromMessageBody, brand_ctx: dict, db: AsyncIOMotorDatabase,
) -> "_PlanBuildResult | dict":
    """Shared by /meta/launch-from-message (plan + launch in one call) and
    /meta/plan-from-message (plan only, launched later via /meta/plan/{id}/launch) —
    understand the message, decide the platform, refine geo, and produce the ad
    creative, ending with the policy gate (one bad ad risks the whole pooled ad
    account, so it's checked here regardless of which caller is planning).
    Returns a dict with an "early_return" key for the need_more/advise stages —
    the caller should return that dict directly. Raises HTTPException for hard
    failures (bad config, unsupported creative, policy block, generation failure)."""
    import uuid
    from app.core.config import settings
    from .ads_connection import AdsConnectionRequired, resolve_ads_page_for_launch
    from .creative import creative_from_draft, creative_from_recomposite, creative_from_upload, generate_ad_creative, get_brand_context, service_area_from_geo, write_shoot_script
    from .decision_engine import apply_platform_override, plan_campaign
    from .history import get_campaign_history, remembered_budget_ngn, remembered_business_name, remembered_category
    from .nl import to_campaign_request, NlUnavailableError
    from .jane_consultant import build_history_turns, consult
    from .threads import thread_history
    from .policy import Severity, review_ad_creative

    if not (settings.META_AD_ACCOUNT_ID and settings.META_ADS_ACCESS_TOKEN):
        raise HTTPException(status_code=400, detail="Meta ads not configured — need META_AD_ACCOUNT_ID and META_ADS_ACCESS_TOKEN")

    # 0. Per-brand Meta ads connection — checked FIRST, before Jane says a word, so a
    # client never designs a whole campaign only to hit a wall at the end (Per-Brand
    # Page Connection plan §3). There is no shared-page fallback: a missing connection
    # fails loudly with the exact state, mapped to its own prompt on the frontend.
    # require_whatsapp=False here: whether WhatsApp is actually needed depends on the
    # campaign's goal, which Jane hasn't determined yet at this point — a followers/
    # engagement campaign never uses it at all. Re-checked strictly once the goal is
    # known, at step 1.6 below.
    try:
        ads_conn = await resolve_ads_page_for_launch(
            db, brand_ctx.get("user_id"), brand_ctx.get("brand_id"), require_whatsapp=False,
        )
    except AdsConnectionRequired as e:
        return {"early_return": {
            "stage": f"meta_connection_{e.state.value}",
            "understood": {},
            "page_name": e.page_name,
        }}
    page_id = ads_conn["page_id"]

    if body.creative_source in ("upload", "recomposite") and not body.reference_image_url:
        raise HTTPException(status_code=400, detail=f"reference_image_url is required for creative_source={body.creative_source}")
    if body.creative_source == "draft" and not body.draft_id:
        raise HTTPException(status_code=400, detail="draft_id is required for creative_source=draft")

    # Tie the campaign to the caller's active brand so it shows up in their
    # campaign list; fall back to a random id only if there's no brand context.
    business_id = brand_ctx.get("brand_id") or f"oneshot_{uuid.uuid4().hex[:8]}"

    # 0.5. Recall — a returning business shouldn't have to re-explain what Jane
    # already learned launching their last campaign here (PRD §6). Only meaningful
    # for a real brand context; a one-shot anonymous business_id has no history.
    history = await get_campaign_history(db, business_id) if brand_ctx.get("brand_id") else []
    # The logged-in brand already has its name + industry on file (the SAME brand
    # playbook normal content generation reads), so Jane must never ask a signed-in
    # brand "what's your business?" — that's the whole point of being logged in.
    # Precedence: what they said in THIS request → what a past campaign remembered →
    # the brand profile on the account.
    brand_profile = (await get_brand_context(brand_ctx.get("user_id", ""), db, brand_ctx.get("brand_id"))
                     if brand_ctx.get("user_id") else {})
    known_business_name = (body.business_name or remembered_business_name(history)
                           or brand_profile.get("brand_name", ""))
    known_category = (body.category or remembered_category(history)
                      or brand_profile.get("industry", ""))
    known_budget = remembered_budget_ngn(history)

    # 1. Jane (the strategic consultant, jane-strategy-extraction v1.1.0) reads the
    # REAL turn-by-turn conversation — forms a hypothesis, hunts the intermediary/
    # trigger, reasons about geography, and scales question depth to the budget tier
    # — rather than extracting fields off a checklist. The frontend's own flattened
    # "brief so far" string is only the client's fragments with no idea which answer
    # matched which question; feeding ONLY that (no history) made her re-ask the same
    # ground forever. Fetch this thread's saved messages (Jane's own prior questions
    # included) so she can actually track what's already been established.
    saved_messages = (await thread_history(db, brand_ctx.get("brand_id"), body.thread_id)
                      if body.thread_id else [])
    thread_turns = build_history_turns(saved_messages)
    # If the AI is unreachable (quota/outage), surface a clear "try again later"
    # instead of falling through to a follow-up question — otherwise every answer
    # re-triggers the same question (an infinite loop).
    try:
        parsed = await consult(body.message, known_business_name, known_category, known_budget, thread_turns)
    except NlUnavailableError:
        raise HTTPException(status_code=503, detail=_AI_DIFFICULTIES)

    # Live-confirmed regression: budget_ngn (and sometimes city/goal) correctly parsed
    # on one turn came back None/blank on the very next turn — build_history_turns only
    # turns a prior "jane" result into a turn the model can see when it carries a
    # `question` or is stage planned/launched, so a choose_plan_variant turn (no
    # `question` field) is INVISIBLE to the re-parse, and the model re-derives from
    # scratch and drops what it already knew. Backfill from the last turn that actually
    # had each field, so an established fact never silently reverts to "still missing".
    prior_understood = _last_known_understood(saved_messages)
    for field in ("business_name", "category", "goal", "offer_type", "budget_ngn",
                  "desired_conversions", "city", "geo_mode"):
        if not getattr(parsed, field, None) and prior_understood.get(field):
            setattr(parsed, field, prior_understood[field])

    # Live-confirmed separate bug: a client answered a budget question with a bare
    # number and the model's own budget_ngn field came back None on that SAME turn
    # (and every turn after — nothing in prior_understood above to backfill from
    # either, since it was never captured even once). Deterministic fallback, only
    # trusted when the question actually being answered was about budget/spend.
    if not parsed.budget_ngn:
        last_assistant = next((t.get("content", "") for t in reversed(thread_turns)
                               if t.get("role") == "assistant"), "")
        bare_budget = _extract_trailing_bare_budget(body.message, last_assistant)
        if bare_budget:
            parsed.budget_ngn = bare_budget

    # 1.5. Backwards budget (PRD §3.1) — the user described an outcome ("20 customers"),
    # not a Naira amount. Convert using this business's own real cost-per-conversation
    # (falls back to the platform floor for a brand-new business with no history yet),
    # so "how much should I spend" is answered from data, not a guess.
    budget_estimate = None
    if (not parsed.budget_ngn or parsed.budget_ngn <= 0) and parsed.desired_conversions:
        from .wallet import WalletService
        from .store import MongoWalletStore

        wallet = WalletService(MongoWalletStore(db))
        trailing_cost = await wallet.trailing_cost_per_conversation(business_id)
        price_per_conversation = WalletService.price_conversation(trailing_cost)
        parsed.budget_ngn = round(parsed.desired_conversions * price_per_conversation, 2)
        budget_estimate = {
            "desired_conversions": parsed.desired_conversions,
            "price_per_conversation_ngn": price_per_conversation,
            "estimated_budget_ngn": parsed.budget_ngn,
        }

    req = to_campaign_request(parsed, business_id=business_id)
    if req is None:
        clarify = parsed.clarify or "How much would you like to spend?"
        if known_budget and "spend" in clarify.lower():
            clarify += f" Last time you spent ₦{known_budget:,.0f} — want to do the same again?"
        return {"early_return": {"stage": "need_more", "understood": parsed.model_dump(), "question": clarify}}

    # 1.6. Now that the goal is actually known: a followers/engagement campaign never
    # routes through WhatsApp, so step 0 above deliberately let ADS_NO_WHATSAPP through.
    # Every other goal DOES need it — re-check strictly now, catching it here instead of
    # at launch.
    if req.goal != Goal.FOLLOWERS and not ads_conn["whatsapp_number"]:
        try:
            ads_conn = await resolve_ads_page_for_launch(db, brand_ctx.get("user_id"), brand_ctx.get("brand_id"))
        except AdsConnectionRequired as e:
            return {"early_return": {
                "stage": f"meta_connection_{e.state.value}",
                "understood": parsed.model_dump(),
                "page_name": e.page_name,
            }}

    # 1.7. Multi-Plan Audience Variants (spec v1.0.0) — most businesses have more than
    # one viable audience, and the client knows their customers better than Jane's
    # reasoning does. Present up to five ranked, genuinely-distinct strategies with an
    # argued recommendation, rather than silently picking one — the client picks one
    # (or, budget permitting, more), and THAT audience's own segment/geo/trigger drive
    # the rest of this build. Best-effort: an outage here never blocks planning, it
    # just falls back to today's silent-pick behaviour.
    selected_variant: Optional[PlanVariant] = None
    if body.selected_plan_variant is not None:
        try:
            selected_variant = PlanVariant.model_validate(body.selected_plan_variant)
        except Exception as e:
            print(f"[oneshot] selected_plan_variant malformed, ignoring: {e}", flush=True)
    if selected_variant is None:
        try:
            from .plan_variants import generate_plan_variants, PlanVariantsUnavailableError
            corpus = await _retrieve_for_plan_generation(db, parsed, business_id)
            variant_set = await generate_plan_variants(
                parsed, business_name=req.business_name, description=req.description,
                corpus=corpus,
            )
        except Exception as e:
            variant_set = None
            print(f"[oneshot] plan variants skipped: {e}", flush=True)
        if variant_set and len(variant_set.variants) > 1:
            import uuid as _uuid
            return {"early_return": {
                "stage": "choose_plan_variant",
                "understood": parsed.model_dump(),
                "plan_variants": variant_set.model_dump(),
                "variant_group_id": body.variant_group_id or f"vgrp_{_uuid.uuid4().hex[:16]}",
            }}
        # Only ever 0 or 1 genuinely distinct audience exists — nothing to choose
        # between, so fall straight through with Jane's own single read (unchanged
        # today's behaviour) rather than showing a pointless one-card "choice".

    # 2. Jane decides the platform + budget split, with her reasoning.
    result = plan_campaign(req, funded_amount_ngn=req.budget_ngn, total_funded_wallets_ngn=req.budget_ngn)
    if result.decision == PlanDecision.ADVISE:
        return {"early_return": {"stage": "advise", "understood": parsed.model_dump(),
                "advice": result.advice.model_dump(), "trace": result.advice.trace}}
    plan = result.plan

    # Consultant's own reasoning (jane-strategy-extraction §7.6/§8) — state the geography
    # assumption back and flag any creative-fit concern, ahead of the deterministic
    # engine's mechanical why, so the client sees the strategist's voice first.
    consultant_notes = " ".join(filter(None, [
        parsed.stated_plan, parsed.intermediary_note, parsed.creative_fit_warning,
    ]))
    if consultant_notes:
        plan.explanation = f"{consultant_notes} {plan.explanation}"

    # 2.5. WhatsApp destination — resolved from the per-brand ads connection (step 0
    # above already guarantees READY, which requires a linked number). This is a real
    # Click-to-WhatsApp destination now, not a wa.me link — see the adapter.
    wa_number = ads_conn["whatsapp_number"]

    if budget_estimate:
        plan.explanation = (
            f"Based on similar campaigns costing about ₦{budget_estimate['price_per_conversation_ngn']:,.0f} "
            f"per conversation, ₦{budget_estimate['estimated_budget_ngn']:,.0f} should get you around "
            f"{budget_estimate['desired_conversions']} conversations. {plan.explanation}"
        )
        price_per_conversation = budget_estimate["price_per_conversation_ngn"]
    else:
        # Forward-looking estimate (PRD §3.3) — the user gave a budget directly, so
        # show what it should buy too, using this business's own real per-conversation
        # price. ALWAYS an estimate, never a promise — never used to gate anything.
        from .wallet import WalletService
        from .store import MongoWalletStore

        wallet = WalletService(MongoWalletStore(db))
        trailing_cost = await wallet.trailing_cost_per_conversation(business_id)
        price_per_conversation = WalletService.price_conversation(trailing_cost)
    plan.estimated_conversations = max(1, round(req.budget_ngn / price_per_conversation))

    # TikTok has a live adapter now, but only actually usable once real Marketing
    # API credentials exist (staging/prod both start with these empty — a TikTok
    # for Business + Marketing API app approval is a real, external, days-to-weeks
    # process, not something flipped on by a deploy). Route to TikTok only when
    # Jane herself picked it AND credentials are configured; otherwise force Meta
    # exactly as before. Google is still pending — no live adapter wired into this
    # launch path yet. Jane's original recommendation is always surfaced in the
    # response for transparency, regardless of what actually launches.
    jane_platforms = [p.platform.value for p in plan.platforms]
    tiktok_ready = bool(settings.TIKTOK_ADS_ADVERTISER_ID and settings.TIKTOK_ADS_ACCESS_TOKEN)
    jane_picked_tiktok = any(p.platform == Platform.TIKTOK for p in plan.platforms)
    if jane_picked_tiktok and tiktok_ready:
        plan.platforms = [p for p in plan.platforms if p.platform == Platform.TIKTOK]
        forced_to_meta = False
    else:
        forced_to_meta = not any(p.platform == Platform.META for p in plan.platforms)
        if forced_to_meta:
            plan = apply_platform_override(plan, [Platform.META])
        else:
            plan.platforms = [p for p in plan.platforms if p.platform == Platform.META]

    # 3. Geo refinement — prefer the consultant's own §7 judgment (which of own-radius/
    # watering-hole/mixed/non-local, and which named pockets), validated by real
    # geocoding; fall back to the legacy heuristic if the consultant didn't set one
    # (e.g. no city given at all). Best-effort — never blocks planning.
    #
    # A selected audience-plan variant carries its OWN named pockets (spec §8:
    # plan.geo_pockets → brief ZONE A) — a different audience within the same city
    # can legitimately mean different areas (e.g. developers near active
    # construction sites vs. homeowners in new estates). Override the consultant's
    # areas with the variant's when one was selected; the geo MODE (own_radius/
    # watering_hole/mixed) is still Jane's own read, not something a variant changes.
    geo_areas = parsed.geo_areas
    if selected_variant and selected_variant.geo_pockets:
        geo_areas = [{"name": name, "reason": selected_variant.trigger} for name in selected_variant.geo_pockets]
    geo_dump = None
    try:
        if parsed.geo_mode:
            from .geo import geo_plan_from_named_areas
            geo_plan = await geo_plan_from_named_areas(
                parsed.geo_mode, parsed.city, geo_areas, parsed.geo_explanation,
            )
            if geo_plan is not None:   # None for non_local — no geography to attach
                plan.geo = geo_plan
                geo_dump = geo_plan.model_dump()
        elif parsed.city:
            from .geo import geo_for_request
            geo_plan = await geo_for_request(req.business_name, req.category, parsed.city, req.goal, req.description)
            plan.geo = geo_plan
            geo_dump = geo_plan.model_dump()
    except Exception as e:
        print(f"[oneshot] geo skipped: {e}", flush=True)

    # service_area (creative brief spec) — the one place a location legitimately
    # belongs in customer-facing COPY, distinct from plan.geo/parsed.city which
    # ground targeting and image generation but must never leak into copy.
    service_area = service_area_from_geo(plan.geo, parsed.city)

    # 4. The ad creative — Jane generates it, or the caller supplies their own upload/draft.
    business_name = req.business_name or body.business_name or "Your Business"
    category = req.category or body.category
    user_id = brand_ctx.get("user_id", "")
    brand_id = brand_ctx.get("brand_id")

    # Image-selection step (PRD §2): don't silently generate once budget is set — ask the
    # user how to source the image (upload their own / pick a past post / let Jane generate).
    # Only when the caller hasn't already chosen ("ask"); a concrete source skips through.
    if body.creative_source == "ask":
        from .creative import list_recent_drafts
        drafts = await list_recent_drafts(user_id, db, brand_id, limit=6) if user_id else []
        return {"early_return": {
            "stage": "choose_creative_source",
            "understood": parsed.model_dump(),
            "creative_options": {"can_generate": True, "drafts": drafts},
            # Jane's geography/audience call (stated_plan, jane-strategy-extraction §7.6) is
            # REQUIRED to be confirmed back to the client, never silently decided — this was
            # being computed into plan.explanation but never reaching the client at this step,
            # the exact point they'd otherwise see it before committing to an image.
            "explanation": plan.explanation,
        }}

    # A selected audience-plan variant's own phrasing drives Zone A/B here (spec §8)
    # instead of the brand's generic target_audience — empty when no variant was
    # selected, which is a no-op fallback to today's existing behaviour.
    variant_segment = selected_variant.audience_segment if selected_variant else ""
    variant_who_its_for = selected_variant.who_its_for if selected_variant else ""
    # The variant's own named areas are real targeting parameters (Zone A), just like
    # geo_areas above — equally forbidden from appearing in copy. Live-confirmed leak:
    # these reached Meta's real targeting but were never passed into the creative
    # functions at all, so the leakage check had no way to catch them.
    variant_geo_pockets = selected_variant.geo_pockets if selected_variant else None

    if body.creative_source == "upload":
        creative = await creative_from_upload(
            business_name, category, body.reference_image_url, req.goal.value, req.description,
            user_id=user_id, db=db, brand_id=brand_id, is_video=body.is_video,
            city=parsed.city, service_area=service_area,
            audience_segment=variant_segment, who_its_for=variant_who_its_for,
            geo_pockets=variant_geo_pockets,
            budget_ngn=float(parsed.budget_ngn or 0),
        )
    elif body.creative_source == "recomposite":
        creative = await creative_from_recomposite(
            business_name, category, body.reference_image_url, req.goal.value, req.description,
            user_id=user_id, db=db, brand_id=brand_id,
            city=parsed.city, service_area=service_area,
            audience_segment=variant_segment, who_its_for=variant_who_its_for,
            geo_pockets=variant_geo_pockets,
        )
    elif body.creative_source == "draft":
        creative = await creative_from_draft(
            business_name, category, body.draft_id, user_id, db,
            goal=req.goal.value, brand_id=brand_id,
            city=parsed.city, service_area=service_area,
            audience_segment=variant_segment, who_its_for=variant_who_its_for,
            geo_pockets=variant_geo_pockets,
        )
        if creative is None:
            raise HTTPException(status_code=404, detail="Draft not found or has no image")
    elif body.reuse_image_url:
        # Refinement of an existing plan (e.g. "target lagos and abuja", "make it ₦10k") —
        # keep the image from the prior plan. Copy is still rewritten to match any changed
        # brief, but no image is generated, so no content credit is spent.
        creative = await creative_from_upload(
            business_name, category, body.reuse_image_url, req.goal.value, req.description,
            user_id=user_id, db=db, brand_id=brand_id, is_video=False,
            city=parsed.city, service_area=service_area,
            audience_segment=variant_segment, who_its_for=variant_who_its_for,
            geo_pockets=variant_geo_pockets,
        )
    else:
        # AI generation is the one creative path that costs a content credit — an
        # uploaded photo/video or a reused draft (PRD §5.1) doesn't touch this at all.
        from app.services.CreditService import credit_service

        if not await credit_service.check_sufficient_credits(user_id, required=1):
            raise HTTPException(
                status_code=402,
                detail="You're out of content credits — top up to generate a new ad image, or upload your own photo/video instead.",
            )
        creative = await generate_ad_creative(
            business_name, category, req.goal.value, req.description,
            user_id=user_id, db=db, brand_id=brand_id, city=parsed.city,
            behaviour=plan.behaviour.value, service_area=service_area,
            audience_segment=variant_segment, who_its_for=variant_who_its_for,
            geo_pockets=variant_geo_pockets,
        )
        if creative.image_url:
            # "reason" is a strict Literal on CreditTransaction — "campaign_generation"
            # (the function's own default) is the closest existing match; there's no
            # ad-specific reason value and adding one is a shared-model change beyond
            # this feature's scope.
            await credit_service.deduct_credit(user_id, campaign_id=business_id, reason="campaign_generation")
    if not creative.image_url:
        # Empty creative almost always means the image/copy model failed (often the
        # same quota/outage that breaks parsing), so give the same clear message.
        raise HTTPException(status_code=503, detail=_AI_DIFFICULTIES)

    # plan.explanation's "you have photos"/"video" phrase (decision_engine._explain) was
    # written from the NL-guessed has_video BEFORE the user actually chose upload/generate
    # here — a real upload of a video left it stuck saying "you have photos". Patch it now
    # that the real creative kind is known.
    if creative.is_video and "you have video" not in plan.explanation:
        plan.explanation = (
            plan.explanation
            .replace("you have photos", "you have video")
            .replace("no creative is needed for search", "you have video")
        )

    # 4.5. Policy gate — one bad ad can suspend the whole pooled ad account, so this
    # runs before a plan is ever shown as ready, not just right before launch.
    # BLOCK severity aborts with the specific guidance; WARN-only violations are
    # logged but don't stop planning (re-checked again at commit in /plan/{id}/launch).
    policy_result = review_ad_creative(creative.headline, creative.primary_text)
    blocking = [v for v in policy_result.violations if v.severity == Severity.BLOCK]
    if blocking:
        raise HTTPException(
            status_code=400,
            detail=f"Can't use this creative — {blocking[0].guidance}",
        )
    for v in policy_result.violations:
        print(f"[policy] WARN on plan for {business_id}: {v.category} — matched '{v.matched_text}'", flush=True)

    # 4.6. Creative-fit pushback (creative brief spec §6.2/§9) — the user brought their
    # own upload/draft/recomposited photo, but Jane judges this business would clearly
    # do better as video. Never silently accept or silently downgrade: surface a real
    # tappable choice (Case 3 style, same early-return shape as choose_creative_source),
    # with the shoot script already written so "use it" has something concrete to show.
    # GENERATE doesn't need this gate — there's no user-supplied media to reconcile
    # against; it just gets the existing passive nudge below.
    if (body.creative_source in ("upload", "draft", "recomposite")
            and creative.video_recommendation and not body.ignore_creative_fit_pushback):
        pushback_script = None
        try:
            pushback_script = await write_shoot_script(
                business_name, category, req.goal.value, creative.video_recommendation,
                req.description, await get_brand_context(user_id, db, brand_id) if user_id else {},
            )
        except Exception as e:
            print(f"[oneshot] pushback shoot script skipped: {e}", flush=True)
        return {"early_return": {
            "stage": "creative_fit_pushback",
            "understood": parsed.model_dump(),
            "reason": creative.video_recommendation,
            "options": ["keep_as_is", "use_script", "reconsider"],
            "shoot_script": (pushback_script.model_dump()
                             if pushback_script and pushback_script.shots else None),
            "creative_preview": {
                "headline": creative.headline, "primary_text": creative.primary_text,
                "image_url": creative.image_url,
            },
        }}

    plan.page_id = page_id
    plan.whatsapp_number = wa_number
    plan.creative = creative
    if creative.video_recommendation:
        # Path C (PRD §4.1): gpt-image-1 can't shoot the recommended video itself, so
        # hand the user a phone-filmable script instead of just a suggestion — film
        # it, upload it (POST /creative/upload), then swap it into this plan via
        # POST /meta/plan/{plan_id}/creative before launching.
        try:
            plan.shoot_script = await write_shoot_script(
                business_name, category, req.goal.value, creative.video_recommendation,
                req.description, await get_brand_context(user_id, db, brand_id) if user_id else {},
            )
        except Exception as e:
            print(f"[oneshot] shoot script skipped: {e}", flush=True)
        nudge = (
            "Film this yourself and I'll turn it into your ad — see the shoot script."
            if plan.shoot_script and plan.shoot_script.shots
            else "You can upload a video instead before launching if you'd like."
        )
        plan.explanation = f"{plan.explanation} {creative.video_recommendation} {nudge}"

    # Tier C — the Jane Campaign Summary: every choice + its why, plus reach/click/lead
    # estimates. Reach comes from Meta's real delivery_estimate (best-effort; the summary
    # still renders without it). Never blocks the plan.
    summary_dump = None
    try:
        from .summary import build_campaign_summary
        from .adapters.meta import MetaAdPlatformAdapter

        estimate = None
        # get_delivery_estimate is a Meta-only capability (Meta's own Delivery
        # Estimate API) — TikTokAdsAdapter has no equivalent, so only attempt this
        # for a Meta-bound plan rather than failing every TikTok launch through
        # the except below for no reason.
        if plan.platforms and plan.platforms[0].platform == Platform.META:
            try:
                est_adapter = MetaAdPlatformAdapter(db, access_token=settings.META_ADS_ACCESS_TOKEN)
                # Reach the REAL audience: build the targeting from the plan's geo pins (radius
                # around each validated coordinate) so the estimate isn't all of Nigeria.
                custom_locations = [
                    {"latitude": pin.lat, "longitude": pin.lng,
                     "radius": pin.radius_km, "distance_unit": "kilometer"}
                    for pin in (plan.geo.pins if plan.geo else [])
                    if pin.lat is not None and pin.lng is not None
                ]
                targeting = ({"geo_locations": {"custom_locations": custom_locations}}
                             if custom_locations else {"geo_locations": {"countries": ["NG"]}})
                estimate = await est_adapter.get_delivery_estimate(targeting)
            except Exception as e:
                print(f"[oneshot] delivery estimate skipped: {e}", flush=True)
        summary = build_campaign_summary(plan, req, price_per_result_ngn=price_per_conversation,
                                         delivery_estimate=estimate)
        summary_dump = summary.model_dump(mode="json")
    except Exception as e:
        print(f"[oneshot] summary skipped: {e}", flush=True)

    return _PlanBuildResult(
        business_id=business_id, req=req, plan=plan, jane_platforms=jane_platforms,
        forced_to_meta=forced_to_meta, geo_dump=geo_dump, understood=parsed.model_dump(),
        budget_estimate=budget_estimate, summary=summary_dump, thread_id=body.thread_id,
        variant_group_id=body.variant_group_id,
        selected_plan_variant=selected_variant.model_dump() if selected_variant else None,
    )


def _total_due_ngn(budget_ngn: float) -> float:
    """What the customer's wallet must cover to run a `budget_ngn` campaign: the ad
    budget PLUS URI's service fee (the AD_SPEND_MARKUP margin). Meta only ever spends
    `budget_ngn`; billing (billing.py) debits the wallet up to budget × markup as the
    campaign delivers, so the wallet is gated to exactly that here — which also makes
    the wallet empty right when Meta's own budget is exhausted."""
    return round(budget_ngn * C.AD_SPEND_MARKUP, 2)


async def _wallet_status(db: AsyncIOMotorDatabase, business_id: str, budget_ngn: float) -> tuple[float, bool]:
    """(balance, sufficient) — the real Mongo-backed balance vs. the TOTAL due (ad
    budget + service fee), not just the ad budget."""
    from .store import MongoWalletStore
    from .wallet import WalletService

    wallet = WalletService(MongoWalletStore(db))
    balance = await wallet.get_balance(business_id)
    return balance, balance >= _total_due_ngn(budget_ngn)


def _wallet_shortfall_message(balance: float, budget_ngn: float) -> str:
    due = _total_due_ngn(budget_ngn)
    fee = round(due - budget_ngn, 2)
    return (
        f"Your ad wallet has ₦{balance:,.0f} — top up ₦{(due - balance):,.0f} more "
        f"before launching. A ₦{budget_ngn:,.0f} campaign costs ₦{due:,.0f} "
        f"(₦{budget_ngn:,.0f} ad spend + ₦{fee:,.0f} service fee)."
    )


def _plan_response_dict(built: _PlanBuildResult) -> dict:
    """The `plan`+`creative` shape shared by every stage that returns a built plan
    (planned, launched) — kept in one place so the two endpoints can't drift apart."""
    plan = built.plan
    return {
        "plan": {
            "goal": plan.goal.value,
            "behaviour": plan.behaviour.value,
            "explanation": plan.explanation,
            "platforms": [p.model_dump(mode="json") for p in plan.platforms],
            "per_business_cap_ngn": plan.per_business_cap_ngn,
            "account_cap_ngn": plan.account_cap_ngn,
            "budget_tier": plan.budget_tier,
            "estimated_conversations": plan.estimated_conversations,
            "geo": built.geo_dump,
            "trace": plan.trace,
        },
        "creative": plan.creative.model_dump(mode="json"),
        "shoot_script": plan.shoot_script.model_dump(mode="json") if plan.shoot_script else None,
        "budget_estimate": built.budget_estimate,
        # So the review can show where leads land (and let the user catch a wrong
        # auto-adopted number before launch).
        "whatsapp_number": plan.whatsapp_number,
        # Tier C — the structured, explained summary (each choice + its why + estimates).
        "summary": built.summary,
        # Multi-Plan Audience Variants — which audience this specific build used, and
        # the group tag linking it to any sibling builds from the same variant choice
        # (spec §7 — one creative per selected plan, shown/launched as a set).
        "variant_group_id": built.variant_group_id,
        "selected_plan_variant": built.selected_plan_variant,
    }


async def _do_launch(built: _PlanBuildResult, body_message: str, body_business_name: str, brand_ctx: dict, db: AsyncIOMotorDatabase) -> dict:
    """The actual platform launch + campaign-record enrichment — shared by the
    one-shot endpoint and /meta/plan/{id}/launch, both of which have already done
    their own wallet-gate check by the time they call this. Dispatches by
    plan.platforms[0].platform — Meta unless Jane picked TikTok AND TikTok
    credentials are configured (see the forcing logic in _build_campaign_plan)."""
    from app.core.config import settings
    from .adapters.meta import MetaAdPlatformAdapter, MetaAPIError
    from .adapters.tiktok import TikTokAdsAdapter, TikTokAdsAPIError
    from .wallet import WalletService
    from .store import MongoWalletStore

    plan, req, business_id = built.plan, built.req, built.business_id
    wallet = WalletService(MongoWalletStore(db))
    auth = await wallet.authorization_for(business_id, total_funded_wallets_ngn=req.budget_ngn)

    launch_platform = plan.platforms[0].platform if plan.platforms else Platform.META
    is_tiktok = launch_platform == Platform.TIKTOK

    if is_tiktok:
        adapter = TikTokAdsAdapter(db, advertiser_id=settings.TIKTOK_ADS_ADVERTISER_ID, access_token=settings.TIKTOK_ADS_ACCESS_TOKEN)
    else:
        adapter = MetaAdPlatformAdapter(db, access_token=settings.META_ADS_ACCESS_TOKEN)
    try:
        launch = await adapter.launch_campaign(plan, auth)
    except TikTokAdsAPIError as e:
        _raise_http_for_tiktok_error(e)
    except MetaAPIError as e:
        _raise_http_for_meta_error(e)
    except (ValueError, NotImplementedError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    campaign_collection = "jane_ads_tiktok_campaigns" if is_tiktok else "jane_ads_meta_campaigns"
    # Enrich the stored campaign record with display fields so the campaign-list
    # view can render name/creative/budget without re-deriving them.
    await db[campaign_collection].update_one(
        {"campaign_id": launch.campaign_id},
        {"$set": {
            "brand_id": brand_ctx.get("brand_id"),
            "user_id": brand_ctx.get("user_id"),
            "display_name": req.business_name or body_business_name or "Campaign",
            "category": req.category,
            "headline": plan.creative.headline,
            "primary_text": plan.creative.primary_text,
            "image_url": plan.creative.image_url,
            "budget_ngn": req.budget_ngn,
            "goal": plan.goal.value,
            "city": req.geo,
            "message": body_message,
            "thread_id": built.thread_id,
            # Where this campaign's leads route (wa.me/<this>) — so "My Campaigns" can show
            # the user exactly where to find their conversations. Absent on legacy campaigns
            # that predate wa.me routing (they went to the shared Page's inbox).
            "whatsapp_number": plan.whatsapp_number,
        }},
    )

    launch_note = (
        "Created DISABLE (paused) — zero spend. Review and activate in TikTok Ads Manager to go live."
        if is_tiktok else
        "Created PAUSED — zero spend. Review and activate in Ads Manager to go live."
    )
    ads_manager_url = (
        f"https://ads.tiktok.com/i18n/perf/campaign?aadvid={settings.TIKTOK_ADS_ADVERTISER_ID}"
        if is_tiktok else
        f"https://adsmanager.facebook.com/adsmanager/manage/campaigns"
        f"?act={settings.META_AD_ACCOUNT_ID}&selected_campaign_ids={launch.campaign_id}"
    )
    return {
        "stage": "launched",
        "understood": built.understood,
        "jane_recommended_platforms": built.jane_platforms,
        "forced_to_meta": built.forced_to_meta,
        **_plan_response_dict(built),
        "launch": {
            "campaign_id": launch.campaign_id,
            "ad_ids": launch.ad_ids,
            "page_id": plan.page_id,
            "status": "DISABLE" if is_tiktok else "PAUSED",
            "note": launch_note,
            "ads_manager_url": ads_manager_url,
        },
    }


@router.post("/meta/launch-from-message")
async def meta_launch_from_message(
    body: MetaLaunchFromMessageBody,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """The full one-shot flow: plain-English message → Jane understands it → decides
    the platform (with her reasoning) → generates a real branded ad creative → pushes
    a REAL campaign to Meta (created PAUSED, zero spend) → returns everything. For
    reviewing a plan before committing to Meta, use /meta/plan-from-message +
    /meta/plan/{id}/launch instead — this endpoint plans and launches in one call."""
    built = await _build_campaign_plan(body, brand_ctx, db)
    if isinstance(built, dict):
        return built["early_return"]

    # Wallet gate — the ad wallet must actually have the money before anything
    # reaches Meta. Blocks with the exact shortfall rather than silently launching
    # a campaign whose real Meta daily budget got clamped to less than requested.
    balance, sufficient = await _wallet_status(db, built.business_id, built.req.budget_ngn)
    if not sufficient:
        raise HTTPException(status_code=400, detail=_wallet_shortfall_message(balance, built.req.budget_ngn))

    return await _do_launch(built, body.message, body.business_name, brand_ctx, db)


@router.post("/meta/plan-from-message")
async def meta_plan_from_message(
    body: MetaLaunchFromMessageBody,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Plan-before-launch, step 1: understand the message, decide the platform,
    generate the creative — but never touch Meta. Returns a reviewable plan (persisted
    so it can be launched later, or just abandoned — 'save for later', nothing lost)
    plus an informational wallet check. Actually launching it is a separate, explicit
    call: POST /meta/plan/{plan_id}/launch."""
    import uuid
    from datetime import datetime, timedelta, timezone

    built = await _build_campaign_plan(body, brand_ctx, db)
    if isinstance(built, dict):
        return built["early_return"]

    balance, sufficient = await _wallet_status(db, built.business_id, built.req.budget_ngn)

    plan_id = f"plan_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc)
    await db["jane_ads_pending_plans"].insert_one({
        "plan_id": plan_id,
        "business_id": built.business_id,
        "brand_id": brand_ctx.get("brand_id"),
        "user_id": brand_ctx.get("user_id"),
        "message": body.message,
        "business_name": body.business_name,
        "req": built.req.model_dump(mode="json"),
        "plan": built.plan.model_dump(mode="json"),
        "jane_platforms": built.jane_platforms,
        "forced_to_meta": built.forced_to_meta,
        "geo_dump": built.geo_dump,
        "understood": built.understood,
        "budget_estimate": built.budget_estimate,
        "summary": built.summary,
        "thread_id": built.thread_id,
        "variant_group_id": built.variant_group_id,
        "selected_plan_variant": built.selected_plan_variant,
        "status": "pending",
        "created_at": now,
        "expires_at": now + timedelta(days=7),
    })

    return {
        "stage": "planned",
        "plan_id": plan_id,
        "understood": built.understood,
        "jane_recommended_platforms": built.jane_platforms,
        "forced_to_meta": built.forced_to_meta,
        **_plan_response_dict(built),
        "wallet": {
            "balance_ngn": balance,
            "budget_ngn": built.req.budget_ngn,
            "service_fee_ngn": round(_total_due_ngn(built.req.budget_ngn) - built.req.budget_ngn, 2),
            "total_due_ngn": _total_due_ngn(built.req.budget_ngn),
            "sufficient": sufficient,
        },
    }


class PlanCreativeUpdateBody(BaseModel):
    reference_image_url: str        # from POST /creative/upload — the user's own shot footage
    is_video: bool = True


@router.post("/meta/plan/{plan_id}/creative")
async def meta_plan_update_creative(
    plan_id: str,
    body: PlanCreativeUpdateBody,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Path C, step 2 — fold the user's own shot footage back into a pending plan
    before launch: Jane wrote the shoot script (see plan.shoot_script), the user
    filmed it and uploaded it via POST /creative/upload, and this swaps that media
    into the plan in place of the AI-generated photo. Copy/headline/CTA are kept —
    only the media and source change. Policy is re-checked again anyway at launch."""
    from .models import AdCreative, CreativeSource

    doc = await db["jane_ads_pending_plans"].find_one({"plan_id": plan_id})
    if not doc or doc.get("brand_id") != brand_ctx.get("brand_id"):
        raise HTTPException(status_code=404, detail="Plan not found")
    if doc["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"This plan is already {doc['status']} — describe a new campaign to Jane to plan another.")
    if not body.reference_image_url:
        raise HTTPException(status_code=400, detail="reference_image_url is required — upload the footage via /creative/upload first.")

    plan = CampaignPlan.model_validate(doc["plan"])
    plan.creative = AdCreative(
        image_url=body.reference_image_url,
        is_video=body.is_video,
        headline=plan.creative.headline,
        primary_text=plan.creative.primary_text,
        cta=plan.creative.cta,
        source=CreativeSource.UPLOAD,
        generated=True,
    )
    await db["jane_ads_pending_plans"].update_one(
        {"plan_id": plan_id}, {"$set": {"plan": plan.model_dump(mode="json")}},
    )
    return {"plan_id": plan_id, "creative": plan.creative.model_dump(mode="json")}


@router.post("/meta/plan/{plan_id}/launch")
async def meta_launch_plan(
    plan_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Plan-before-launch, step 2 — the ONLY place a plan actually becomes a real
    Meta campaign. Re-validates wallet + policy here at commit time (not when the
    plan was first built), since real time may have passed since planning."""
    from datetime import datetime, timezone
    from .policy import Severity, review_ad_creative

    brand_id = brand_ctx.get("brand_id")
    doc = await db["jane_ads_pending_plans"].find_one({"plan_id": plan_id})
    if not doc or doc.get("brand_id") != brand_id:
        raise HTTPException(status_code=404, detail="Plan not found")
    if doc["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"This plan is already {doc['status']} — describe a new campaign to Jane to plan another.")
    expires_at = doc["expires_at"]
    if hasattr(expires_at, "tzinfo") and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        await db["jane_ads_pending_plans"].update_one({"plan_id": plan_id}, {"$set": {"status": "expired"}})
        raise HTTPException(status_code=410, detail="This plan has expired — describe the campaign to Jane again to make a fresh one.")

    plan = CampaignPlan.model_validate(doc["plan"])
    req = CampaignRequest.model_validate(doc["req"])

    # Re-resolve the ads connection at commit time rather than trusting the page_id/
    # whatsapp_number frozen into the plan when it was first built — real time has
    # passed, and this is exactly the moment a client who just fixed a rejected
    # WhatsApp number (Meta: "not linked to your account") needs the retry to
    # actually pick up their correction, not silently resend the stale one.
    from .ads_connection import AdsConnectionRequired, resolve_ads_page_for_launch
    try:
        ads_conn = await resolve_ads_page_for_launch(
            db, brand_ctx.get("user_id"), brand_id, require_whatsapp=plan.goal != Goal.FOLLOWERS,
        )
    except AdsConnectionRequired as e:
        raise HTTPException(status_code=409, detail=f"meta_connection_{e.state.value}")
    plan.page_id = ads_conn["page_id"]
    plan.whatsapp_number = ads_conn["whatsapp_number"]

    # Policy re-check at commit — cheap and deterministic, and real time has passed
    # since the plan was built, so this is a genuine safety re-validation, not just
    # a formality.
    policy_result = review_ad_creative(plan.creative.headline, plan.creative.primary_text)
    blocking = [v for v in policy_result.violations if v.severity == Severity.BLOCK]
    if blocking:
        raise HTTPException(status_code=400, detail=f"Can't launch this ad — {blocking[0].guidance}")

    balance, sufficient = await _wallet_status(db, doc["business_id"], req.budget_ngn)
    if not sufficient:
        raise HTTPException(status_code=400, detail=_wallet_shortfall_message(balance, req.budget_ngn))

    built = _PlanBuildResult(
        business_id=doc["business_id"], req=req, plan=plan,
        jane_platforms=doc["jane_platforms"], forced_to_meta=doc["forced_to_meta"],
        geo_dump=doc.get("geo_dump"), understood=doc["understood"],
        budget_estimate=doc.get("budget_estimate"), summary=doc.get("summary"),
        thread_id=doc.get("thread_id", ""),
    )
    result = await _do_launch(built, doc["message"], doc["business_name"], brand_ctx, db)
    await db["jane_ads_pending_plans"].update_one(
        {"plan_id": plan_id},
        {"$set": {"status": "launched", "campaign_id": result["launch"]["campaign_id"]}},
    )
    return result


# ── Plan Defence — Jane can explain/defend a plan she already built ───────────

class PlanAskBody(BaseModel):
    question: str
    confirm_correction: bool = False   # user already saw a challenge's re-derived
                                       # preview and wants it to REPLACE the stored
                                       # plan — never applied silently on first ask


@router.post("/meta/plan/{plan_id}/ask")
async def meta_plan_ask(
    plan_id: str,
    body: PlanAskBody,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Answer a question about an existing plan, run a what-if, or fold in a
    corrected foundation fact — the three follow-up shapes a real client asks after
    seeing a plan (Plan Defence spec). Never fabricates a number: questions/what-ifs
    only ever reference the plan's own persisted derivation or re-run the real
    decision engine; a challenge always shows its re-derived plan for confirmation
    before it replaces anything stored."""
    from datetime import datetime, timezone

    from .plan_defence import NlUnavailableError, classify_followup, explain_plan, what_if
    from .summary import CampaignSummary

    brand_id = brand_ctx.get("brand_id")
    doc = await db["jane_ads_pending_plans"].find_one({"plan_id": plan_id})
    if not doc or doc.get("brand_id") != brand_id:
        raise HTTPException(status_code=404, detail="Plan not found")
    if not (body.question or "").strip():
        raise HTTPException(status_code=400, detail="question is required")

    plan = CampaignPlan.model_validate(doc["plan"])
    req = CampaignRequest.model_validate(doc["req"])
    summary = CampaignSummary.model_validate(doc["summary"]) if doc.get("summary") else None

    intent = await classify_followup(body.question, current_budget_ngn=req.budget_ngn)
    now = datetime.now(timezone.utc)

    async def _log(kind: str, answer: str) -> None:
        await db["jane_ads_pending_plans"].update_one(
            {"plan_id": plan_id},
            {"$push": {"qa_log": {"question": body.question, "kind": kind, "answer": answer, "at": now}}},
        )

    if intent.kind == "question":
        if intent.what_if_budget_ngn:
            try:
                result = what_if(plan, req, intent.what_if_budget_ngn, summary)
            except ValueError as e:
                await _log("question", str(e))
                return {"kind": "question", "answer": str(e)}
            await _log("question", result.narrative)
            return {
                "kind": "question",
                "answer": result.narrative,
                "what_if": {
                    "changed": result.changed,
                    "original": result.original.model_dump(mode="json"),
                    "hypothetical": result.hypothetical.model_dump(mode="json"),
                },
            }
        try:
            answer = await explain_plan(body.question, plan, req, summary, doc.get("understood"))
        except NlUnavailableError:
            raise HTTPException(status_code=503, detail=_AI_DIFFICULTIES)
        await _log("question", answer)
        return {"kind": "question", "answer": answer}

    if intent.kind == "challenge":
        if doc["status"] != "pending":
            answer = (f"This campaign has already {doc['status']} — I can't fold a correction "
                     "into it here. Describe a new campaign to Jane to plan an updated one.")
            await _log("challenge", answer)
            return {"kind": "challenge", "answer": answer}

        # Reuse the whole existing re-plan machinery (Plan Defence spec §4) — fold the
        # correction into the flattened brief the SAME way consult() already reads it,
        # and reuse the existing creative image (no new content credit, no image churn)
        # since a foundation-fact correction is about targeting/budget, not the visual.
        synthetic_body = MetaLaunchFromMessageBody(
            message=f"{doc['message']} {body.question}".strip(),
            business_name=doc.get("business_name", ""),
            category=req.category,
            reuse_image_url=plan.creative.image_url if plan.creative else "",
            thread_id=doc.get("thread_id", ""),
        )
        rebuilt = await _build_campaign_plan(synthetic_body, brand_ctx, db)
        if isinstance(rebuilt, dict):
            return {"kind": "challenge", **rebuilt["early_return"]}

        preview = {
            "kind": "challenge",
            "stage": "challenge_preview" if not body.confirm_correction else "planned",
            "plan_id": plan_id,
            **_plan_response_dict(rebuilt),
        }
        if not body.confirm_correction:
            preview["note"] = "This reflects your correction — resend with confirm_correction=true to replace the current plan."
            await _log("challenge", rebuilt.plan.explanation)
            return preview

        await db["jane_ads_pending_plans"].update_one(
            {"plan_id": plan_id},
            {"$set": {
                "req": rebuilt.req.model_dump(mode="json"),
                "plan": rebuilt.plan.model_dump(mode="json"),
                "jane_platforms": rebuilt.jane_platforms,
                "forced_to_meta": rebuilt.forced_to_meta,
                "geo_dump": rebuilt.geo_dump,
                "understood": rebuilt.understood,
                "budget_estimate": rebuilt.budget_estimate,
                "summary": rebuilt.summary,
                "message": synthetic_body.message,
            }},
        )
        await _log("challenge", rebuilt.plan.explanation)
        return preview

    # "new_campaign" — never silently act on it as a correction to THIS plan.
    answer = ("That sounds like a new campaign rather than something about this one — "
             "describe it to Jane fresh and I'll plan it separately.")
    await _log("new_campaign", answer)
    return {"kind": "new_campaign", "answer": answer}


@router.get("/meta/campaigns")
async def meta_campaigns(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
    with_metrics: bool = True,
) -> dict:
    """List the active brand's campaigns for the management view — Meta and TikTok
    together (the route stays named /meta/campaigns for backwards compatibility;
    the frontend already calls it and matches the PRD's own "Jane decides the
    platform silently" philosophy — the caller never had to pick one). Each row
    carries its display fields (name, creative, budget) plus — when with_metrics —
    live reach/conversation/spend numbers pulled from the platform. Metrics
    failures per campaign are swallowed so one bad campaign never blanks the whole
    list."""
    from app.core.config import settings
    from .adapters.meta import MetaAdPlatformAdapter
    from .adapters.tiktok import TikTokAdsAdapter

    brand_id = brand_ctx.get("brand_id")
    if not brand_id:
        return {"campaigns": []}

    meta_records = await (db["jane_ads_meta_campaigns"]
                     .find({"brand_id": brand_id}, {"_id": 0})
                     .sort("created_at", -1).to_list(length=200))
    tiktok_records = await (db["jane_ads_tiktok_campaigns"]
                     .find({"brand_id": brand_id}, {"_id": 0})
                     .sort("created_at", -1).to_list(length=200))

    meta_adapter = None
    if with_metrics and settings.META_ADS_ACCESS_TOKEN and settings.META_AD_ACCOUNT_ID:
        meta_adapter = MetaAdPlatformAdapter(db, access_token=settings.META_ADS_ACCESS_TOKEN)
    tiktok_adapter = None
    if with_metrics and settings.TIKTOK_ADS_ACCESS_TOKEN and settings.TIKTOK_ADS_ADVERTISER_ID:
        tiktok_adapter = TikTokAdsAdapter(db, advertiser_id=settings.TIKTOK_ADS_ADVERTISER_ID, access_token=settings.TIKTOK_ADS_ACCESS_TOKEN)

    out = []
    for is_tiktok, r in [(False, r) for r in meta_records] + [(True, r) for r in tiktok_records]:
        collection = "jane_ads_tiktok_campaigns" if is_tiktok else "jane_ads_meta_campaigns"
        adapter = tiktok_adapter if is_tiktok else meta_adapter
        created = r.get("created_at")
        row = {
            "campaign_id": r.get("campaign_id"),
            "platform": "tiktok" if is_tiktok else "meta",
            "name": r.get("display_name") or "Campaign",
            "headline": r.get("headline", ""),
            "primary_text": r.get("primary_text", ""),
            "image_url": r.get("image_url", ""),
            "budget_ngn": r.get("budget_ngn"),
            "goal": r.get("goal", ""),
            "city": r.get("city", ""),
            # Where leads for this campaign land, so the user can find their conversations.
            # Empty on legacy campaigns (pre-wa.me routing) — the UI flags those.
            "whatsapp_number": r.get("whatsapp_number") or "",
            "status": "paused",   # everything is created PAUSED (Meta) / DISABLE (TikTok) for now
            "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
            "ads_manager_url": (
                f"https://ads.tiktok.com/i18n/perf/campaign?aadvid={settings.TIKTOK_ADS_ADVERTISER_ID}"
                if is_tiktok else
                f"https://adsmanager.facebook.com/adsmanager/manage/campaigns"
                f"?act={settings.META_AD_ACCOUNT_ID}&selected_campaign_ids={r.get('campaign_id')}"
            ),
            "metrics": None,
        }
        if adapter and r.get("campaign_id"):
            try:
                summary = await adapter.fetch_campaign_summary(r["campaign_id"])
                # A campaign can be deleted by means we never see (directly in Ads
                # Manager, or a manual cleanup) — once the platform itself reports
                # it as gone, drop our own record too instead of showing a stale
                # "Deleted" ghost card forever. This is the ONLY status we self-heal
                # on; everything else (paused/active/in review/etc.) still renders.
                if summary["delivery"] == "Deleted":
                    await db[collection].delete_one({"campaign_id": r["campaign_id"]})
                    continue
                row["status"] = summary["delivery"].lower()
                row["metrics"] = {
                    "spend_ngn": round(summary["spend_ngn"], 2),
                    "conversations": summary["conversations"],
                    "cost_per_conversation_ngn": (
                        round(summary["cost_per_conversation_ngn"], 2)
                        if summary["cost_per_conversation_ngn"] is not None else None
                    ),
                    "impressions": summary["impressions"],
                    "reach": summary["reach"],
                    "delivery": summary["delivery"],
                    "ends_at": summary["ends_at"],
                }
            except Exception as e:
                print(f"[campaigns] metrics failed for {r.get('campaign_id')}: {e}", flush=True)
        out.append(row)

    out.sort(key=lambda row: row.get("created_at") or "", reverse=True)
    return {"campaigns": out}


class CampaignStatusBody(BaseModel):
    active: bool


@router.post("/meta/campaigns/{campaign_id}/status")
async def set_meta_campaign_status(
    campaign_id: str,
    body: CampaignStatusBody,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Turn a campaign on or off from the caller's own campaign-management view —
    no Ads Manager needed. Scoped to the caller's active brand so a campaign_id
    can't be toggled by anyone outside the brand that owns it. Going active is the
    one genuinely consequential action here — real budget can start being spent.
    Looks the campaign up across both platform collections — the route stays
    /meta/campaigns/... for backwards compatibility, but a campaign_id could now
    belong to either."""
    from app.core.config import settings
    from .adapters.meta import MetaAdPlatformAdapter, MetaAPIError
    from .adapters.tiktok import TikTokAdsAdapter, TikTokAdsAPIError

    brand_id = brand_ctx.get("brand_id")
    record = await db["jane_ads_meta_campaigns"].find_one({"campaign_id": campaign_id})
    is_tiktok = False
    if not record:
        record = await db["jane_ads_tiktok_campaigns"].find_one({"campaign_id": campaign_id})
        is_tiktok = True
    if not record or record.get("brand_id") != brand_id:
        raise HTTPException(status_code=404, detail="Campaign not found")

    if is_tiktok:
        if not (settings.TIKTOK_ADS_ADVERTISER_ID and settings.TIKTOK_ADS_ACCESS_TOKEN):
            raise HTTPException(status_code=400, detail="TikTok ads not configured")
        adapter = TikTokAdsAdapter(db, advertiser_id=settings.TIKTOK_ADS_ADVERTISER_ID, access_token=settings.TIKTOK_ADS_ACCESS_TOKEN)
        try:
            return await adapter.set_delivery(campaign_id, body.active)
        except TikTokAdsAPIError as e:
            _raise_http_for_tiktok_error(e)

    if not (settings.META_AD_ACCOUNT_ID and settings.META_ADS_ACCESS_TOKEN):
        raise HTTPException(status_code=400, detail="Meta ads not configured")

    adapter = MetaAdPlatformAdapter(db, access_token=settings.META_ADS_ACCESS_TOKEN)
    try:
        result = await adapter.set_delivery(campaign_id, body.active)
    except MetaAPIError as e:
        # Same self-heal the campaign LIST already does, which this endpoint was
        # missing: a campaign deleted on Meta's side (in Ads Manager, or a manual
        # cleanup) left a stale row here, and pausing it surfaced Meta's raw
        # "Deleted campaigns can't be edited" text as a 502 — live-confirmed on a
        # real record. Drop our own row and tell the caller plainly instead.
        if e.subcode == META_DELETED_CAMPAIGN_SUBCODE:
            await db["jane_ads_meta_campaigns"].delete_one({"campaign_id": campaign_id})
            raise HTTPException(
                status_code=410,
                detail="That campaign no longer exists on Meta — it was deleted. "
                       "We've removed it from your list.",
            )
        _raise_http_for_meta_error(e)
    return result


@router.delete("/meta/campaigns/{campaign_id}")
async def delete_meta_campaign(
    campaign_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
) -> dict:
    """Permanently delete a campaign — from the caller's own campaign-management view,
    scoped to their active brand so a campaign_id can't be deleted by anyone outside
    the brand that owns it. Removes it from OUR list too, not just the platform's side.
    Looks the campaign up across both platform collections, same as the status
    endpoint above."""
    from app.core.config import settings
    from .adapters.meta import MetaAdPlatformAdapter, MetaAPIError
    from .adapters.tiktok import TikTokAdsAdapter, TikTokAdsAPIError

    brand_id = brand_ctx.get("brand_id")
    record = await db["jane_ads_meta_campaigns"].find_one({"campaign_id": campaign_id})
    is_tiktok = False
    if not record:
        record = await db["jane_ads_tiktok_campaigns"].find_one({"campaign_id": campaign_id})
        is_tiktok = True
    if not record or record.get("brand_id") != brand_id:
        raise HTTPException(status_code=404, detail="Campaign not found")

    if is_tiktok:
        if not (settings.TIKTOK_ADS_ADVERTISER_ID and settings.TIKTOK_ADS_ACCESS_TOKEN):
            raise HTTPException(status_code=400, detail="TikTok ads not configured")
        adapter = TikTokAdsAdapter(db, advertiser_id=settings.TIKTOK_ADS_ADVERTISER_ID, access_token=settings.TIKTOK_ADS_ACCESS_TOKEN)
        try:
            await adapter.delete_campaign(campaign_id)
        except TikTokAdsAPIError as e:
            _raise_http_for_tiktok_error(e)
        await db["jane_ads_tiktok_campaigns"].delete_one({"campaign_id": campaign_id})
        return {"deleted": True}

    if not (settings.META_AD_ACCOUNT_ID and settings.META_ADS_ACCESS_TOKEN):
        raise HTTPException(status_code=400, detail="Meta ads not configured")

    adapter = MetaAdPlatformAdapter(db, access_token=settings.META_ADS_ACCESS_TOKEN)
    try:
        await adapter.delete_campaign(campaign_id)
    except MetaAPIError as e:
        _raise_http_for_meta_error(e)

    await db["jane_ads_meta_campaigns"].delete_one({"campaign_id": campaign_id})
    return {"deleted": True}


@router.get("/demo", response_class=HTMLResponse)
async def demo_page() -> str:
    return _DEMO_HTML


_DEMO_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Jane + Ads — Decision Engine</title>
<style>
  :root { --pink:#C2185B; --ink:#111; --muted:#888; --bg:#faf8f7; }
  * { box-sizing:border-box; }
  body { font-family:-apple-system,Segoe UI,Roboto,sans-serif; margin:0; background:var(--bg); color:var(--ink); }
  .wrap { max-width:720px; margin:0 auto; padding:32px 20px 60px; }
  h1 { font-size:22px; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:14px; margin:0 0 24px; }
  .card { background:#fff; border:1px solid #eee; border-radius:14px; padding:20px; margin-bottom:16px; }
  label { display:block; font-size:12px; font-weight:700; color:#555; margin:12px 0 4px; text-transform:uppercase; letter-spacing:.4px; }
  input[type=text], input[type=number], select { width:100%; padding:10px 12px; border:1.5px solid #e0dcd9; border-radius:9px; font-size:14px; }
  .row { display:flex; gap:12px; } .row > div { flex:1; }
  .chk { display:flex; align-items:center; gap:8px; margin-top:14px; font-size:14px; }
  button { margin-top:18px; width:100%; padding:13px; border:none; border-radius:10px;
    background:linear-gradient(135deg,#C2185B,#8E1545); color:#fff; font-weight:800; font-size:15px; cursor:pointer; }
  .examples { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
  .ex { font-size:12px; padding:5px 10px; border:1px solid #e0dcd9; border-radius:20px; background:#fff; cursor:pointer; }
  .out { display:none; }
  .verdict { font-size:18px; font-weight:800; margin:0 0 6px; }
  .why { color:#444; font-style:italic; margin:0 0 16px; }
  .plat { display:flex; justify-content:space-between; align-items:center; padding:12px 14px; border:1.5px solid #C2185B33; background:#fff8fb; border-radius:10px; margin-bottom:8px; }
  .plat b { font-size:15px; } .plat .meta { color:var(--muted); font-size:12px; }
  .sim { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:8px; }
  .kpi { background:#f6f5f3; border-radius:10px; padding:12px; }
  .kpi .n { font-size:20px; font-weight:800; } .kpi .l { font-size:11px; color:var(--muted); text-transform:uppercase; }
  .ok { color:#16a34a; font-weight:700; } .advise { color:var(--pink); font-weight:700; }
  .pill { display:inline-block; font-size:11px; font-weight:700; padding:3px 9px; border-radius:20px; background:#eee; color:#555; text-transform:uppercase; }
  .thinking { font-size:13px; font-weight:700; color:var(--pink); margin:0 0 12px; }
  .steps { list-style:none; padding:0; margin:0 0 18px; counter-reset:s; }
  .steps li { position:relative; padding:9px 12px 9px 40px; margin-bottom:7px; background:#f6f5f3;
    border-left:3px solid var(--pink); border-radius:6px; font-size:13px; color:#333;
    opacity:0; transform:translateY(6px); transition:opacity .3s, transform .3s; }
  .steps li.show { opacity:1; transform:none; }
  .steps li::before { counter-increment:s; content:counter(s); position:absolute; left:10px; top:9px;
    width:20px; height:20px; border-radius:50%; background:var(--pink); color:#fff;
    font-size:11px; font-weight:800; display:flex; align-items:center; justify-content:center; }
  .divider { border:0; border-top:1px dashed #ddd; margin:16px 0; }
  /* Decision-tree diagram */
  .tree-wrap { margin:0 0 16px; }
  .tree-wrap > summary { cursor:pointer; font-weight:800; font-size:14px; color:var(--pink);
    padding:14px 16px; background:#fff; border:1px solid #eee; border-radius:12px; list-style:none; }
  .tree-wrap > summary::-webkit-details-marker { display:none; }
  .tree-wrap[open] > summary { border-radius:12px 12px 0 0; border-bottom:none; }
  .tree { background:#fff; border:1px solid #eee; border-top:none; border-radius:0 0 12px 12px; padding:6px 16px 18px; }
  .lane { border:1.5px dashed var(--pink); border-radius:10px; padding:11px 14px; background:#fff8fb; }
  .lane .tag { display:block; font-size:10px; font-weight:800; letter-spacing:.6px; color:var(--pink); text-transform:uppercase; margin-bottom:3px; }
  .lane .quote { font-style:italic; color:#444; font-size:13px; margin:0; }
  .lane .inputs { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
  .chip { font-size:11px; font-weight:700; padding:3px 9px; border-radius:20px; background:#f0eded; color:#555; }
  .flow { text-align:center; color:var(--muted); font-size:16px; line-height:1; margin:5px 0; }
  .flow small { display:block; font-size:10px; letter-spacing:.4px; text-transform:uppercase; margin-top:2px; }
  .rail { border-top:2px solid var(--pink); text-align:center; margin:12px 0 10px; }
  .rail span { position:relative; top:-9px; background:#fff; padding:0 10px; font-size:10px; font-weight:800;
    letter-spacing:1px; text-transform:uppercase; color:var(--pink); }
  .node { display:flex; gap:11px; padding:10px 12px; border-radius:9px; background:#f6f5f3; margin-bottom:7px; }
  .node .num { flex-shrink:0; width:22px; height:22px; border-radius:50%; background:var(--pink); color:#fff;
    font-size:11px; font-weight:800; display:flex; align-items:center; justify-content:center; }
  .node .body { font-size:13px; color:#222; } .node .body b { color:#111; }
  .branchset { display:flex; flex-wrap:wrap; gap:6px; margin-top:7px; }
  .branch { font-size:11px; font-weight:700; padding:3px 9px; border-radius:6px; border:1px solid #C2185B33;
    background:#fff8fb; color:#333; }
  .branch b { color:var(--pink); }
</style></head>
<body><div class="wrap">
  <h1>Jane + Ads — Decision Engine</h1>
  <p class="sub">Goal first, behaviour next, business type is only a hint — decided per campaign, always explained. Pick a scenario or fill it in; Jane reasons it out live from the real engine.</p>

  <div class="card" id="authCard">
    <label>🔑 Log in to use your real brand playbook, upload your own media, or pick a draft</label>
    <div class="row">
      <div><input type="email" id="authEmail" placeholder="email"/></div>
      <div><input type="password" id="authPassword" placeholder="password"/></div>
    </div>
    <button onclick="doLogin()" style="margin-top:10px">Log in</button>
    <div id="authStatus" style="font-size:12px;color:#888;margin-top:8px"></div>
    <button type="button" onclick="viewLog()" style="margin-top:10px;width:auto;padding:9px 14px;background:#555">📋 View decision log</button>
    <div id="logPanel"></div>
  </div>

  <div class="card" id="oneShotCard" style="border:2px solid #C2185B">
    <label>🎤 Talk to Jane → real ad in Ads Manager (the full flow, one shot)</label>
    <p class="sub" style="margin:2px 0 10px">Type a plain-English ask. Jane understands it, decides the platform, writes the copy, generates the image, and pushes a real campaign to Meta — created PAUSED, zero spend.</p>
    <textarea id="osMsg" rows="2" style="width:100%;box-sizing:border-box;font-size:14px;padding:10px 12px;border:1.5px solid #C2185B55;border-radius:9px;resize:vertical;font-family:inherit;color:#111" placeholder="e.g. I run a skincare brand in Lekki, I want people to discover us, budget 20k">I run a skincare brand in Lekki, I want people to discover us this week, budget 20k</textarea>
    <div class="row">
      <div><label>Business name</label><input type="text" id="osBizName" value="GlowUp Skincare"/></div>
      <div><label>Category (hint)</label><input type="text" id="osCategory" value="skincare"/></div>
    </div>
    <button type="button" onclick="launchFromMessage()" style="margin-top:10px;background:linear-gradient(135deg,#C2185B,#8E1545)">🎤 Jane, make &amp; launch this ad</button>
    <div id="osResult" style="margin-top:12px"></div>
  </div>

  <div class="card" id="metaTestCard">
    <label>🔴 Test REAL Meta ads — manual inputs (creates an actual campaign — always PAUSED, zero spend)</label>
    <div class="row">
      <div><label>Business name</label><input type="text" id="metaBizName" value="Test Business"/></div>
      <div><label>Budget (₦, total)</label><input type="number" id="metaBudget" value="15000"/></div>
    </div>
    <div class="row">
      <div><label>Days</label><input type="number" id="metaDays" value="7"/></div>
      <div><label>Image URL (must be a real, public direct-image link)</label><input type="text" id="metaImageUrl" value="https://images.unsplash.com/photo-1506744038136-46273834b3fb?w=1200&amp;h=628&amp;fit=crop&amp;fm=jpg"/></div>
    </div>
    <div class="row">
      <div><label>Headline</label><input type="text" id="metaHeadline" value="Chat With Us"/></div>
      <div><label>Primary text</label><input type="text" id="metaPrimaryText" value="Chat with us on WhatsApp!"/></div>
    </div>
    <button type="button" onclick="launchRealMetaAd()" style="margin-top:10px;background:#C2185B">🔴 Launch real ad (paused)</button>
    <div id="metaTestResult" style="font-size:13px;margin-top:10px"></div>
  </div>

  <details class="tree-wrap">
    <summary>▸ How Jane decides — the logic</summary>
    <div class="tree">
      <div class="lane">
        <span class="tag">Layer 0 · the LLM (Jane) understands</span>
        <p class="quote">"I want people who already know my boutique to find me — they can't reach me on Google."</p>
        <div class="inputs">
          <span class="chip">goal: leads</span>
          <span class="chip">behaviour: search</span>
          <span class="chip">budget: ₦15,000</span>
          <span class="chip">creative: photos</span>
          <span class="chip">geo: Lekki</span>
        </div>
      </div>
      <div class="flow">↓<small>hands structured inputs to the rule engine</small></div>
      <div class="rail"><span>Deterministic decision tree</span></div>

      <div class="node"><div class="num">1</div><div class="body"><b>Goal leads.</b> The goal of THIS campaign drives everything — decided per campaign, never per business.</div></div>
      <div class="node"><div class="num">2</div><div class="body"><b>Behaviour.</b> Business type sets a default; the user's stated behaviour or the goal overrides it.
        <div class="branchset"><span class="branch">default (hint)</span><span class="branch">→ user override</span><span class="branch">→ goal implication</span></div></div></div>
      <div class="node"><div class="num">3</div><div class="body"><b>Behaviour → platforms.</b>
        <div class="branchset"><span class="branch"><b>search</b> → Google</span><span class="branch"><b>discover</b> → Meta / TikTok</span><span class="branch"><b>mixed</b> → Meta + Google</span></div></div></div>
      <div class="node"><div class="num">4</div><div class="body"><b>Creative gate.</b> No native video → TikTok removed. Google Search needs no creative.</div></div>
      <div class="node"><div class="num">5</div><div class="body"><b>Budget gate.</b>
        <div class="branchset"><span class="branch">below floor → <b>advise</b> (pool / top up)</span><span class="branch">small → <b>one</b> best fit</span><span class="branch">funds several → <b>run several</b></span></div></div></div>
      <div class="node"><div class="num">6</div><div class="body"><b>Geography.</b> Radius / city / pin — a targeting setting WITHIN the platform, not a reason to switch platforms.</div></div>
      <div class="node"><div class="num">7</div><div class="body"><b>Recommend + explain.</b> Name the platform(s) AND explain why, in plain language — always. Both caps (per-business + per-account) attached.</div></div>
    </div>
  </details>

  <div class="card">
    <label>💬 Tell Jane what you want — in plain English</label>
    <textarea id="msg" rows="2" style="width:100%;box-sizing:border-box;font-size:14px;padding:10px 12px;border:1.5px solid #C2185B55;border-radius:9px;resize:vertical;font-family:inherit;color:#111" placeholder="e.g. I run a small restaurant in Surulere, I want more lunch customers this week, I've got 10k"></textarea>
    <button onclick="talk()" style="margin-top:10px">Talk to Jane</button>
    <div style="text-align:center;font-size:12px;color:#aaa;margin:12px 0 2px">— or fill it in manually —</div>
    <div class="row">
      <div><label>Business name</label><input type="text" id="name" value="Ada's Closet"/></div>
      <div><label>Category (hint only)</label><input type="text" id="cat" value="fashion"/></div>
    </div>
    <div class="row">
      <div><label>Goal of this campaign</label>
        <select id="goal">
          <option value="messages">Messages (WhatsApp)</option>
          <option value="leads">Leads</option>
          <option value="bookings">Bookings</option>
          <option value="walk_ins">Walk-ins</option>
          <option value="awareness">Awareness</option>
          <option value="sales">Sales</option>
        </select></div>
      <div><label>Budget (₦)</label><input type="number" id="budget" value="10000"/></div>
    </div>
    <label>How do customers buy this? (override the hint)</label>
    <select id="beh">
      <option value="">— use the business-type default —</option>
      <option value="search">They SEARCH for it (Google)</option>
      <option value="discover">They DISCOVER it scrolling (Meta/TikTok)</option>
      <option value="mixed">Both</option>
    </select>
    <label>City / area — enables pin-and-pocket targeting (optional)</label>
    <input type="text" id="city" placeholder="e.g. Surulere, Lagos, Lekki"/>
    <label class="chk"><input type="checkbox" id="video"/> Has native video (enables TikTok)</label>
    <label class="chk"><input type="checkbox" id="newthing"/> Brand-new thing nobody searches for yet</label>
    <label class="chk"><input type="checkbox" id="demand"/> People already look for this</label>
    <div class="examples">
      <span class="ex" onclick="ex({name:'Mama Kitchen',cat:'restaurant',goal:'messages',budget:10000,city:'Surulere'})">Lunch spot · Surulere pins</span>
      <span class="ex" onclick="ex({name:'Prime Homes',cat:'luxury real estate',goal:'leads',budget:60000,city:'Lagos'})">Luxury realtor · wealth pockets</span>
      <span class="ex" onclick="ex({name:'Ada Closet',cat:'fashion',goal:'leads',budget:15000,beh:'search'})">Fashion · they SEARCH my name</span>
      <span class="ex" onclick="ex({name:'Okafor Clinic',cat:'clinic',goal:'awareness',budget:10000,newthing:true})">Clinic · new-service launch</span>
      <span class="ex" onclick="ex({name:'GlowUp',cat:'skincare',goal:'awareness',budget:60000,video:true,city:'Lekki'})">Skincare ₦60k +video</span>
      <span class="ex" onclick="ex({name:'Tiny Shop',cat:'fashion',goal:'messages',budget:2000})">Tiny ₦2k</span>
    </div>
    <button onclick="run()">Ask Jane</button>
  </div>

  <div class="card out" id="out"></div>
</div>
<script>
async function talk(){
  const msg=document.getElementById('msg').value;
  const out=document.getElementById('out');out.style.display='block';
  const esc=t=>String(t||'').replace(/</g,'&lt;');
  out.innerHTML='<p class="thinking">🧠 Jane is reading your message…</p>';
  let d;
  try{
    const r=await fetch('/jane-ads/understand',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
    if(!r.ok) throw new Error('HTTP '+r.status);
    d=await r.json();
  }catch(e){
    out.innerHTML='<p class="verdict advise">Couldn\\'t reach Jane</p><p class="why">The server was busy reloading. Try again.</p>';return;
  }
  const u=d.understood||{};
  // Fill the form from what Jane understood — visible proof she parsed the sentence.
  if(u.business_name) document.getElementById('name').value=u.business_name;
  if(u.category) document.getElementById('cat').value=u.category;
  if(u.goal) document.getElementById('goal').value=u.goal;
  if(u.budget_ngn) document.getElementById('budget').value=u.budget_ngn;
  document.getElementById('beh').value=u.stated_behaviour||'';
  if(u.city) document.getElementById('city').value=u.city;
  document.getElementById('video').checked=!!u.has_video;
  document.getElementById('newthing').checked=!!u.is_new_thing;
  document.getElementById('demand').checked=!!u.has_existing_demand;
  const chips='<div class="branchset" style="margin:6px 0 0">'+
    (u.category?'<span class="chip">'+esc(u.category)+'</span>':'')+
    (u.goal?'<span class="chip">goal: '+esc(u.goal)+'</span>':'')+
    (u.budget_ngn?'<span class="chip">₦'+Number(u.budget_ngn).toLocaleString()+'</span>':'')+
    (u.city?'<span class="chip">📍 '+esc(u.city)+'</span>':'')+
    (u.stated_behaviour?'<span class="chip">'+esc(u.stated_behaviour)+'</span>':'')+'</div>';
  if(d.decision==='need_more'){
    out.innerHTML='<p class="thinking">🧠 Here\\'s what I understood</p>'+chips+
      '<hr class="divider"/><p class="verdict advise">'+esc(d.question)+'</p>'+
      '<p class="why">Add it above (or in the message) and I\\'ll plan it.</p>';
    return;
  }
  // Understood everything → render the full plan (reuses the form path).
  run();
}
function ex(o){
  document.getElementById('name').value=o.name||'';
  document.getElementById('cat').value=o.cat||'';
  document.getElementById('goal').value=o.goal||'messages';
  document.getElementById('budget').value=o.budget||10000;
  document.getElementById('beh').value=o.beh||'';
  document.getElementById('city').value=o.city||'';
  document.getElementById('video').checked=!!o.video;
  document.getElementById('newthing').checked=!!o.newthing;
  document.getElementById('demand').checked=!!o.demand;
  run();
}
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function run(overridePlatforms, overrideReason){
  const beh=document.getElementById('beh').value;
  const body={business_name:document.getElementById('name').value,category:document.getElementById('cat').value,
    goal:document.getElementById('goal').value,
    budget_ngn:parseFloat(document.getElementById('budget').value||'0'),
    has_video:document.getElementById('video').checked,
    is_new_thing:document.getElementById('newthing').checked,
    has_existing_demand:document.getElementById('demand').checked,
    city:document.getElementById('city').value,
    stated_behaviour:beh||null};
  if(overridePlatforms && overridePlatforms.length){
    body.override_platforms=overridePlatforms; body.override_reason=overrideReason||'';
  }
  const out=document.getElementById('out');out.style.display='block';
  const naira=n=>'₦'+Number(n).toLocaleString();
  const esc=t=>String(t).replace(/</g,'&lt;');
  // 1. Reveal Jane's reasoning steps one at a time.
  out.innerHTML='<p class="thinking">🧠 Jane is working it out…</p><ul class="steps" id="steps"></ul>';
  let d;
  try{
    const r=await fetch('/jane-ads/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    if(!r.ok) throw new Error('HTTP '+r.status);
    d=await r.json();
    if(!d || !d.decision) throw new Error('unexpected response');
  }catch(err){
    out.innerHTML='<p class="verdict advise">Couldn\\'t reach Jane</p>'+
      '<p class="why">The server was busy reloading for a second. Just click Ask Jane again.</p>';
    return;
  }
  const ul=document.getElementById('steps');
  for(const step of (d.trace||[])){
    const li=document.createElement('li');li.innerHTML=esc(step);ul.appendChild(li);
    await sleep(60);li.classList.add('show');await sleep(480);
  }
  document.querySelector('.thinking').textContent='🧠 How Jane decided';
  await sleep(250);
  // 2. Then reveal the verdict below the reasoning.
  if(d.decision==='advise'){
    out.insertAdjacentHTML('beforeend','<hr class="divider"/>'+
      '<p class="verdict advise">Jane advises: don\\'t run yet</p>'+
      '<p class="why">'+d.advice.reason+'</p>'+
      (d.advice.can_pool?'<p class="ok">✓ Can pool with similar businesses to clear the floor.</p>':''));
    return;
  }
  let html='<hr class="divider"/>'+
    (d.overridden?'<p class="pill" style="background:#C2185B22;color:#C2185B;display:inline-block;margin-bottom:8px">↺ overridden — Jane recommended '+(d.jane_recommended_platforms||[]).map(p=>p.toUpperCase()).join(' + ')+'</p>':'')+
    '<p class="verdict">'+d.platforms.map(p=>p.platform.toUpperCase()).join(' + ')+'</p>'+
    '<span class="pill">goal: '+d.goal+'</span> <span class="pill">'+d.behaviour+'</span> '+
    '<span class="pill">cap '+naira(d.per_business_cap_ngn)+'</span>'+
    '<p class="why">"'+d.explanation+'"</p>';
  d.platforms.forEach(p=>{html+='<div class="plat"><b>'+p.platform.toUpperCase()+'</b>'+
    '<span class="meta">'+naira(p.budget_ngn)+' · '+p.days+' days · '+p.variants+' variant(s) · test: '+p.test_scope+'</span></div>';});
  html+='<div style="margin-top:14px;padding:12px 14px;background:#f6f5f3;border-radius:10px">'+
    '<div style="font-size:12px;font-weight:700;color:#555;margin-bottom:8px">Not what you expected? Override Jane\\'s platform choice:</div>'+
    '<div class="branchset" id="ovrPlats">'+
      ['meta','google','tiktok'].map(p=>'<label class="chip" style="cursor:pointer"><input type="checkbox" value="'+p+'" style="margin-right:4px"/>'+p.toUpperCase()+'</label>').join('')+
    '</div>'+
    '<input type="text" id="ovrReason" placeholder="why? (optional)" style="margin-top:8px;width:100%;box-sizing:border-box;padding:8px 10px;border:1.5px solid #e0dcd9;border-radius:8px;font-size:13px"/>'+
    '<button type="button" onclick="runOverride()" style="background:#555;margin-top:8px;padding:9px 14px;width:auto">↺ Run with my choice instead</button>'+
  '</div>';
  if(d.geo){
    const g=d.geo;
    html+='<p class="thinking" style="margin-top:18px">📍 Geo — '+(g.mode==='watering_hole'?'watering-hole (go to where they gather)':'own-radius (pull them in)')+'</p>';
    if(g.pins && g.pins.length){
      g.pins.forEach(pin=>{html+='<div class="plat"><b>'+esc(pin.name)+'</b>'+
        '<span class="meta">~'+pin.radius_km+'km · '+esc(pin.reason||'')+'</span></div>';});
      html+='<p class="why">"'+esc(g.explanation)+'"</p>';
    } else {
      html+='<p class="why">"'+esc(g.explanation)+'"</p>';
    }
  }
  const s=d.simulation;
  const priceLabel = s.price_min_ngn===s.price_max_ngn
    ? naira(s.price_max_ngn)
    : naira(s.price_min_ngn)+'→'+naira(s.price_max_ngn)+' (dynamic)';
  const convLabel = s.prepaid_stopped
    ? s.conversations_charged+' of '+s.conversations_delivered+' (prepaid cap hit)'
    : s.conversations_charged;
  html+='<p class="thinking" style="margin-top:18px">💳 Real wallet — top up, charge, prepaid-first</p>'+
    '<div class="sim">'+
    '<div class="kpi"><div class="n">'+convLabel+'</div><div class="l">Conversations charged</div></div>'+
    '<div class="kpi"><div class="n">'+priceLabel+'</div><div class="l">Price / conversation</div></div>'+
    '<div class="kpi"><div class="n">'+naira(s.wallet_before_ngn)+' → '+naira(s.wallet_after_ngn)+'</div><div class="l">Wallet balance</div></div>'+
    '<div class="kpi"><div class="n '+(s.cap_respected?'ok':'')+'">'+(s.cap_respected?'✓ within cap':'✗ over cap')+'</div><div class="l">Spend ('+naira(s.spent_ngn)+')</div></div>'+
    '</div>';
  const loggedIn=!!authToken;
  html+='<div style="margin-top:16px">'+
    '<div class="branchset" style="margin-bottom:8px">'+
      '<button type="button" onclick="genCreative(\\'generate\\')" style="background:#111;width:auto;margin:0;padding:10px 16px">🎨 Generate'+(loggedIn?' (my brand)':'')+'</button>'+
      '<button type="button" onclick="triggerUpload()" style="background:'+(loggedIn?'#555':'#ccc')+';width:auto;margin:0;padding:10px 16px" '+(loggedIn?'':'disabled title="log in first"')+'>📤 Upload my own photo/video</button>'+
      '<button type="button" onclick="pickDraft()" style="background:'+(loggedIn?'#555':'#ccc')+';width:auto;margin:0;padding:10px 16px" '+(loggedIn?'':'disabled title="log in first"')+'>🗂️ Pick from my drafts</button>'+
    '</div>'+
    '<input type="file" id="uploadFile" accept="image/png,image/jpeg,image/webp,video/mp4,video/quicktime,video/webm" style="display:none" onchange="uploadAndGenerate()"/>'+
  '</div><div id="creative"></div>';
  out.insertAdjacentHTML('beforeend',html);
}
function runOverride(){
  const checked=[...document.querySelectorAll('#ovrPlats input:checked')].map(c=>c.value);
  if(!checked.length){ alert('Pick at least one platform to override with.'); return; }
  run(checked, document.getElementById('ovrReason').value);
}
async function viewLog(){
  if(!authToken){ alert('Log in first to view the decision log.'); return; }
  const panel=document.getElementById('logPanel');
  const esc=t=>String(t||'').replace(/</g,'&lt;');
  panel.innerHTML='<p class="thinking" style="margin-top:10px">📋 Loading…</p>';
  let d;
  try{
    const r=await fetch('/jane-ads/instrumentation/demo',{headers:{'Authorization':'Bearer '+authToken}});
    d=await r.json();
    if(!r.ok) throw new Error(d.detail||('HTTP '+r.status));
  }catch(e){ panel.innerHTML='<p class="why">Could not load the log: '+esc(e.message||e)+'</p>'; return; }
  let h='<hr class="divider"/><p class="thinking">📋 Decisions ('+d.decisions.length+')</p>';
  if(!d.decisions.length) h+='<p class="why">No decisions logged yet — run a plan above.</p>';
  d.decisions.forEach(dec=>{
    const plats=(dec.overridden?dec.final_platforms:dec.jane_platforms).map(p=>p.toUpperCase()).join(' + ')||'—';
    h+='<div class="plat"><b>'+dec.decision.toUpperCase()+(dec.overridden?' <span class="pill" style="background:#C2185B22;color:#C2185B">overridden</span>':'')+'</b>'+
      '<span class="meta">'+esc(plats)+' · '+new Date(dec.at).toLocaleString()+'</span></div>';
  });
  if(d.overrides.length){
    h+='<p class="thinking" style="margin-top:14px">↺ Overrides ('+d.overrides.length+')</p>';
    d.overrides.forEach(o=>{
      h+='<div class="plat"><b>'+o.jane_platforms.map(p=>p.toUpperCase()).join(' + ')+' → '+o.user_platforms.map(p=>p.toUpperCase()).join(' + ')+'</b>'+
        '<span class="meta">'+esc(o.reason||'no reason given')+'</span></div>';
    });
  }
  panel.innerHTML=h;
}

async function launchRealMetaAd(){
  if(!authToken){ alert('Log in first to launch a real Meta ad.'); return; }
  const box=document.getElementById('metaTestResult');
  const esc=t=>String(t||'').replace(/</g,'&lt;');
  box.innerHTML='<p class="thinking">🔴 Creating a real (paused) campaign on Meta…</p>';
  const body={
    business_name: document.getElementById('metaBizName').value,
    budget_ngn: parseFloat(document.getElementById('metaBudget').value||'0'),
    days: parseInt(document.getElementById('metaDays').value||'7', 10),
    image_url: document.getElementById('metaImageUrl').value,
    headline: document.getElementById('metaHeadline').value,
    primary_text: document.getElementById('metaPrimaryText').value,
  };
  let d;
  try{
    const r=await fetch('/jane-ads/meta/test-launch',{method:'POST',
      headers:{'Content-Type':'application/json','Authorization':'Bearer '+authToken},
      body:JSON.stringify(body)});
    d=await r.json();
    if(!r.ok) throw new Error(d.detail||('HTTP '+r.status));
  }catch(e){ box.innerHTML='<p class="why">Launch failed: '+esc(e.message||e)+'</p>'; return; }
  box.innerHTML='<div class="plat"><b>✅ Real campaign created</b>'+
    '<span class="meta">campaign_id: '+esc(d.campaign_id)+'</span></div>'+
    '<p class="why">'+esc(d.note)+'</p>'+
    '<a href="'+d.ads_manager_url+'" target="_blank" rel="noopener">Open in Ads Manager →</a>';
}

async function launchFromMessage(){
  if(!authToken){ alert('Log in first (top card) to run the full flow.'); return; }
  const box=document.getElementById('osResult');
  const esc=t=>String(t||'').replace(/</g,'&lt;');
  const naira=n=>'₦'+Number(n).toLocaleString();
  box.innerHTML='<p class="thinking">🧠 Jane is reading your message → deciding the platform → writing copy → generating a real AI image → pushing to Meta.<br/>This takes ~60–90s (the AI image is the slow part). Hang tight…</p>';
  const body={
    message: document.getElementById('osMsg').value,
    business_name: document.getElementById('osBizName').value,
    category: document.getElementById('osCategory').value,
  };
  let d;
  try{
    const r=await fetch('/jane-ads/meta/launch-from-message',{method:'POST',
      headers:{'Content-Type':'application/json','Authorization':'Bearer '+authToken},
      body:JSON.stringify(body)});
    d=await r.json();
    if(!r.ok) throw new Error(d.detail||('HTTP '+r.status));
  }catch(e){ box.innerHTML='<p class="why">Failed: '+esc(e.message||e)+'</p>'; return; }

  if(d.stage==='need_more'){
    box.innerHTML='<p class="verdict advise">'+esc(d.question)+'</p><p class="why">Add it to the message and try again.</p>';
    return;
  }
  if(d.stage==='advise'){
    box.innerHTML='<p class="verdict advise">Jane advises: don\\'t run yet</p><p class="why">'+esc(d.advice.reason)+'</p>';
    return;
  }
  const u=d.understood||{}, pl=d.plan||{}, cr=d.creative||{}, la=d.launch||{};
  let h='<hr class="divider"/>';
  // 1. What Jane understood
  h+='<p class="thinking">🧠 What Jane understood</p><div class="branchset">'+
    (u.business_name?'<span class="chip">'+esc(u.business_name)+'</span>':'')+
    (u.category?'<span class="chip">'+esc(u.category)+'</span>':'')+
    (u.goal?'<span class="chip">goal: '+esc(u.goal)+'</span>':'')+
    (u.budget_ngn?'<span class="chip">'+naira(u.budget_ngn)+'</span>':'')+
    (u.city?'<span class="chip">📍 '+esc(u.city)+'</span>':'')+'</div>';
  // 2. Jane's decision
  h+='<p class="thinking" style="margin-top:14px">🎯 Jane\\'s plan</p>';
  if(d.forced_to_meta) h+='<p class="pill" style="background:#C2185B22;color:#C2185B;display:inline-block">Jane leaned '+(d.jane_recommended_platforms||[]).join(' + ').toUpperCase()+' — forced to META (only live adapter for now)</p>';
  h+='<p class="why">"'+esc(pl.explanation)+'"</p>';
  (pl.platforms||[]).forEach(p=>{h+='<div class="plat"><b>'+p.platform.toUpperCase()+'</b><span class="meta">'+naira(p.budget_ngn)+' · '+p.days+' days · '+p.variants+' variant(s) · test: '+p.test_scope+'</span></div>';});
  if(pl.geo && pl.geo.pins && pl.geo.pins.length){
    h+='<p class="why" style="margin-top:6px">📍 Targeting: '+pl.geo.pins.map(x=>esc(x.name)).join(', ')+'</p>';
  }
  // 3. The generated creative
  h+='<p class="thinking" style="margin-top:14px">🎨 The ad Jane made</p>';
  if(cr.image_url){ h+='<img src="'+cr.image_url+'" alt="ad" style="width:100%;max-width:260px;border-radius:12px;display:block;margin:8px 0"/>'; }
  h+='<p class="verdict" style="font-size:16px">'+esc(cr.headline)+'</p>'+
     '<p class="why">"'+esc(cr.primary_text)+'"</p>'+
     '<div class="plat"><b>Call to action</b><span class="meta">'+esc(cr.cta)+'</span></div>';
  // 4. The live campaign
  h+='<hr class="divider"/><div class="plat"><b>✅ Pushed to Meta (PAUSED)</b><span class="meta">campaign '+esc(la.campaign_id)+'</span></div>'+
     '<p class="why">'+esc(la.note)+'</p>'+
     '<a href="'+la.ads_manager_url+'" target="_blank" rel="noopener" style="font-weight:800;color:#C2185B">Open in Ads Manager →</a>';
  box.innerHTML=h;
}

// ── Auth (needed for brand-playbook / upload / draft sources) ────────────────
let authToken = localStorage.getItem('janeAdsToken') || '';
let authEmail = localStorage.getItem('janeAdsEmail') || '';
function updateAuthStatus(){
  document.getElementById('authStatus').textContent = authToken ? ('✓ Logged in as '+authEmail) : '';
  document.getElementById('authEmail').style.display = authToken ? 'none' : '';
  document.getElementById('authPassword').style.display = authToken ? 'none' : '';
}
updateAuthStatus();
async function doLogin(){
  const email=document.getElementById('authEmail').value;
  const password=document.getElementById('authPassword').value;
  const status=document.getElementById('authStatus');
  status.textContent='Logging in…';
  try{
    const r=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})});
    const d=await r.json();
    const token=d.responseData && d.responseData.accessToken;
    if(!token) throw new Error(d.responseMessage||'login failed');
    authToken=token; authEmail=email;
    localStorage.setItem('janeAdsToken',token); localStorage.setItem('janeAdsEmail',email);
    updateAuthStatus();
  }catch(e){ status.textContent='Login failed — check your credentials.'; }
}

// ── Ad creative — anonymous (generate only) vs authenticated (all 3 sources) ─
function renderCreative(a){
  const esc=t=>String(t||'').replace(/</g,'&lt;');
  const box=document.getElementById('creative');
  let h='<hr class="divider"/>';
  if(a.source) h+='<span class="pill">source: '+esc(a.source)+'</span>';
  if(a.image_url && a.is_video){
    h+='<video src="'+a.image_url+'" controls style="width:100%;max-width:280px;border-radius:12px;display:block;margin:10px auto"></video>';
  } else if(a.image_url){
    h+='<img src="'+a.image_url+'" alt="ad" style="width:100%;max-width:280px;border-radius:12px;display:block;margin:10px auto"/>';
  } else {
    h+='<p class="why">(Media unavailable — showing copy only.)</p>';
  }
  h+='<p class="verdict" style="font-size:16px">'+esc(a.headline)+'</p>'+
     '<p class="why">"'+esc(a.primary_text)+'"</p>'+
     '<div class="plat"><b>Call to action</b><span class="meta">'+esc(a.cta)+'</span></div>';
  box.innerHTML=h;
}
async function genCreative(source, extra){
  extra = extra || {};
  const box=document.getElementById('creative');
  box.innerHTML='<p class="thinking" style="margin-top:14px">🎨 Jane is making the ad…</p>';
  const base={business_name:document.getElementById('name').value,category:document.getElementById('cat').value,
    goal:document.getElementById('goal').value,description:'',city:document.getElementById('city').value};
  let url='/jane-ads/creative', headers={'Content-Type':'application/json'}, body=base;
  if(authToken){
    url='/jane-ads/creative/for-brand';
    headers['Authorization']='Bearer '+authToken;
    body={...base, source, ...extra};
  }
  let a;
  try{
    const r=await fetch(url,{method:'POST',headers,body:JSON.stringify(body)});
    a=await r.json();
    if(!r.ok) throw new Error(a.detail||('HTTP '+r.status));
  }catch(e){ box.innerHTML='<p class="why">Couldn\\'t generate the creative: '+String(e.message||e)+'</p>'; return; }
  renderCreative(a);
}
function triggerUpload(){
  if(!authToken){ alert('Log in first to upload your own photo.'); return; }
  document.getElementById('uploadFile').click();
}
async function uploadAndGenerate(){
  const input=document.getElementById('uploadFile');
  const file=input.files[0];
  if(!file) return;
  const box=document.getElementById('creative');
  const isVid=file.type.startsWith('video/');
  box.innerHTML='<p class="thinking" style="margin-top:14px">📤 Uploading your '+(isVid?'video':'photo')+'…</p>';
  const form=new FormData(); form.append('file',file);
  try{
    const r=await fetch('/jane-ads/creative/upload',{method:'POST',headers:{'Authorization':'Bearer '+authToken},body:form});
    const d=await r.json();
    if(!r.ok) throw new Error(d.detail||'upload failed');
    await genCreative('upload',{reference_image_url:d.url,is_video:d.is_video});
  }catch(e){ box.innerHTML='<p class="why">Upload failed: '+String(e.message||e)+'</p>'; }
}
async function pickDraft(){
  if(!authToken){ alert('Log in first to pick from your drafts.'); return; }
  const box=document.getElementById('creative');
  box.innerHTML='<p class="thinking" style="margin-top:14px">🗂️ Loading your drafts…</p>';
  try{
    const r=await fetch('/jane-ads/creative/drafts',{headers:{'Authorization':'Bearer '+authToken}});
    const d=await r.json();
    const drafts=d.drafts||[];
    if(!drafts.length){ box.innerHTML='<p class="why">No drafts with images found yet.</p>'; return; }
    let h='<p class="thinking" style="margin-top:14px">🗂️ Pick a draft</p><div class="branchset">';
    drafts.forEach(dr=>{
      h+='<img src="'+dr.image_url+'" alt="draft" style="width:64px;height:96px;object-fit:cover;border-radius:6px;cursor:pointer;border:2px solid transparent" '+
         'onclick="genCreative(\\'draft\\',{draft_id:\\''+dr.draft_id+'\\'})"/>';
    });
    h+='</div>';
    box.innerHTML=h;
  }catch(e){ box.innerHTML='<p class="why">Could not load drafts.</p>'; }
}
</script>
</body></html>"""

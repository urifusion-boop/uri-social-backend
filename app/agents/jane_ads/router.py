"""
Jane + Ads — demo router.

Two endpoints, no auth (internal evidence UI):
  POST /jane-ads/plan   — run the real decision engine + a mock end-to-end
  GET  /jane-ads/demo   — a self-contained HTML page to click through it

The HTML page is served from the backend so it calls /jane-ads/plan same-origin
(no CORS). It uses the ACTUAL decision engine — nothing is duplicated in JS.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.core.auth_bearer import JWTBearer
from app.dependencies import get_active_brand_context, get_db_dependency

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
    Platform,
    PurchaseBehaviour,
)
from .payments import JaneAdsPayments
from .store import InMemoryWalletStore, MongoWalletStore
from .wallet import InsufficientFundsError, MinimumTopUpError, WalletService

router = APIRouter(prefix="/jane-ads", tags=["Jane + Ads (demo)"])


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
    from .nl import parse_message, to_campaign_request
    parsed = await parse_message(body.message, body.business_name, body.category)
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
    source: str = "generate"           # generate | upload | draft
    reference_image_url: str = ""      # required for source=upload
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
    from .creative import creative_from_draft, creative_from_upload, generate_ad_creative
    user_id = brand_ctx["user_id"]
    brand_id = brand_ctx.get("brand_id")

    if body.source == "upload":
        if not body.reference_image_url:
            raise HTTPException(status_code=400, detail="reference_image_url is required for source=upload")
        ad = await creative_from_upload(
            body.business_name, body.category, body.reference_image_url, body.goal,
            body.description, user_id=user_id, db=db, brand_id=brand_id,
            is_video=body.is_video,
        )
    elif body.source == "draft":
        if not body.draft_id:
            raise HTTPException(status_code=400, detail="draft_id is required for source=draft")
        ad = await creative_from_draft(
            body.business_name, body.category, body.draft_id, user_id, db,
            goal=body.goal, brand_id=brand_id,
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


class MetaTestLaunchBody(BaseModel):
    business_name: str = "Test Business"
    budget_ngn: float = Field(15_000, gt=0)
    days: int = Field(7, gt=0)
    image_url: str
    headline: str = "Chat With Us"
    primary_text: str = "Chat with us on WhatsApp!"


@router.post("/meta/test-launch")
async def meta_test_launch(
    body: MetaTestLaunchBody,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    _token: dict = Depends(JWTBearer()),
) -> dict:
    """Launches a REAL Meta campaign (created PAUSED — zero spend until a human
    activates it in Ads Manager) against the configured ad account. Lets anyone test
    the live Meta adapter directly rather than trusting a one-off script. Requires
    META_AD_ACCOUNT_ID, META_ADS_ACCESS_TOKEN, and META_ADS_PAGE_ID to be configured."""
    import uuid
    from app.core.config import settings
    from .adapters.meta import MetaAdPlatformAdapter, MetaAPIError
    from .models import (
        ABTestScope, AdCreative, CampaignPlan, CampaignObjective, Goal, PlatformPlan,
        PurchaseBehaviour, SpendAuthorization,
    )

    if not (settings.META_AD_ACCOUNT_ID and settings.META_ADS_ACCESS_TOKEN and settings.META_ADS_PAGE_ID):
        raise HTTPException(
            status_code=400,
            detail="Meta ads not configured — need META_AD_ACCOUNT_ID, META_ADS_ACCESS_TOKEN, META_ADS_PAGE_ID",
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
        page_id=settings.META_ADS_PAGE_ID,
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


class MetaLaunchFromMessageBody(BaseModel):
    message: str                          # plain-English ask, e.g. "get me lunch customers in Surulere, ₦15k"
    business_name: str = ""
    category: str = ""
    page_id: str = ""                     # override the target page (multi-client); defaults to META_ADS_PAGE_ID
    conversation_cost_ngn: float = Field(500.0, gt=0)
    creative_source: str = "generate"     # generate | upload | draft
    reference_image_url: str = ""         # required for creative_source=upload (from /creative/upload)
    is_video: bool = False                # is reference_image_url a video?
    draft_id: str = ""                    # required for creative_source=draft (from /creative/drafts)


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
    from .creative import creative_from_draft, creative_from_upload, generate_ad_creative
    from .decision_engine import apply_platform_override, plan_campaign
    from .history import get_campaign_history, remembered_budget_ngn, remembered_business_name, remembered_category
    from .nl import parse_message, to_campaign_request
    from .policy import Severity, review_ad_creative

    if not (settings.META_AD_ACCOUNT_ID and settings.META_ADS_ACCESS_TOKEN):
        raise HTTPException(status_code=400, detail="Meta ads not configured — need META_AD_ACCOUNT_ID and META_ADS_ACCESS_TOKEN")
    page_id = body.page_id or settings.META_ADS_PAGE_ID
    if not page_id:
        raise HTTPException(status_code=400, detail="No target page — set META_ADS_PAGE_ID or pass page_id")
    if body.creative_source == "upload" and not body.reference_image_url:
        raise HTTPException(status_code=400, detail="reference_image_url is required for creative_source=upload")
    if body.creative_source == "draft" and not body.draft_id:
        raise HTTPException(status_code=400, detail="draft_id is required for creative_source=draft")
    if body.creative_source == "upload" and body.is_video:
        # MetaAdPlatformAdapter.launch_campaign already rejects this safely (Meta
        # needs a /advideos + video_data shape, not built yet) before creating
        # anything on Meta — but only after the geo/creative work below runs, with
        # an implementation-detail message. Fail fast here instead, with a message
        # the caller can actually act on.
        raise HTTPException(status_code=400, detail="Video ads aren't supported yet — please upload a photo instead.")

    # Tie the campaign to the caller's active brand so it shows up in their
    # campaign list; fall back to a random id only if there's no brand context.
    business_id = brand_ctx.get("brand_id") or f"oneshot_{uuid.uuid4().hex[:8]}"

    # 0.5. Recall — a returning business shouldn't have to re-explain what Jane
    # already learned launching their last campaign here (PRD §6). Only meaningful
    # for a real brand context; a one-shot anonymous business_id has no history.
    history = await get_campaign_history(db, business_id) if brand_ctx.get("brand_id") else []
    known_business_name = body.business_name or remembered_business_name(history)
    known_category = body.category or remembered_category(history)
    known_budget = remembered_budget_ngn(history)

    # 1. Jane reads the plain-English message.
    parsed = await parse_message(body.message, known_business_name, known_category)

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

    # 2. Jane decides the platform + budget split, with her reasoning.
    result = plan_campaign(req, funded_amount_ngn=req.budget_ngn, total_funded_wallets_ngn=req.budget_ngn)
    if result.decision == PlanDecision.ADVISE:
        return {"early_return": {"stage": "advise", "understood": parsed.model_dump(),
                "advice": result.advice.model_dump(), "trace": result.advice.trace}}
    plan = result.plan
    if budget_estimate:
        plan.explanation = (
            f"Based on similar campaigns costing about ₦{budget_estimate['price_per_conversation_ngn']:,.0f} "
            f"per conversation, ₦{budget_estimate['estimated_budget_ngn']:,.0f} should get you around "
            f"{budget_estimate['desired_conversions']} conversations. {plan.explanation}"
        )

    # For now, always launch on Meta — it's the only platform with a live adapter
    # (Google/TikTok are still pending, #7/#8). If Jane's decision landed elsewhere,
    # force the plan onto Meta so the demo always produces a real ad. Jane's original
    # recommendation is still surfaced in the response for transparency.
    jane_platforms = [p.platform.value for p in plan.platforms]
    forced_to_meta = not any(p.platform == Platform.META for p in plan.platforms)
    if forced_to_meta:
        plan = apply_platform_override(plan, [Platform.META])
    else:
        plan.platforms = [p for p in plan.platforms if p.platform == Platform.META]

    # 3. Geo refinement (pin-and-pocket) — best-effort, never blocks planning.
    geo_dump = None
    if parsed.city:
        try:
            from .geo import geo_for_request
            geo_plan = await geo_for_request(req.business_name, req.category, parsed.city, req.goal, req.description)
            plan.geo = geo_plan
            geo_dump = geo_plan.model_dump()
        except Exception as e:
            print(f"[oneshot] geo skipped: {e}", flush=True)

    # 4. The ad creative — Jane generates it, or the caller supplies their own upload/draft.
    business_name = req.business_name or body.business_name or "Your Business"
    category = req.category or body.category
    user_id = brand_ctx.get("user_id", "")
    brand_id = brand_ctx.get("brand_id")
    if body.creative_source == "upload":
        creative = await creative_from_upload(
            business_name, category, body.reference_image_url, req.goal.value, req.description,
            user_id=user_id, db=db, brand_id=brand_id, is_video=body.is_video,
        )
    elif body.creative_source == "draft":
        creative = await creative_from_draft(
            business_name, category, body.draft_id, user_id, db,
            goal=req.goal.value, brand_id=brand_id,
        )
        if creative is None:
            raise HTTPException(status_code=404, detail="Draft not found or has no image")
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
        )
        if creative.image_url:
            # "reason" is a strict Literal on CreditTransaction — "campaign_generation"
            # (the function's own default) is the closest existing match; there's no
            # ad-specific reason value and adding one is a shared-model change beyond
            # this feature's scope.
            await credit_service.deduct_credit(user_id, campaign_id=business_id, reason="campaign_generation")
    if not creative.image_url:
        raise HTTPException(status_code=502, detail="Jane couldn't generate the ad image (creative service). Try again.")

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

    plan.page_id = page_id
    plan.creative = creative

    return _PlanBuildResult(
        business_id=business_id, req=req, plan=plan, jane_platforms=jane_platforms,
        forced_to_meta=forced_to_meta, geo_dump=geo_dump, understood=parsed.model_dump(),
        budget_estimate=budget_estimate,
    )


async def _wallet_status(db: AsyncIOMotorDatabase, business_id: str, budget_ngn: float) -> tuple[float, bool]:
    """(balance, sufficient) — the real Mongo-backed balance, not the claimed budget."""
    from .store import MongoWalletStore
    from .wallet import WalletService

    wallet = WalletService(MongoWalletStore(db))
    balance = await wallet.get_balance(business_id)
    return balance, balance >= budget_ngn


def _wallet_shortfall_message(balance: float, budget_ngn: float) -> str:
    return (
        f"Your ad wallet has ₦{balance:,.0f} — top up ₦{(budget_ngn - balance):,.0f} more "
        f"via /jane-ads/wallet/topup before launching a ₦{budget_ngn:,.0f} campaign."
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
            "geo": built.geo_dump,
            "trace": plan.trace,
        },
        "creative": plan.creative.model_dump(mode="json"),
        "budget_estimate": built.budget_estimate,
    }


async def _do_launch(built: _PlanBuildResult, body_message: str, body_business_name: str, brand_ctx: dict, db: AsyncIOMotorDatabase) -> dict:
    """The actual Meta launch + campaign-record enrichment — shared by the one-shot
    endpoint and /meta/plan/{id}/launch, both of which have already done their own
    wallet-gate check by the time they call this."""
    from app.core.config import settings
    from .adapters.meta import MetaAdPlatformAdapter, MetaAPIError
    from .wallet import WalletService
    from .store import MongoWalletStore

    plan, req, business_id = built.plan, built.req, built.business_id
    wallet = WalletService(MongoWalletStore(db))
    auth = await wallet.authorization_for(business_id, total_funded_wallets_ngn=req.budget_ngn)

    adapter = MetaAdPlatformAdapter(db, access_token=settings.META_ADS_ACCESS_TOKEN)
    try:
        launch = await adapter.launch_campaign(plan, auth)
    except MetaAPIError as e:
        _raise_http_for_meta_error(e)
    except (ValueError, NotImplementedError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Enrich the stored campaign record with display fields so the campaign-list
    # view can render name/creative/budget without re-deriving them.
    await db["jane_ads_meta_campaigns"].update_one(
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
        }},
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
            "status": "PAUSED",
            "note": "Created PAUSED — zero spend. Review and activate in Ads Manager to go live.",
            "ads_manager_url": (
                f"https://adsmanager.facebook.com/adsmanager/manage/campaigns"
                f"?act={settings.META_AD_ACCOUNT_ID}&selected_campaign_ids={launch.campaign_id}"
            ),
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
            "sufficient": sufficient,
        },
    }


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
        budget_estimate=doc.get("budget_estimate"),
    )
    result = await _do_launch(built, doc["message"], doc["business_name"], brand_ctx, db)
    await db["jane_ads_pending_plans"].update_one(
        {"plan_id": plan_id},
        {"$set": {"status": "launched", "campaign_id": result["launch"]["campaign_id"]}},
    )
    return result


@router.get("/meta/campaigns")
async def meta_campaigns(
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    brand_ctx: dict = Depends(get_active_brand_context),
    with_metrics: bool = True,
) -> dict:
    """List the active brand's campaigns for the management view. Each row carries
    its display fields (name, creative, budget) plus — when with_metrics — live
    reach/conversation/spend numbers pulled from the platform. Metrics failures per
    campaign are swallowed so one bad campaign never blanks the whole list."""
    from app.core.config import settings
    from .adapters.meta import MetaAdPlatformAdapter

    brand_id = brand_ctx.get("brand_id")
    if not brand_id:
        return {"campaigns": []}

    records = await (db["jane_ads_meta_campaigns"]
                     .find({"brand_id": brand_id}, {"_id": 0})
                     .sort("created_at", -1).to_list(length=200))

    adapter = None
    if with_metrics and settings.META_ADS_ACCESS_TOKEN and settings.META_AD_ACCOUNT_ID:
        adapter = MetaAdPlatformAdapter(db, access_token=settings.META_ADS_ACCESS_TOKEN)

    out = []
    for r in records:
        created = r.get("created_at")
        row = {
            "campaign_id": r.get("campaign_id"),
            "name": r.get("display_name") or "Campaign",
            "headline": r.get("headline", ""),
            "primary_text": r.get("primary_text", ""),
            "image_url": r.get("image_url", ""),
            "budget_ngn": r.get("budget_ngn"),
            "goal": r.get("goal", ""),
            "city": r.get("city", ""),
            "status": "paused",   # everything is created PAUSED for now
            "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
            "ads_manager_url": (
                f"https://adsmanager.facebook.com/adsmanager/manage/campaigns"
                f"?act={settings.META_AD_ACCOUNT_ID}&selected_campaign_ids={r.get('campaign_id')}"
            ),
            "metrics": None,
        }
        if adapter and r.get("campaign_id"):
            try:
                summary = await adapter.fetch_campaign_summary(r["campaign_id"])
                # A campaign can be deleted by means we never see (directly in Ads
                # Manager, or a manual cleanup) — once Meta itself reports it as
                # gone, drop our own record too instead of showing a stale "Deleted"
                # ghost card forever. This is the ONLY status we self-heal on;
                # everything else (paused/active/in review/etc.) still renders.
                if summary["delivery"] == "Deleted":
                    await db["jane_ads_meta_campaigns"].delete_one({"campaign_id": r["campaign_id"]})
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
    one genuinely consequential action here — real budget can start being spent."""
    from app.core.config import settings
    from .adapters.meta import MetaAdPlatformAdapter, MetaAPIError

    brand_id = brand_ctx.get("brand_id")
    record = await db["jane_ads_meta_campaigns"].find_one({"campaign_id": campaign_id})
    if not record or record.get("brand_id") != brand_id:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if not (settings.META_AD_ACCOUNT_ID and settings.META_ADS_ACCESS_TOKEN):
        raise HTTPException(status_code=400, detail="Meta ads not configured")

    adapter = MetaAdPlatformAdapter(db, access_token=settings.META_ADS_ACCESS_TOKEN)
    try:
        result = await adapter.set_delivery(campaign_id, body.active)
    except MetaAPIError as e:
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
    the brand that owns it. Removes it from OUR list too, not just Meta's side."""
    from app.core.config import settings
    from .adapters.meta import MetaAdPlatformAdapter, MetaAPIError

    brand_id = brand_ctx.get("brand_id")
    record = await db["jane_ads_meta_campaigns"].find_one({"campaign_id": campaign_id})
    if not record or record.get("brand_id") != brand_id:
        raise HTTPException(status_code=404, detail="Campaign not found")
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

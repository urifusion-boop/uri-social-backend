"""
The campaign wizard's "Ads" step: see the ads before the plan.

Jane used to make a campaign's ads only while building its plan, on the last step and after
her follow-up questions, so the owner saw no ad until the very end (staging, 2026-11-25). Here
the wizard asks for the ads as soon as its Creative step is answered: Jane makes them from the
same answers the plan uses (what is sold and to whom, where, the goal, the style), one by one in
the background, and the page shows each as it arrives. The owner can see each one whole, make one
again, remove it or change its words. The plan then runs exactly these ads
(MetaLaunchFromMessageBody.creative_source == "preview"): nothing is made or charged twice.

Each ad costs what an ad made inside the plan costs (ad_image_billing), charged once it is safely
kept, and each is kept in Creatives like every other ad Jane makes.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.jobs import queue
from app.jobs.worker import JobError

PREVIEWS = "ad_previews"
JOB = "wizard_ad_preview"
MAX_ADS = 6
DRAFT_SOURCE = "ad_preview"
_UNAVAILABLE = "Jane couldn't make that ad just now. Try again shortly."

# The wizard's objective → the goal Jane's copywriter writes for (models.Goal).
_GOAL_FOR_OBJECTIVE = {"sales": "sales", "leads": "leads", "traffic": "traffic", "awareness": "awareness",
                       "engagement": "followers", "messages": "messages"}
# Where the button sends people, when the owner picked it in the wizard.
_DESTINATIONS = ("whatsapp", "website", "instagram_dm")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_indexes(db) -> None:
    await db[PREVIEWS].create_index("preview_id", unique=True)
    await db[PREVIEWS].create_index([("brand_id", 1), ("created_at", -1)])


def public(doc: dict) -> dict:
    return {"id": doc["preview_id"], "count": doc["count"], "status": doc["status"],
            "error": doc.get("error", ""), "results": [_public_ad(r) for r in doc.get("results") or []]}


def _public_ad(r: dict) -> dict:
    return {"image_url": r.get("image_url", ""), "headline": r.get("headline", ""),
            "primary_text": r.get("primary_text", ""), "cta": r.get("cta", ""), "angle": r.get("concept", ""),
            "draft_id": r.get("draft_id", ""), "redoing": bool(r.get("redoing"))}


def clean_inputs(*, description: str, city: str = "", objective: str = "", destination_type: str = "",
                 budget_ngn: float = 0, vsg01_format_id: str = "", platform: str = "meta") -> dict:
    description = " ".join((description or "").split())[:600]
    if not description:
        raise ValueError("Say what you're advertising first.")
    return {
        "description": description,
        "city": " ".join((city or "").split())[:120],
        "goal": _GOAL_FOR_OBJECTIVE.get((objective or "").strip().lower(), "messages"),
        "destination_type": destination_type if destination_type in _DESTINATIONS else "",
        "budget_ngn": max(0.0, float(budget_ngn or 0)),
        "vsg01_format_id": (vsg01_format_id or "").strip()[:40],
        "platform": "tiktok" if platform == "tiktok" else "meta",
    }


async def start(db, *, brand_id: str, user_id: str, count: int, inputs: dict) -> dict:
    """Ask Jane for `count` ads. The balance must cover them all before any is made."""
    from app.services.CreditService import credit_service

    from . import ad_image_billing
    count = max(1, min(MAX_ADS, int(count or 1)))
    needed = count if count > 1 else ad_image_billing.credits_needed(inputs.get("vsg01_format_id") or None)
    if not await credit_service.check_sufficient_credits(user_id, required=needed):
        raise PermissionError(f"Making {count} ad{'s' if count != 1 else ''} takes {needed} content credit"
                              f"{'s' if needed != 1 else ''}. Top up, or make fewer.")
    doc = {"preview_id": f"adp_{uuid.uuid4().hex[:16]}", "brand_id": brand_id, "user_id": user_id,
           "count": count, "status": "running", "inputs": inputs, "results": [], "error": "",
           "created_at": _now()}
    await db[PREVIEWS].insert_one(dict(doc))
    job = await queue.enqueue(db, JOB, {"preview_id": doc["preview_id"]}, brand_id=brand_id, user_id=user_id,
                              key=doc["preview_id"], max_attempts=2)
    await db[PREVIEWS].update_one({"preview_id": doc["preview_id"]}, {"$set": {"job_id": job["job_id"]}})
    return public(doc)


async def get(db, brand_id: str, preview_id: str) -> Optional[dict]:
    doc = await db[PREVIEWS].find_one({"brand_id": brand_id, "preview_id": preview_id}, {"_id": 0})
    if doc and doc.get("status") == "running" and doc.get("job_id"):
        # The job can end without the handler having a say (a worker gave up on it). The page
        # polls this, so it must hear that it ended rather than wait forever.
        job = await queue.get(db, doc["job_id"])
        if job and job.get("status") == queue.FAILED:
            message = (job.get("error") or {}).get("message") or "Jane couldn't finish these. Try again shortly."
            change: dict[str, Any] = {"$set": {"status": "failed", "error": message}}
            if doc.get("results"):
                change["$unset"] = {f"results.{i}.redoing": "" for i in range(len(doc["results"]))}
            await db[PREVIEWS].update_one({"preview_id": preview_id, "status": "running"}, change)
            doc = {**doc, "status": "failed", "error": message}
    return public(doc) if doc else None


async def ads_for_plan(db, brand_id: str, preview_id: str) -> list:
    """The finished ads, as the plan runs them: the first is the plan's ad, the rest the others
    it tests. Empty while none is ready."""
    from .models import AdCreative
    doc = await db[PREVIEWS].find_one({"brand_id": brand_id, "preview_id": preview_id}, {"_id": 0})
    if not doc:
        return []
    keep = AdCreative.model_fields.keys()
    return [AdCreative.model_validate({k: v for k, v in r.items() if k in keep})
            for r in doc.get("results") or [] if r.get("image_url") and not r.get("redoing")]


# ── making them ────────────────────────────────────────────────────────────────

async def _profile(db, user_id: str, brand_id: str) -> dict:
    from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService
    res = await BrandProfileService.get(user_id, db, brand_id=brand_id)
    return (res.get("responseData") or {}) if isinstance(res, dict) and res.get("status") else {}


def angle_for(index: int, count: int) -> str:
    """One ad: no angle, so the owner's style choice leads. Several: each a different way in,
    in the same order the plan tests them."""
    from . import concepts as concepts_mod
    if count <= 1:
        return ""
    return concepts_mod.ANGLES[index % len(concepts_mod.ANGLES)][0]


async def _make_one(db, doc: dict, angle: str, *, avoid: str = ""):
    """One ad, made exactly the way the plan makes one (generate_ad_creative, the plan's angles
    and layouts, the brand's destination), and charged the same way once it exists."""
    from app.services.CreditService import credit_service
    from app.utils.image_usage import counting as counting_images

    from . import ad_image_billing
    from . import concepts as concepts_mod
    from .creative import generate_ad_creative
    from .creative_paths import without_budget
    from .destination import get_brand_destination

    inputs, user_id, brand_id = doc["inputs"], doc["user_id"], doc["brand_id"]
    profile = await _profile(db, user_id, brand_id)
    saved = await get_brand_destination(db, brand_id)
    destination_type = inputs.get("destination_type") or saved["destination_type"]
    brief = without_budget(inputs["description"], inputs.get("budget_ngn") or None)
    if avoid:
        brief += f" Make it clearly different from this earlier attempt: {avoid}"
    wallet = await credit_service.get_user_wallet(user_id)
    unaffordable = ad_image_billing.styles_costing_more_than(wallet.credits_remaining if wallet else 0)
    with counting_images() as image_calls:
        ad = await generate_ad_creative(
            profile.get("brand_name") or "Your business", profile.get("industry") or "", inputs["goal"],
            concepts_mod.brief_for(brief, angle) if angle else brief,
            image_layout=concepts_mod.layout_for(angle) if angle else "",
            user_id=user_id, db=db, brand_id=brand_id, city=inputs.get("city") or "",
            service_area=inputs.get("city") or "", budget_ngn=float(inputs.get("budget_ngn") or 0),
            destination_type=destination_type, destination_cta=saved["destination_cta"],
            vsg01_format_id=None if angle else (inputs.get("vsg01_format_id") or None),
            platform=inputs.get("platform") or "meta", exclude_format_ids=unaffordable or None)
    if not ad.image_url:
        return None
    ad.concept = angle
    charged = ad_image_billing.credits_to_charge(ad.vsg01_format_id)
    return ad, charged, image_calls.n


async def _keep(db, doc: dict, ad, charged: int, image_calls: int, *, index: Optional[int] = None) -> dict:
    """Keep the ad (in the preview, and in Creatives), then charge for it: a failure before this
    never costs a credit."""
    from app.services.CreditService import credit_service

    from . import ad_image_billing
    result = ad.model_dump(mode="json")
    old = (doc.get("results") or [])[index] if index is not None else {}
    draft_id = old.get("draft_id") or str(uuid.uuid4())
    draft = {"content": ad.primary_text, "headline": ad.headline, "image_url": ad.image_url}
    if old.get("draft_id"):
        await db["content_drafts"].update_one({"id": draft_id}, {"$set": draft})
    else:
        await db["content_drafts"].insert_one({
            "id": draft_id, "request_id": draft_id, "user_id": doc["user_id"], "brand_id": doc["brand_id"],
            "platform": "instagram", **draft, "has_image": True, "status": "draft",
            "content_source": DRAFT_SOURCE, "preview_id": doc["preview_id"], "created_at": _now()})
    result["draft_id"] = draft_id
    if index is None:
        await db[PREVIEWS].update_one({"preview_id": doc["preview_id"]}, {"$push": {"results": result}})
    else:
        await db[PREVIEWS].update_one({"preview_id": doc["preview_id"]}, {"$set": {f"results.{index}": result}})
    await credit_service.deduct_credit(doc["user_id"], campaign_id=doc["preview_id"], reason="campaign_generation",
                                       amount=charged)
    await ad_image_billing.record(db, user_id=doc["user_id"], brand_id=doc["brand_id"],
                                  campaign_id=doc["preview_id"], format_id=ad.vsg01_format_id,
                                  image_calls=image_calls, charged=charged)
    return result


async def preview_job(db, job: dict, ctx) -> dict:
    pid = job["payload"]["preview_id"]
    doc = await db[PREVIEWS].find_one({"preview_id": pid})
    if not doc:
        raise JobError("Those ads were deleted.", code="gone")
    last_attempt = int(job.get("attempts", 1)) >= int(job.get("max_attempts", 1))

    async def give_up(change: Optional[dict] = None) -> None:
        # Until the last attempt it stays "running": a retry follows.
        if last_attempt:
            await db[PREVIEWS].update_one({"preview_id": pid}, {"$set": {"status": "failed", "error": _UNAVAILABLE,
                                                                         **(change or {})}})

    redo = job["payload"].get("redo_index")
    if redo is not None:
        index = int(redo)
        results = doc.get("results") or []
        if not 0 <= index < len(results):
            await db[PREVIEWS].update_one({"preview_id": pid}, {"$set": {"status": "done"}})
            return {"preview_id": pid}
        old = results[index]
        await ctx.step(f"redo{index}", f"Making ad {index + 1} again")
        try:
            made = await _make_one(db, doc, old.get("concept") or "", avoid=old.get("headline") or "")
        except Exception as e:
            print(f"[ad preview] {pid} redo {index + 1} failed: {e!r}", flush=True)
            made = None
        if not made:
            await give_up({f"results.{index}.redoing": False})
            raise JobError(_UNAVAILABLE, code="ai_unavailable", retry=True)
        await _keep(db, doc, *made, index=index)
        await db[PREVIEWS].update_one({"preview_id": pid}, {"$set": {"status": "done", "error": ""}})
        await ctx.step(f"redo{index}", f"Making ad {index + 1} again", "done", made[0].headline)
        return {"preview_id": pid}

    done = len(doc.get("results") or [])  # a retry carries on where the last attempt stopped
    await db[PREVIEWS].update_one({"preview_id": pid}, {"$set": {"status": "running", "error": ""}})
    for i in range(done, doc["count"]):
        await ctx.step(f"ad{i}", f"Making ad {i + 1} of {doc['count']}")
        try:
            made = await _make_one(db, doc, angle_for(i, doc["count"]))
        except Exception as e:
            print(f"[ad preview] {pid} ad {i + 1} failed: {e!r}", flush=True)
            made = None
        if not made:
            await give_up()
            raise JobError(_UNAVAILABLE, code="ai_unavailable", retry=True)
        await _keep(db, doc, *made)
        await ctx.step(f"ad{i}", f"Making ad {i + 1} of {doc['count']}", "done", made[0].headline)
    await db[PREVIEWS].update_one({"preview_id": pid}, {"$set": {"status": "done"}})
    return {"preview_id": pid}


# ── the owner's changes ────────────────────────────────────────────────────────

async def _ad(db, brand_id: str, preview_id: str, index: int) -> tuple[dict, dict]:
    doc = await db[PREVIEWS].find_one({"brand_id": brand_id, "preview_id": preview_id}, {"_id": 0})
    if not doc:
        raise LookupError("Those ads weren't found.")
    results = doc.get("results") or []
    if not 0 <= index < len(results):
        raise LookupError("That ad wasn't found.")
    return doc, results[index]


async def edit(db, *, brand_id: str, preview_id: str, index: int, headline: str, primary_text: str) -> dict:
    """The owner's own words. The same checks as any ad's words: a headline of up to 40
    characters, and nothing the ad-policy check would block."""
    from .models import Platform
    from .policy import Severity, review_ad_creative
    headline, primary_text = " ".join((headline or "").split()), (primary_text or "").strip()
    if not headline or not primary_text:
        raise ValueError("Add a headline and the ad's text.")
    if len(headline) > 40:
        raise ValueError("Keep the headline to 40 characters.")
    blocking = [v for v in review_ad_creative(headline, primary_text, platform=Platform.META.value).violations
                if v.severity == Severity.BLOCK]
    if blocking:
        raise ValueError(f"Those words can't run as an ad: {blocking[0].guidance}")
    doc, ad = await _ad(db, brand_id, preview_id, index)
    await db[PREVIEWS].update_one({"preview_id": preview_id}, {"$set": {
        f"results.{index}.headline": headline, f"results.{index}.primary_text": primary_text}})
    if ad.get("draft_id"):
        await db["content_drafts"].update_one({"id": ad["draft_id"]},
                                              {"$set": {"headline": headline, "content": primary_text}})
    return _public_ad({**ad, "headline": headline, "primary_text": primary_text})


async def remove(db, *, brand_id: str, preview_id: str, index: int) -> dict:
    """Not wanted: off the list, and out of Creatives. The last one can't go (the campaign needs an ad)."""
    doc, ad = await _ad(db, brand_id, preview_id, index)
    if doc["status"] == "running":
        raise ValueError("Jane is still making these. Try again when she's done.")
    if len(doc["results"]) <= 1:
        raise ValueError("The campaign needs at least one ad. Make it again instead.")
    results = [r for i, r in enumerate(doc["results"]) if i != index]
    await db[PREVIEWS].update_one({"preview_id": preview_id}, {"$set": {"results": results,
                                                                        "count": len(results)}})
    if ad.get("draft_id"):
        await db["content_drafts"].delete_one({"id": ad["draft_id"], "content_source": DRAFT_SOURCE})
    return public({**doc, "results": results, "count": len(results)})


async def redo(db, *, brand_id: str, user_id: str, preview_id: str, index: int) -> dict:
    """Make one ad again, in its place, at the same price as making it was. Checked first."""
    from app.services.CreditService import credit_service

    from . import ad_image_billing
    doc, ad = await _ad(db, brand_id, preview_id, index)
    if doc["status"] == "running":
        raise ValueError("Jane is still making these. Try again when she's done.")
    needed = 1 if ad.get("concept") else ad_image_billing.credits_needed(doc["inputs"].get("vsg01_format_id") or None)
    if not await credit_service.check_sufficient_credits(user_id, required=needed):
        raise PermissionError(f"Making it again takes {needed} content credit{'s' if needed != 1 else ''}. "
                              "Top up first.")
    await db[PREVIEWS].update_one({"preview_id": preview_id}, {"$set": {
        "status": "running", "error": "", f"results.{index}.redoing": True}})
    job = await queue.enqueue(db, JOB, {"preview_id": preview_id, "redo_index": index}, brand_id=brand_id,
                              user_id=user_id, key=f"{preview_id}:redo:{index}:{uuid.uuid4().hex[:6]}",
                              max_attempts=2)
    await db[PREVIEWS].update_one({"preview_id": preview_id}, {"$set": {"job_id": job["job_id"]}})
    return await get(db, brand_id, preview_id)

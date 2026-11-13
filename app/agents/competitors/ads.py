"""
Competitor ads (PRD Workstream 3) and "Make one like this" (Workstream 4).

Their ads: ads the owner has seen and saved (a screenshot or saved image, with an optional link
and competitor). No ad data source is assumed: Meta's official Ad Library API does not return
ordinary commercial ads for Nigeria, and the source for automatic collection is a business
decision still to be made. Every saved ad is read by a vision model for its angle, hook, offer,
call to action, layout and why it works, in plain words.

Make one like this: Jane's own ad maker (jane_ads.creative.generate_ad_creative) writes and
designs new ads for THIS business from the reference's IDEA — its angle, hook and layout — never
its words, logo, people or artwork. Results arrive one by one; each costs one content credit,
the same as any ad Jane makes.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import openai

from app.core.config import settings
from app.jobs import queue
from app.jobs.worker import JobError

ADS = "competitor_ads"
RECREATIONS = "ad_recreations"
ANALYSIS_JOB = "competitor_ad_analysis"
RECREATE_JOB = "ad_recreate"
MODELS = ("gpt-5.5", "gpt-5.4", "gpt-4o")
MAX_VARIATIONS = 3
ANGLE_LABELS = {
    "problem": "Problem / solution",
    "offer": "Offer",
    "outcome": "Result",
    "versus": "Before / after",
    "announcement": "News",
    "post": "Opinion",
    "proof": "Proof",
    "other": "Other",
}
# The reference's angle → the layout Jane's ad maker already knows how to draw.
_LAYOUT_ANGLE = {"problem": "problem", "offer": "offer", "outcome": "outcome", "versus": "versus",
                 "announcement": "announcement", "post": "post", "proof": "outcome", "other": "outcome"}


async def ensure_indexes(db) -> None:
    await db[ADS].create_index([("brand_id", 1), ("created_at", -1)], name="brand_recent")
    await db[ADS].create_index([("brand_id", 1), ("competitor_id", 1)], name="brand_competitor")
    await db[ADS].create_index("ad_id", unique=True, name="ad_id_unique")
    await db[RECREATIONS].create_index("recreation_id", unique=True, name="recreation_id_unique")
    await db[RECREATIONS].create_index([("brand_id", 1), ("created_at", -1)], name="brand_recent")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def public_ad(doc: dict) -> dict:
    a = doc.get("analysis") or {}
    return {"id": doc["ad_id"], "image_url": doc["image_url"], "competitor_id": doc.get("competitor_id", ""),
            "competitor_name": doc.get("competitor_name", ""), "platform": doc.get("platform", ""),
            "link": doc.get("link", ""), "note": doc.get("note", ""), "status": doc["status"],
            "analysis": a, "angle_label": ANGLE_LABELS.get(a.get("angle", ""), ""),
            "error": doc.get("error", ""), "created_at": doc.get("created_at")}


# ── saving and reading their ads ────────────────────────────────────────────────

async def save_ad(db, *, brand_id: str, user_id: str, image_url: str, competitor_id: str = "",
                  platform: str = "", link: str = "", note: str = "") -> dict:
    if not image_url.startswith("https://"):
        raise ValueError("Upload the ad's picture first.")
    competitor_name = ""
    if competitor_id:
        c = await db["brand_competitors"].find_one({"brand_id": brand_id, "competitor_id": competitor_id})
        if not c:
            raise ValueError("That competitor wasn't found.")
        competitor_name = c["name"]
    doc = {"ad_id": f"cad_{uuid.uuid4().hex[:16]}", "brand_id": brand_id, "user_id": user_id,
           "image_url": image_url, "competitor_id": competitor_id, "competitor_name": competitor_name,
           "platform": (platform or "").lower()[:20], "link": link.strip()[:500], "note": note.strip()[:300],
           "status": "analyzing", "created_at": _now()}
    await db[ADS].insert_one(dict(doc))
    await queue.enqueue(db, ANALYSIS_JOB, {"ad_id": doc["ad_id"]}, brand_id=brand_id, user_id=user_id,
                        key=doc["ad_id"], max_attempts=3)
    return public_ad(doc)


async def list_ads(db, brand_id: str, *, competitor_id: str = "", angle: str = "", platform: str = "",
                   before: Optional[datetime] = None, limit: int = 24) -> dict:
    q: dict[str, Any] = {"brand_id": brand_id}
    if competitor_id:
        q["competitor_id"] = competitor_id
    if angle:
        q["analysis.angle"] = angle
    if platform:
        q["platform"] = platform
    if before:
        q["created_at"] = {"$lt": before}
    limit = max(1, min(60, limit))
    docs = await db[ADS].find(q, {"_id": 0}).sort("created_at", -1).limit(limit + 1).to_list(length=limit + 1)
    more = len(docs) > limit
    docs = docs[:limit]
    return {"ads": [public_ad(d) for d in docs],
            "next_before": docs[-1]["created_at"].isoformat() if more and docs else None,
            "angles": [{"value": k, "label": v} for k, v in ANGLE_LABELS.items()]}


async def delete_ad(db, brand_id: str, ad_id: str) -> bool:
    res = await db[ADS].delete_one({"brand_id": brand_id, "ad_id": ad_id})
    return res.deleted_count == 1


# ── reading an ad ──────────────────────────────────────────────────────────────

_ANALYSIS_PROMPT = (
    "This is an advertisement a small business saw from a competitor. Describe it for that "
    "business, in plain words, from what you can see only.\n"
    'Return ONLY JSON: {"headline": "the main words on the ad, exactly, or empty", '
    '"angle": one of "problem","offer","outcome","versus","announcement","post","proof","other", '
    '"hook": "what grabs attention first, in a few words", '
    '"offer": "the offer or price shown, exactly, or empty", '
    '"cta": "what it asks people to do, or empty", '
    '"customer_problem": "the problem it speaks to, or empty", '
    '"format": "photo" | "graphic" | "video" | "carousel" | "text", '
    '"layout": "how it is laid out, one sentence (e.g. photo of a driver, bold headline in a band at the bottom)", '
    '"why_it_works": "2-3 short sentences a business owner would understand"}'
)


async def _vision(image_url: str) -> dict:
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not configured")
    client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    last: Optional[Exception] = None
    for model in MODELS:
        try:
            resp = await client.chat.completions.create(
                model=model, response_format={"type": "json_object"}, timeout=60,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": _ANALYSIS_PROMPT},
                    {"type": "image_url", "image_url": {"url": image_url}}]}])
            return json.loads(resp.choices[0].message.content or "{}")
        except (openai.NotFoundError, openai.PermissionDeniedError) as e:
            last = e
            continue
        except openai.BadRequestError as e:
            if "model" in str(e).lower():
                last = e
                continue
            raise
    raise RuntimeError(str(last))


def clean_analysis(raw: dict) -> dict:
    def s(k, n=240):
        return " ".join(str(raw.get(k) or "").split())[:n]
    angle = raw.get("angle") if raw.get("angle") in ANGLE_LABELS else "other"
    fmt = raw.get("format") if raw.get("format") in ("photo", "graphic", "video", "carousel", "text") else "graphic"
    return {"headline": s("headline", 160), "angle": angle, "hook": s("hook", 120), "offer": s("offer", 120),
            "cta": s("cta", 80), "customer_problem": s("customer_problem", 160), "format": fmt,
            "layout": s("layout", 200), "why_it_works": s("why_it_works", 420)}


async def analysis_job(db, job: dict, ctx) -> dict:
    ad_id = job["payload"]["ad_id"]
    ad = await db[ADS].find_one({"ad_id": ad_id})
    if not ad:
        raise JobError("That ad was deleted.", code="gone")
    await ctx.step("read", "Reading the ad")
    try:
        analysis = clean_analysis(await _vision(ad["image_url"]))
    except openai.BadRequestError:
        await db[ADS].update_one({"ad_id": ad_id}, {"$set": {"status": "failed",
                                                             "error": "Jane couldn't read that picture. Try a clearer screenshot."}})
        raise JobError("Jane couldn't read that picture. Try a clearer screenshot.", code="unreadable")
    except Exception as e:
        print(f"[competitor ads] analysis failed for {ad_id}: {e!r}", flush=True)
        if job.get("attempts", 1) >= job.get("max_attempts", 1):
            await db[ADS].update_one({"ad_id": ad_id}, {"$set": {"status": "failed",
                                                                 "error": "Jane couldn't read this ad just now."}})
        raise JobError("Jane couldn't read this ad just now. She'll try again shortly.", code="ai_unavailable",
                       retry=True)
    await db[ADS].update_one({"ad_id": ad_id}, {"$set": {"status": "ready", "analysis": analysis, "error": ""}})
    await ctx.step("read", "Reading the ad", "done", ANGLE_LABELS[analysis["angle"]])
    return {"ad_id": ad_id}


# ── make one like this ─────────────────────────────────────────────────────────

def public_recreation(doc: dict) -> dict:
    return {"id": doc["recreation_id"], "ad_id": doc["ad_id"], "product": doc.get("product", ""),
            "count": doc["count"], "status": doc["status"], "results": doc.get("results", []),
            "error": doc.get("error", ""), "reference": doc.get("reference", {})}


async def start_recreate(db, *, brand_id: str, user_id: str, ad_id: str, product: str = "", count: int = 3) -> dict:
    from app.services.CreditService import credit_service
    ad = await db[ADS].find_one({"brand_id": brand_id, "ad_id": ad_id}, {"_id": 0})
    if not ad:
        raise LookupError("That ad wasn't found.")
    if ad["status"] != "ready":
        raise ValueError("Jane is still reading that ad. Try again in a moment.")
    count = max(1, min(MAX_VARIATIONS, int(count or 1)))
    if not await credit_service.check_sufficient_credits(user_id, required=count):
        raise PermissionError(f"Making {count} ad{'s' if count != 1 else ''} takes {count} content credit"
                              f"{'s' if count != 1 else ''}. Top up, or make fewer.")
    doc = {"recreation_id": f"rec_{uuid.uuid4().hex[:16]}", "brand_id": brand_id, "user_id": user_id,
           "ad_id": ad_id, "product": product.strip()[:160], "count": count, "status": "running", "results": [],
           "reference": {"image_url": ad["image_url"], "competitor_name": ad.get("competitor_name", ""),
                         "analysis": ad.get("analysis", {})},
           "created_at": _now()}
    await db[RECREATIONS].insert_one(dict(doc))
    await queue.enqueue(db, RECREATE_JOB, {"recreation_id": doc["recreation_id"]}, brand_id=brand_id,
                        user_id=user_id, key=doc["recreation_id"], max_attempts=2)
    return public_recreation(doc)


def recreation_brief(analysis: dict, product: str, description: str) -> str:
    """What Jane's ad maker is told: this business's product, in the reference's idea. The
    reference's own words, offer and branding are deliberately left out."""
    bits = [f"Advertise: {product or description or 'the business’s main product'}."]
    if analysis.get("hook"):
        bits.append(f"Open the way the reference ad does: {analysis['hook']}.")
    if analysis.get("customer_problem"):
        bits.append(f"Speak to this customer problem if it fits this business: {analysis['customer_problem']}.")
    if analysis.get("layout"):
        bits.append(f"Use a similar composition: {analysis['layout']}.")
    bits.append("Write everything new for this business. Never reuse the reference's words, name, logo, "
                "people or prices; only use an offer or price this business actually has.")
    return " ".join(bits)


async def recreate_job(db, job: dict, ctx) -> dict:
    from app.agents.jane_ads import concepts as concepts_mod
    from app.agents.jane_ads.creative import generate_ad_creative
    from app.services.CreditService import credit_service

    rid = job["payload"]["recreation_id"]
    rec = await db[RECREATIONS].find_one({"recreation_id": rid})
    if not rec:
        raise JobError("That request was deleted.", code="gone")
    profile = await _profile(db, rec["user_id"], rec["brand_id"])
    analysis = (rec.get("reference") or {}).get("analysis") or {}
    angle = _LAYOUT_ANGLE.get(analysis.get("angle", ""), "outcome")
    angles = [angle] + [a for a, _, _ in concepts_mod.ANGLES if a != angle]
    brief = recreation_brief(analysis, rec.get("product", ""), profile.get("product_description", ""))
    done = len(rec.get("results") or [])  # a retry carries on where the last attempt stopped
    await db[RECREATIONS].update_one({"recreation_id": rid}, {"$set": {"status": "running", "error": ""}})
    for i in range(done, rec["count"]):
        a = angles[i % len(angles)]
        await ctx.step(f"ad{i}", f"Making version {i + 1} of {rec['count']}")
        ad = await generate_ad_creative(
            profile.get("brand_name") or "Your business", profile.get("industry") or "", "traffic",
            concepts_mod.brief_for(brief, a), user_id=rec["user_id"], db=db, brand_id=rec["brand_id"],
            city=profile.get("customer_location") or "", image_layout=concepts_mod.layout_for(a))
        if not ad.image_url:
            await db[RECREATIONS].update_one({"recreation_id": rid}, {"$set": {"status": "failed",
                                                                               "error": "Jane couldn't make that ad just now. Try again shortly."}})
            raise JobError("Jane couldn't make that ad just now. Try again shortly.", code="ai_unavailable", retry=True)
        await credit_service.deduct_credit(rec["user_id"], campaign_id=rid, reason="campaign_generation")
        result = {"image_url": ad.image_url, "headline": ad.headline, "primary_text": ad.primary_text, "angle": a}
        await db[RECREATIONS].update_one({"recreation_id": rid}, {"$push": {"results": result}})
        await ctx.step(f"ad{i}", f"Making version {i + 1} of {rec['count']}", "done", ad.headline)
    await db[RECREATIONS].update_one({"recreation_id": rid}, {"$set": {"status": "done"}})
    return {"recreation_id": rid}


async def get_recreation(db, brand_id: str, recreation_id: str) -> Optional[dict]:
    doc = await db[RECREATIONS].find_one({"brand_id": brand_id, "recreation_id": recreation_id}, {"_id": 0})
    return public_recreation(doc) if doc else None


async def _profile(db, user_id: str, brand_id: str) -> dict:
    from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService
    res = await BrandProfileService.get(user_id, db, brand_id=brand_id)
    return (res.get("responseData") or {}) if isinstance(res, dict) and res.get("status") else {}


async def use_version(db, *, brand_id: str, user_id: str, recreation_id: str, index: int) -> dict:
    """Keep one version as a content draft (it then shows in Creatives) and hand back the draft the
    campaign builder already knows how to start from. Asking twice for the same version returns
    the same draft."""
    rec = await db[RECREATIONS].find_one({"brand_id": brand_id, "recreation_id": recreation_id}, {"_id": 0})
    if not rec:
        raise LookupError("That request wasn't found.")
    results = rec.get("results") or []
    if not 0 <= index < len(results):
        raise LookupError("That version wasn't found.")
    chosen = results[index]
    if chosen.get("draft_id"):
        draft_id = chosen["draft_id"]
    else:
        draft_id = str(uuid.uuid4())
        await db["content_drafts"].insert_one({
            "id": draft_id, "user_id": user_id, "brand_id": brand_id, "platform": "instagram",
            "content": chosen.get("primary_text", ""), "headline": chosen.get("headline", ""),
            "image_url": chosen["image_url"], "has_image": True, "status": "draft",
            "content_source": "ad_recreate", "recreation_id": recreation_id, "created_at": _now(),
        })
        await db[RECREATIONS].update_one({"recreation_id": recreation_id},
                                         {"$set": {f"results.{index}.draft_id": draft_id}})
    return {"draft_id": draft_id, "platform": "instagram", "content": (chosen.get("primary_text") or "")[:200],
            "image_url": chosen["image_url"], "created_at": _now().isoformat()}

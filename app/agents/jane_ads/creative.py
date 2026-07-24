"""
Jane + Ads — ad creative generation (split-doc 1.6).

Three sources, matching how normal content creation already works on the platform
(PRD Part D2) and how the competitive tools (AdCreative.ai, Canva, Meta Advantage+)
converge: brand-kit-anchored generation, your own uploaded media, or reusing
something you already made:

  1. GENERATE (default) — pulls the SAME brand playbook normal posts use (colours,
     voice, industry, region, audience from BrandProfileService) and weaves it into
     the ad image prompt, so ads look like the brand instead of a generic template.
  2. UPLOAD — the user's own photo/video (already uploaded via the existing
     /upload-user-content flow) becomes the creative directly.
  3. DRAFT — an existing content draft the user already generated and liked.

DELIBERATE DEVIATION from a first attempt: GENERATE does NOT delegate straight to
`ImageContentService._generate_platform_image` (the function normal organic posts
use). That function's own internal brief-writing step decides POSTER-vs-PHOTO and a
TEXT_LEVEL itself, tuned for organic content where a bold on-image headline is often
desirable — and it could not be reliably steered away from baking a full headline/
subtext/"link in bio" poster into the ad image, even with an explicit "no text"
instruction (verified: it still rendered one). A paid ad must NOT bake its message
into the image — the headline/primary_text/CTA are separate fields the ad platform
lays over it. So GENERATE reuses the brand DATA (the playbook) via a direct,
controlled image call instead of the organic-content TEMPLATE pipeline.

Whatever the source, Jane always writes fresh ad copy and auto-attaches the WhatsApp
CTA — the customer never sets that (every ad routes to WhatsApp).

Copy-writing + image generation are live (LLM); `assemble_creative`, `_as_ad_content`,
and the draft-summary mapping are pure and unit-tested.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Optional

import aiohttp
import openai

from app.core.config import settings

from .models import AdCopy, AdCreative, CreativeSource, ShootScript, ShootShot

WHATSAPP_CTA = "Send WhatsApp Message"

# Ads are always vertical (CTWA/Stories placements) regardless of which ad platform
# the campaign ultimately runs on — "instagram"+"story" is the engine's 9:16 spec.
_AD_IMAGE_PLATFORM = "instagram"
_AD_IMAGE_TYPE = "story"


# ── Brand context (reuse the same playbook normal posts use) ──────────────────

async def get_brand_context(user_id: str, db, brand_id: Optional[str] = None) -> dict:
    """Fetch and convert the brand profile the same way normal content generation
    does, so ads inherit brand voice, colours, fonts, and audience — not a generic
    style. Returns {} if there's no profile (falls back to generic generation)."""
    if not user_id or db is None:
        return {}
    from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService
    try:
        resp = await BrandProfileService.get(user_id, db, brand_id)
        profile = resp.get("responseData") if isinstance(resp, dict) else None
        return BrandProfileService.to_brand_context(profile)
    except Exception as e:
        print(f"[Creative] brand context error: {e}", flush=True)
        return {}


# ── Copy (LLM writes it, voice-matched to the brand when available) ──────────

def _brand_prompt_bits(bc: dict) -> str:
    """Turn the brand playbook into short prompt fragments — shared by the copy and
    image prompts so both draw on the SAME brand data."""
    bits = []
    if bc.get("brand_voice"):
        bits.append(f"Brand voice: {bc['brand_voice']}.")
    if bc.get("target_audience"):
        bits.append(f"Audience: {bc['target_audience']}.")
    if bc.get("industry"):
        bits.append(f"Industry: {bc['industry']}.")
    if bc.get("region"):
        bits.append(f"Market: {bc['region']}.")
    if bc.get("brand_colors"):
        bits.append(f"Brand colours: {', '.join(bc['brand_colors'][:3])}.")
    return " ".join(bits)


def _brand_color_directive(bc: dict) -> str:
    """A forceful (not a passing mention) instruction that the scene's props/wardrobe/
    decor actually carry the brand's colours — matches how normal content generation
    treats brand colours as a hard requirement, not a suggestion."""
    colors = bc.get("brand_colors") or []
    if not colors:
        return ""
    return (
        f" The brand's colours ({', '.join(colors[:3])}) MUST be visibly prominent in "
        "the scene — reflected in clothing, props, decor, packaging, or lighting accents, "
        "not just present in the background."
    )


def _logo_reserve_directive(bc: dict) -> str:
    """Leave a real, unobstructed corner for the brand logo to be composited onto
    the finished image afterward (see generate_ad_image) — mirrors the logo-zone
    reservation normal content generation does before its own overlay step."""
    if not bc.get("logo_url"):
        return ""
    position = (bc.get("logo_position") or "bottom_right").replace("_", " ")
    return (
        f" Leave the {position} corner visually clear (no faces, text, or busy detail "
        "there) — a brand logo will be composited onto that corner afterward."
    )


def _location_prompt_bit(city: str, category: str = "") -> str:
    """Ground the ad's visual setting in the real place it targets — without this,
    the image model defaults to a generic global stock-photo look. Two failure modes
    to guard against, both seen in testing:
      1. Generic Western/European café aesthetic (fixed by naming the real city).
      2. Overcorrecting into a rundown/rural stereotype for ANY Nigerian location —
         a developed commercial area (e.g. Ikeja's malls, GRA, business district)
         must not default to a run-down market street. The setting's quality must
         also match the business itself (a fine-dining restaurant looks upscale
         wherever it is), not just the city name."""
    biz_tier = (f"The setting's quality must also match the business itself — a "
                f"{category} should look how a real {category} of that caliber "
                f"actually looks, in that specific area. " if category else "")
    if city:
        return (
            f"Location: {city}, Nigeria. Use your real knowledge of what {city} "
            f"specifically looks like — its actual buildings, streets, and level of "
            f"development (whether that's a modern business district, a mall, an "
            f"upscale estate, or a busy commercial street) — real Nigerian people "
            f"(skin tones, hairstyles, everyday clothing). {biz_tier}"
            "Do NOT default to a generic rundown or rural stereotype just because "
            "it's Nigeria, and do NOT default to a generic Western/European look either "
            f"— depict {city} as it actually, specifically is."
        )
    return (
        f"Location: Nigeria — a real, contemporary Nigerian setting with real Nigerian "
        f"people. {biz_tier}Not a generic Western/European look, and not a generic "
        "rundown/rural stereotype either — depict a real, specific, credible place."
    )


async def write_ad_copy(business_name: str, category: str, goal: str = "messages",
                        description: str = "", brand_context: Optional[dict] = None,
                        city: str = "", behaviour: str = "") -> AdCopy:
    """Write a short headline, primary text, and an image prompt (used only for the
    GENERATE source). Voice-matched to the brand playbook when a profile exists, and
    visually grounded in the real city/area the campaign targets. Also does the
    creative-type reasoning (PRD §4.1): flags when a video would clearly serve this
    ad better than the photo we're about to generate — gpt-image-1 can't produce
    one, so this is a heads-up for the caller to offer an upload, not a capability."""
    if not settings.jane_ads_openai_key:
        return AdCopy()
    bc = brand_context or {}
    brand_bits = _brand_prompt_bits(bc)
    location_bit = _location_prompt_bit(city or bc.get("region", ""), category)
    prompt = (
        f"Write a Meta/Instagram ad for '{business_name or bc.get('brand_name') or 'a business'}' "
        f"(a {category or 'local business'}) whose goal is {goal}. The ad drives people "
        f"to message the business on WhatsApp.{(' ' + brand_bits) if brand_bits else ''}\n"
        f"{location_bit}\n"
        f"Context: {description or 'none'}\n"
        f"How customers find this business: {behaviour or 'unknown'}.\n"
        "Return JSON with:\n"
        "- headline: punchy, <= 5 words, no ALL CAPS, no emoji spam\n"
        "- primary_text: 1–2 warm, concrete sentences in the brand voice above if given "
        "(no hype, no clickbait)\n"
        "- image_prompt: a photorealistic scene for the creative image — real people/"
        "products/workspace fitting the business, in the brand's colours/setting and the "
        "LOCATION above. NO text anywhere in the image — no logos, no watermarks, no "
        "storefront signage/shop signs with the business name, no readable words of any "
        "kind (Meta adds the ad's headline and button separately; baked-in text is never "
        "wanted here, including on buildings/signs in the background). Not an "
        "illustration.\n"
        "- video_recommended: boolean. Set true whenever the business is a movement/"
        "performance/demonstration activity (dance, fitness classes, sports, cooking-"
        "in-action, live music) or depends on personal trust in the founder (coaching, "
        "therapy, consulting, medical/beauty procedures) where seeing the person move "
        "or speak on camera would clearly beat a static photo. Set false for businesses "
        "that are visually static (a shop, a static product) or for a 'search' "
        "behaviour, where the offer/price matters more than the visual. Examples: a "
        "Zumba instructor -> true. A life coach doing consultations -> true. A grocery "
        "store -> false. A phone repair shop -> false.\n"
        "- video_recommendation_reason: one short sentence why, ONLY if "
        "video_recommended is true; else empty string.\n"
        "Return ONLY the JSON."
    )
    try:
        client = openai.AsyncOpenAI(api_key=settings.jane_ads_openai_key)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            timeout=15,
        )
        d = json.loads(resp.choices[0].message.content or "{}")
        return AdCopy(
            headline=str(d.get("headline", "")).strip(),
            primary_text=str(d.get("primary_text", "")).strip(),
            image_prompt=str(d.get("image_prompt", "")).strip(),
            video_recommended=bool(d.get("video_recommended")),
            video_recommendation_reason=str(d.get("video_recommendation_reason", "")).strip(),
        )
    except Exception as e:
        print(f"[Creative] copy error: {e}", flush=True)
        return AdCopy()


async def write_shoot_script(business_name: str, category: str, goal: str,
                             video_recommendation_reason: str, description: str = "",
                             brand_context: Optional[dict] = None) -> ShootScript:
    """Path C (PRD §4.1): when a video was recommended over the (photo-only)
    AI generator, write a short, phone-filmable shoot script instead — no crew or
    equipment assumed. The business owner films it themselves; the footage is
    uploaded via the existing upload path and swapped into the pending plan via
    POST /meta/plan/{plan_id}/creative. Never raises — an empty script just means
    the user films freely instead."""
    if not settings.jane_ads_openai_key:
        return ShootScript()
    bc = brand_context or {}
    brand_bits = _brand_prompt_bits(bc)
    prompt = (
        f"Write a short phone-filmable shoot script for a Meta/Instagram ad for "
        f"'{business_name or bc.get('brand_name') or 'a business'}' (a "
        f"{category or 'local business'}) whose goal is {goal}. Why video: "
        f"{video_recommendation_reason}.{(' ' + brand_bits) if brand_bits else ''}\n"
        f"Context: {description or 'none'}\n"
        "The business owner will film this themselves on a phone — no crew, no "
        "equipment, no editing skill assumed. Keep it to 3-5 short shots totalling "
        "15-30 seconds.\n"
        "Return JSON with:\n"
        "- hook_line: the first ~2 seconds — what's said or shown to stop someone "
        "scrolling\n"
        "- shots: an array of 3-5 objects, each with: direction (what to film / how "
        "to frame it, in plain language a non-filmmaker can follow), say (what to "
        "say on camera — empty string if it's a silent b-roll shot), seconds (a "
        "realistic rough duration, integer)\n"
        "- caption_idea: one short on-screen text/caption idea, or empty string if "
        "none needed\n"
        "Return ONLY the JSON."
    )
    try:
        client = openai.AsyncOpenAI(api_key=settings.jane_ads_openai_key)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            timeout=15,
        )
        d = json.loads(resp.choices[0].message.content or "{}")
        shots = [
            ShootShot(
                direction=str(s.get("direction", "")).strip(),
                say=str(s.get("say", "")).strip(),
                seconds=int(s.get("seconds") or 5),
            )
            for s in (d.get("shots") or []) if isinstance(s, dict)
        ]
        return ShootScript(
            hook_line=str(d.get("hook_line", "")).strip(),
            shots=shots,
            caption_idea=str(d.get("caption_idea", "")).strip(),
        )
    except Exception as e:
        print(f"[Creative] shoot script error: {e}", flush=True)
        return ShootScript()


# ── Source 1: GENERATE — brand data, direct controlled image call ────────────

async def _upload_bytes_to_cloudinary(
    file_bytes: bytes, public_id: str,
    resource_type: str = "image", ext: str = "png", content_type: str = "image/png",
) -> Optional[str]:
    """Signed REST upload straight from `settings` (not the `cloudinary` SDK, which
    reads raw os.environ and silently no-ops when credentials only live in .env via
    pydantic Settings). Matches the pattern already proven in this package's b-roll
    generation — self-contained, no extra package coupling. Works for both images
    (GENERATE's gpt-image-1 stills) and videos (a user's own uploaded ad video)."""
    cloud, api_key, api_secret = (settings.CLOUDINARY_CLOUD_NAME, settings.CLOUDINARY_API_KEY,
                                   settings.CLOUDINARY_API_SECRET)
    if not all([cloud, api_key, api_secret]):
        return None
    ts = int(time.time())
    sig = hashlib.sha1(f"folder=uri-ads&public_id={public_id}&timestamp={ts}{api_secret}".encode()).hexdigest()
    form = aiohttp.FormData()
    form.add_field("file", file_bytes, filename=f"{public_id}.{ext}", content_type=content_type)
    for k, v in (("api_key", api_key), ("timestamp", str(ts)), ("signature", sig),
                 ("public_id", public_id), ("folder", "uri-ads"), ("resource_type", resource_type)):
        form.add_field(k, v)
    # Video files are much larger than a generated still — allow more time to upload.
    timeout = 180 if resource_type == "video" else 60
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"https://api.cloudinary.com/v1_1/{cloud}/{resource_type}/upload",
                              data=form, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                body = json.loads(await r.text())
                return body.get("secure_url") if r.ok else None
    except Exception as e:
        print(f"[Creative] Cloudinary upload error: {e}", flush=True)
        return None


def _as_ad_content(image_prompt: str, brand_context: Optional[dict] = None) -> str:
    """Build the final image-generation prompt: the scene, woven with real brand data
    (colours/industry/region/voice from the SAME playbook normal posts use), with a
    hard, literal instruction against on-image text — never delegated to a pipeline
    that might override it with its own poster-vs-photo judgment."""
    bc = brand_context or {}
    brand_bits = _brand_prompt_bits(bc)
    return (
        f"{image_prompt.strip()}"
        f"{(' Reflect this brand: ' + brand_bits) if brand_bits else ''}"
        f"{_brand_color_directive(bc)}"
        f"{_logo_reserve_directive(bc)} "
        "Photorealistic, natural lighting, shallow depth of field, vertical 9:16. "
        "This is a paid ad photograph — NOT a poster, NOT a graphic design template. "
        "Absolutely no text, words, letters, headlines, captions, watermarks, logos, "
        "or UI rendered anywhere in the image — this includes storefront signs, shop "
        "name boards, street signs, or any readable writing on buildings/objects in "
        "the background. If depicting a storefront, show it WITHOUT a sign or with a "
        "blank/unreadable sign. The ad's headline and button are separate fields the "
        "ad platform overlays afterward. Real photo, not an illustration, cartoon, or "
        "3D render."
    )


async def generate_ad_image(image_prompt: str, brand_context: Optional[dict] = None) -> Optional[str]:
    """Generate the ad image with gpt-image-1, enriched with the brand's colours/
    voice/region pulled from the same playbook normal posts use — a direct, literal
    call so the "no on-image text" rule is never at the mercy of another pipeline's
    own creative judgment. When the brand has a logo, it's composited onto the
    finished image afterward — the SAME real pixel-logo overlay normal content
    generation uses (ImageContentService._overlay_logo), not an AI reinterpretation.
    Returns a Cloudinary URL, or None on failure."""
    if not settings.jane_ads_openai_key or not image_prompt.strip():
        return None
    bc = brand_context or {}
    full_prompt = _as_ad_content(image_prompt, bc)
    try:
        client = openai.AsyncOpenAI(api_key=settings.jane_ads_openai_key)
        resp = await client.images.generate(
            model="gpt-image-1", prompt=full_prompt, size="1024x1536", quality="high", n=1,
        )
        b64 = resp.data[0].b64_json if resp.data else None
        if not b64:
            return None

        logo_url = bc.get("logo_url")
        ext, content_type = "png", "image/png"
        if logo_url:
            from app.agents.social_media_manager.services.image_content_service import ImageContentService
            import asyncio
            b64 = await asyncio.to_thread(
                ImageContentService._overlay_logo, b64, logo_url,
                bc.get("logo_position", "bottom_right"), bc.get("logo_size", "small"),
            )
            ext, content_type = "webp", "image/webp"  # _overlay_logo always re-encodes as WEBP

        import base64
        img_bytes = base64.b64decode(b64)
        return await _upload_bytes_to_cloudinary(img_bytes, f"ad-{uuid.uuid4().hex[:12]}", ext=ext, content_type=content_type)
    except Exception as e:
        print(f"[Creative] image error: {e}", flush=True)
        return None


# ── Source 3: DRAFT — reuse an existing content draft ─────────────────────────

def _draft_to_summary(doc: dict) -> dict:
    """Pure projection of a content_drafts document → the fields the ad picker needs.
    Keeps the Mongo query thin and this mapping unit-testable."""
    return {
        "draft_id": doc.get("id") or doc.get("draft_id", ""),
        "platform": doc.get("platform", ""),
        "content": (doc.get("content") or "")[:200],
        "image_url": doc.get("image_url", ""),
        "created_at": str(doc.get("created_at", "")),
    }


async def list_recent_drafts(user_id: str, db, brand_id: Optional[str] = None, limit: int = 10) -> list[dict]:
    """List the user's recent drafts that have an image — so they can pick one they
    already liked instead of generating something new."""
    if not user_id or db is None:
        return []
    query: dict[str, Any] = {"user_id": user_id, "has_image": True, "image_url": {"$ne": ""}}
    if brand_id:
        query["brand_id"] = brand_id
    try:
        cursor = db["content_drafts"].find(
            query,
            {"_id": 0, "id": 1, "platform": 1, "content": 1, "image_url": 1, "created_at": 1},
        ).sort("created_at", -1).limit(limit)
        docs = await cursor.to_list(length=limit)
        return [_draft_to_summary(d) for d in docs]
    except Exception as e:
        print(f"[Creative] list drafts error: {e}", flush=True)
        return []


async def get_draft_image(draft_id: str, user_id: str, db) -> Optional[dict]:
    """Fetch one draft by id (scoped to the user) for the DRAFT source."""
    if not draft_id or db is None:
        return None
    doc = await db["content_drafts"].find_one(
        {"$or": [{"id": draft_id}, {"draft_id": draft_id}], "user_id": user_id},
        {"_id": 0, "id": 1, "platform": 1, "content": 1, "image_url": 1},
    )
    return _draft_to_summary(doc) if doc else None


# ── Assemble (pure) ───────────────────────────────────────────────────────────

_VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm", ".m4v")


def _looks_like_video(url: str) -> bool:
    """Best-effort detection from the URL extension — used when the caller doesn't
    already know the media type (e.g. a content draft, where it's rare but possible)."""
    return url.lower().split("?")[0].endswith(_VIDEO_EXTENSIONS)


def assemble_creative(copy: AdCopy, image_url: Optional[str],
                      source: CreativeSource = CreativeSource.GENERATE,
                      is_video: Optional[bool] = None) -> AdCreative:
    """Combine copy + media into a submittable creative. The WhatsApp CTA is always
    attached; `generated` is False when there's no media (copy-only fallback).
    `is_video` should be passed explicitly when the caller already knows the media
    type (e.g. from the upload's content-type); otherwise it's guessed from the URL.
    The video recommendation only ever applies to GENERATE — UPLOAD/DRAFT already
    have a real media choice made, so there's nothing to recommend."""
    url = image_url or ""
    return AdCreative(
        image_url=url,
        is_video=is_video if is_video is not None else _looks_like_video(url),
        headline=copy.headline,
        primary_text=copy.primary_text,
        cta=WHATSAPP_CTA,
        source=source,
        generated=bool(url),
        video_recommendation=copy.video_recommendation_reason if (
            source == CreativeSource.GENERATE and copy.video_recommended
        ) else "",
    )


# ── The three entry points ────────────────────────────────────────────────────

async def generate_ad_creative(
    business_name: str, category: str, goal: str = "messages", description: str = "",
    user_id: str = "", db=None, brand_id: Optional[str] = None, city: str = "",
    behaviour: str = "",
) -> AdCreative:
    """SOURCE 1 (default) — Jane writes the copy and generates the image herself,
    using the brand playbook's colours/voice/region/industry, grounded in `city` (the
    campaign's geo target, e.g. from the decision engine's geo pins) so the scene
    reflects the real place, not a generic global look. `behaviour` (search/discover/
    mixed, from the decision engine) informs the creative-type reasoning (PRD §4.1) —
    see write_ad_copy. For "use my own photo", see SOURCE 2 (creative_from_upload) —
    a distinct source, not a reference nudge on generation. Never raises — falls back
    to copy-only if image generation fails."""
    brand_context = await get_brand_context(user_id, db, brand_id) if user_id else {}
    copy = await write_ad_copy(business_name, category, goal, description, brand_context, city, behaviour)
    image_url = await generate_ad_image(copy.image_prompt, brand_context)
    return assemble_creative(copy, image_url, source=CreativeSource.GENERATE)


async def creative_from_upload(
    business_name: str, category: str, image_url: str, goal: str = "messages",
    description: str = "", user_id: str = "", db=None, brand_id: Optional[str] = None,
    is_video: Optional[bool] = None,
) -> AdCreative:
    """SOURCE 2 — the user's own uploaded photo OR video (uploaded via
    /jane-ads/creative/upload, or the existing /upload-user-content flow) becomes
    the creative directly; Jane still writes fresh copy to match it. No location
    grounding needed — the media IS the real place already."""
    brand_context = await get_brand_context(user_id, db, brand_id) if user_id else {}
    copy = await write_ad_copy(business_name, category, goal, description, brand_context)
    return assemble_creative(copy, image_url, source=CreativeSource.UPLOAD, is_video=is_video)


async def creative_from_draft(
    business_name: str, category: str, draft_id: str, user_id: str, db,
    goal: str = "messages", brand_id: Optional[str] = None,
) -> Optional[AdCreative]:
    """SOURCE 3 — reuse a content draft the user already generated and liked.
    Returns None if the draft can't be found (caller should 404)."""
    draft = await get_draft_image(draft_id, user_id, db)
    if draft is None or not draft["image_url"]:
        return None
    brand_context = await get_brand_context(user_id, db, brand_id)
    copy = await write_ad_copy(business_name, category, goal, draft["content"], brand_context)
    return assemble_creative(copy, draft["image_url"], source=CreativeSource.DRAFT)

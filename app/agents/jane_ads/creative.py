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

from .models import AdCopy, AdCreative, CreativeSource

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
                        city: str = "") -> AdCopy:
    """Write a short headline, primary text, and an image prompt (used only for the
    GENERATE source). Voice-matched to the brand playbook when a profile exists, and
    visually grounded in the real city/area the campaign targets."""
    if not settings.OPENAI_API_KEY:
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
        "Return ONLY the JSON."
    )
    try:
        client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
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
        )
    except Exception as e:
        print(f"[Creative] copy error: {e}", flush=True)
        return AdCopy()


# ── Source 1: GENERATE — brand data, direct controlled image call ────────────

async def _upload_bytes_to_cloudinary(img_bytes: bytes, public_id: str) -> Optional[str]:
    """Signed REST upload straight from `settings` (not the `cloudinary` SDK, which
    reads raw os.environ and silently no-ops when credentials only live in .env via
    pydantic Settings). Matches the pattern already proven in this package's b-roll
    generation — self-contained, no extra package coupling."""
    cloud, api_key, api_secret = (settings.CLOUDINARY_CLOUD_NAME, settings.CLOUDINARY_API_KEY,
                                   settings.CLOUDINARY_API_SECRET)
    if not all([cloud, api_key, api_secret]):
        return None
    ts = int(time.time())
    sig = hashlib.sha1(f"folder=uri-ads&public_id={public_id}&timestamp={ts}{api_secret}".encode()).hexdigest()
    form = aiohttp.FormData()
    form.add_field("file", img_bytes, filename=f"{public_id}.png", content_type="image/png")
    for k, v in (("api_key", api_key), ("timestamp", str(ts)), ("signature", sig),
                 ("public_id", public_id), ("folder", "uri-ads"), ("resource_type", "image")):
        form.add_field(k, v)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"https://api.cloudinary.com/v1_1/{cloud}/image/upload",
                              data=form, timeout=aiohttp.ClientTimeout(total=60)) as r:
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
        f"{(' Reflect this brand: ' + brand_bits) if brand_bits else ''} "
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
    own creative judgment. Returns a Cloudinary URL, or None on failure."""
    if not settings.OPENAI_API_KEY or not image_prompt.strip():
        return None
    full_prompt = _as_ad_content(image_prompt, brand_context)
    try:
        client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        resp = await client.images.generate(
            model="gpt-image-1", prompt=full_prompt, size="1024x1536", quality="high", n=1,
        )
        b64 = resp.data[0].b64_json if resp.data else None
        if not b64:
            return None
        import base64
        img_bytes = base64.b64decode(b64)
        return await _upload_bytes_to_cloudinary(img_bytes, f"ad-{uuid.uuid4().hex[:12]}")
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

def assemble_creative(copy: AdCopy, image_url: Optional[str],
                      source: CreativeSource = CreativeSource.GENERATE) -> AdCreative:
    """Combine copy + image into a submittable creative. The WhatsApp CTA is always
    attached; `generated` is False when there's no image (copy-only fallback)."""
    return AdCreative(
        image_url=image_url or "",
        headline=copy.headline,
        primary_text=copy.primary_text,
        cta=WHATSAPP_CTA,
        source=source,
        generated=bool(image_url),
    )


# ── The three entry points ────────────────────────────────────────────────────

async def generate_ad_creative(
    business_name: str, category: str, goal: str = "messages", description: str = "",
    user_id: str = "", db=None, brand_id: Optional[str] = None, city: str = "",
) -> AdCreative:
    """SOURCE 1 (default) — Jane writes the copy and generates the image herself,
    using the brand playbook's colours/voice/region/industry, grounded in `city` (the
    campaign's geo target, e.g. from the decision engine's geo pins) so the scene
    reflects the real place, not a generic global look. For "use my own photo", see
    SOURCE 2 (creative_from_upload) — a distinct source, not a reference nudge on
    generation. Never raises — falls back to copy-only if image generation fails."""
    brand_context = await get_brand_context(user_id, db, brand_id) if user_id else {}
    copy = await write_ad_copy(business_name, category, goal, description, brand_context, city)
    image_url = await generate_ad_image(copy.image_prompt, brand_context)
    return assemble_creative(copy, image_url, source=CreativeSource.GENERATE)


async def creative_from_upload(
    business_name: str, category: str, image_url: str, goal: str = "messages",
    description: str = "", user_id: str = "", db=None, brand_id: Optional[str] = None,
) -> AdCreative:
    """SOURCE 2 — the user's own uploaded photo/video (already hosted via the
    existing /upload-user-content flow) becomes the creative directly; Jane still
    writes fresh copy to match it. No location grounding needed — the photo IS the
    real place already."""
    brand_context = await get_brand_context(user_id, db, brand_id) if user_id else {}
    copy = await write_ad_copy(business_name, category, goal, description, brand_context)
    return assemble_creative(copy, image_url, source=CreativeSource.UPLOAD)


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

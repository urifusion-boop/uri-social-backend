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

from .models import AdCopy, AdCreative, CreativeSource, GeoMode, GeoPlan, ShootScript, ShootShot

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
    from app.agents.social_media_manager.services.style_library import pick_next_style
    try:
        resp = await BrandProfileService.get(user_id, db, brand_id)
        profile = resp.get("responseData") if isinstance(resp, dict) else None
        bc = BrandProfileService.to_brand_context(profile)
        if not bc:
            return bc
        # Organic content always picks a style (the brand's own rotation, or a
        # sensible default) before generating — ads never did, silently falling
        # back to _generate_platform_image's generic "immersive" composition with
        # no style_desc at all, a real, visible quality gap versus organic posts
        # from the same brand. Same selection the organic flow uses, so an ad
        # image looks as considered as a normal post.
        style_selections = bc.get("style_selections") or []
        style_fragments = (profile or {}).get("style_prompt_fragments") or []
        slug, fragment, _ = pick_next_style(
            style_selections, bc.get("style_rotation_index", 0), bc.get("industry", ""), style_fragments,
        )
        if fragment:
            bc = {**bc, "style_prompt_fragment": fragment, "style_slug": slug}
        return bc
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


def service_area_from_geo(geo: Optional[GeoPlan], fallback_city: str = "") -> str:
    """The one place a location legitimately belongs in ad COPY — never the raw
    geo-target used for targeting/image-grounding. Confirmed live bug: a campaign
    targeting Lekki, aimed at 'creative professionals', produced copy reading 'Lekki
    creatives' — the targeting parameter and an internal audience label, both
    written straight into the ad. `service_area` is the same place name in a
    different, customer-facing role: 'I deliver around X', not 'ad for X people'.
    Empty for NON_LOCAL (nothing to tell a customer — geography isn't how they'll
    find or reach the business)."""
    if geo is not None:
        if geo.mode == GeoMode.NON_LOCAL:
            return ""
        if geo.pins:
            return ", ".join(p.name for p in geo.pins[:3])
        if geo.fallback_area:
            return geo.fallback_area
        if geo.city:
            return geo.city
    return fallback_city


# Forbidden abstraction/hype words — Nigerian commerce copy is specific and
# transactional; ungoverned, the model defaults to a Western DTC register that
# identifies nothing and asks for nothing ("Elevate your everyday").
_FORBIDDEN_COPY_WORDS = (
    "premium", "quality", "unmatched", "world-class", "trusted partner", "solutions",
    "elevate", "transform your", "unlock", "experience the difference",
    "limited time", "hurry now", "discover the difference",
)


def _register_rules_block() -> str:
    """Nigerian commerce register (creative brief spec §5) — specific and
    transactional beats fluent and abstract, and it's also what performs under
    retrieval-driven delivery, since specificity is what tells the platform who
    the ad is for."""
    return (
        "REGISTER — write like a real Nigerian business owner texting a customer on "
        "WhatsApp, not a brand:\n"
        "- Name what it actually is (\"bags\", not \"accessories solutions\") — category "
        "words matter.\n"
        "- If a specific price or starting price is mentioned in the context below, "
        "state it plainly (e.g. \"₦12,000\", never \"12k\"). If no price was given, do "
        "NOT invent one.\n"
        "- End with a direct ask — \"Message me to order\", not \"reach out today\" or "
        "\"get in touch to learn more\".\n"
        "- Never use: " + ", ".join(f'"{w}"' for w in _FORBIDDEN_COPY_WORDS) + ".\n"
        "- No manufactured urgency for an evergreen offer. No emoji spam (one is fine, "
        "three fire emoji reads as a flyer)."
    )


def _leakage_terms(geo_target: str, audience_segment: str, interest_category: str,
                    geo_pockets: Optional[list[str]] = None) -> list[str]:
    """The exact values that must never appear verbatim in generated copy — they are
    targeting parameters, not message content. The reader already matches them;
    restating one to someone who matches it is always wasted, and concatenating a
    geo-target with an audience label is the exact observed bug ('Lekki creatives').

    `geo_pockets` (Multi-Plan Audience Variants spec §8) — a selected variant's own
    named areas (e.g. "Yaba", "Victoria Island") are real targeting parameters in
    their own right, distinct from and often narrower than `geo_target` (the city) —
    live-confirmed leak: a variant's pockets were never checked here at all, so they
    leaked straight into copy even though the broader city was correctly protected."""
    terms = [t.strip() for t in (geo_target, audience_segment, interest_category) if t and t.strip()]
    terms += [p.strip() for p in (geo_pockets or []) if p and p.strip()]
    return terms


def _check_leakage(headline: str, primary_text: str, forbidden_terms: list[str]) -> list[str]:
    """Deterministic, cheap check — catches the whole class of leakage bugs without
    trusting the model's own restraint. Case-insensitive substring match against the
    combined copy; returns which specific terms leaked, if any."""
    combined = f"{headline} {primary_text}".lower()
    return [t for t in forbidden_terms if t.lower() in combined]


def _strip_leaked_terms(text: str, leaked: list[str]) -> str:
    """Last-resort correction if a regenerate still leaks (never ship it, never loop
    silently) — remove the exact phrase and tidy up double spaces/punctuation."""
    import re as _re
    for term in leaked:
        text = _re.sub(_re.escape(term), "", text, flags=_re.IGNORECASE)
    return _re.sub(r"\s{2,}", " ", text).strip(" ,.-")


def _zone_a_block(geo_target: str, audience_segment: str, interest_category: str, goal: str,
                   geo_pockets: Optional[list[str]] = None) -> str:
    """Delivery context — labelled and passed so the model can write knowingly (e.g.
    'near you' makes sense because the reader is nearby), but explicitly barred from
    appearing in copy. Never omitted-by-silence: naming it AND prohibiting it beats
    just not mentioning it, per the creative brief spec's own reasoning.

    `geo_pockets` — a selected audience-plan variant's own named areas, distinct from
    (and often narrower than) `geo_target`; equally forbidden, see _leakage_terms."""
    geo_bit = f"the reader is already in/near {geo_target}" if geo_target else ""
    if geo_pockets:
        pockets_str = ", ".join(p for p in geo_pockets if p)
        geo_bit = (f"{geo_bit}, specifically: {pockets_str}" if geo_bit
                   else f"the reader is already in/near: {pockets_str}")
    bits = [geo_bit,
            f"the reader already matches this audience: {audience_segment}" if audience_segment else "",
            f"the reader is already interested in: {interest_category}" if interest_category else ""]
    bits = [b for b in bits if b]
    if not bits:
        return ""
    return (
        "DELIVERY CONTEXT (never write about this — the reader already matches it; "
        f"restating it is always wasted): {'; '.join(bits)}. Campaign goal: {goal}.\n"
    )


def _video_fit_fields_block() -> str:
    """Shared instructions for the video_recommended/video_recommendation_reason JSON
    fields — used by every copy-writing prompt (GENERATE and the image-matched path
    for upload/draft/recomposite) so the SHOULD-vs-CAN pushback (creative brief spec
    §6.2/§9) has a real signal regardless of which asset path the user picked, not
    just when Jane generated the image herself."""
    return (
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
    )


async def _call_ad_copy_model(prompt: str) -> Optional[dict]:
    """Shared model call for write_ad_copy — extracted so the leakage-check retry
    (§4 of the creative brief spec) can call it twice without duplicating the
    request/parse boilerplate. Returns None on any failure (caller falls back)."""
    try:
        client = openai.AsyncOpenAI(api_key=settings.jane_ads_openai_key)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            timeout=15,
        )
        return json.loads(resp.choices[0].message.content or "{}")
    except Exception as e:
        print(f"[Creative] copy error: {e}", flush=True)
        return None


async def write_ad_copy(business_name: str, category: str, goal: str = "messages",
                        description: str = "", brand_context: Optional[dict] = None,
                        city: str = "", behaviour: str = "", service_area: str = "",
                        audience_segment: str = "", who_its_for: str = "",
                        geo_pockets: Optional[list[str]] = None) -> AdCopy:
    """Write a short headline, primary text, and an image prompt (used only for the
    GENERATE source). Voice-matched to the brand playbook when a profile exists, and
    visually grounded in the real city/area the campaign targets. Also does the
    creative-type reasoning (PRD §4.1): flags when a video would clearly serve this
    ad better than the photo we're about to generate — gpt-image-1 can't produce
    one, so this is a heads-up for the caller to offer an upload, not a capability.

    Two-zone brief (creative brief spec §2): DELIVERY CONTEXT (geo/audience — never
    written) is kept separate from MESSAGE (what the ad actually says), backed by a
    deterministic post-generation leakage check — live-confirmed bug: a campaign
    targeting Lekki for 'creative professionals' produced copy reading 'Lekki
    creatives', the targeting parameters written straight into the ad.

    `audience_segment`/`who_its_for` (Multi-Plan Audience Variants spec §8): when a
    client selected a specific audience-plan variant, its own segment/phrasing
    override the brand-level default below — a plan named 'people fitting out a new
    place' must drive Zone B here, not the brand's generic target_audience.
    `geo_pockets` — that same variant's own named areas (e.g. "Yaba", "Victoria
    Island"), distinct from and often narrower than `city` — live-confirmed leak:
    these were never covered by the leakage check at all until this parameter was
    added, so a variant's specific areas leaked into copy even though the broader
    city was correctly protected."""
    if not settings.jane_ads_openai_key:
        return AdCopy()
    bc = brand_context or {}
    brand_bits = _brand_prompt_bits(bc)
    geo_target = city or bc.get("region", "")
    audience_segment = audience_segment or bc.get("target_audience", "")
    location_bit = _location_prompt_bit(geo_target, category)
    # NOTE: `category` is deliberately NOT passed as the Zone A interest-category term
    # here — it's what the business SELLS (Zone B's "sells" line names it on purpose,
    # per the register rule "name what it actually is"), not an ad-targeting interest
    # label. Conflating the two would make the leak check strip the business's own
    # product name out of otherwise-correct copy. No distinct interest_categories
    # signal is computed yet (out of scope this pass — see plan §2), so this stays "".
    zone_a = _zone_a_block(geo_target, audience_segment, "", goal, geo_pockets=geo_pockets)
    zone_b = (
        "MESSAGE (headline/primary_text come from THIS section only):\n"
        f"- sells: {category or 'local business'}{(' — ' + description) if description else ''}\n"
        + (f"- who_its_for (phrase it exactly like this, in the customer's own words): "
           f"{who_its_for}\n" if who_its_for else
           "- who_its_for: phrase how the customer would recognise themselves (e.g. "
           "\"if you run a small business...\") — never the audience label above\n")
        + (f"- service_area (mention this, not the delivery-context location above): "
           f"{service_area}\n" if service_area else "")
        + "- the_action: message on WhatsApp\n"
    )
    prompt = (
        f"Write a Meta/Instagram ad for '{business_name or bc.get('brand_name') or 'a business'}' "
        f"(a {category or 'local business'}) whose goal is {goal}.{(' ' + brand_bits) if brand_bits else ''}\n"
        f"{zone_a}{zone_b}"
        f"{location_bit}\n"
        f"Context: {description or 'none'}\n"
        f"How customers find this business: {behaviour or 'unknown'}.\n"
        f"{_register_rules_block()}\n"
        "Return JSON with:\n"
        "- headline: punchy, <= 5 words, no ALL CAPS, no emoji spam, from MESSAGE only\n"
        "- primary_text: 1–2 sentences in the brand voice above if given, following "
        "REGISTER, from MESSAGE only — never the DELIVERY CONTEXT above\n"
        "- image_prompt: a photorealistic scene for the creative image — real people/"
        "products/workspace fitting the business, in the brand's colours/setting and the "
        "LOCATION above. NO text anywhere in the image — no logos, no watermarks, no "
        "storefront signage/shop signs with the business name, no readable words of any "
        "kind (Meta adds the ad's headline and button separately; baked-in text is never "
        "wanted here, including on buildings/signs in the background). Not an "
        "illustration.\n"
        f"{_video_fit_fields_block()}"
        "Return ONLY the JSON."
    )
    d = await _call_ad_copy_model(prompt)
    if d is None:
        return AdCopy()
    copy = AdCopy(
        headline=str(d.get("headline", "")).strip(),
        primary_text=str(d.get("primary_text", "")).strip(),
        image_prompt=str(d.get("image_prompt", "")).strip(),
        video_recommended=bool(d.get("video_recommended")),
        video_recommendation_reason=str(d.get("video_recommendation_reason", "")).strip(),
    )
    leak_terms = _leakage_terms(geo_target, audience_segment, "", geo_pockets=geo_pockets)
    leaked = _check_leakage(copy.headline, copy.primary_text, leak_terms)
    if leaked:
        retry_prompt = (
            prompt + f"\n\nCORRECTION: your last attempt wrote {leaked!r} directly into the "
            "copy — that's DELIVERY CONTEXT, forbidden in the message. Rewrite headline and "
            "primary_text without it."
        )
        d2 = await _call_ad_copy_model(retry_prompt)
        if d2 is not None:
            copy.headline = str(d2.get("headline", "")).strip() or copy.headline
            copy.primary_text = str(d2.get("primary_text", "")).strip() or copy.primary_text
        leaked = _check_leakage(copy.headline, copy.primary_text, leak_terms)
        if leaked:
            # Never ship a known leak and never loop silently — strip and move on.
            copy.headline = _strip_leaked_terms(copy.headline, leaked)
            copy.primary_text = _strip_leaked_terms(copy.primary_text, leaked)
    return copy


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


async def generate_ad_image(content: str, brand_context: Optional[dict] = None,
                            seed: str = "", reference_image: Optional[str] = None) -> Optional[str]:
    """Generate the ad image using the SAME content engine normal posts use
    (ImageContentService._generate_platform_image) — richer, on-brand visuals than a bare
    gpt-image-1 call, which is what the ad flow wanted (Jane's own images weren't good
    enough). Rendered 9:16 (story) for ad placements, with the brand playbook applied by
    the engine (colours, fonts, logo). Returns a hosted URL, or None on failure.

    `content` is the theme/message the image should convey; `seed` is an optional scene
    hint. The engine may render brand text into the graphic (that's how organic content
    looks) — the ad's headline/primary_text are still separate fields the platform overlays.

    `reference_image` (creative brief spec §7.2, RECOMPOSITE path) — when given a real
    product photo URL, the engine's existing background-removal → forensic product
    analysis → preservation pipeline keeps the product exact and regenerates only the
    scene around it. This is the SAME parameter/pipeline the organic content engine
    already uses for its own recompositing; not new image-generation work, just reuse."""
    if not (content or "").strip():
        return None
    bc = brand_context or {}
    try:
        from app.agents.social_media_manager.services.image_content_service import ImageContentService
        result = await ImageContentService._generate_platform_image(
            platform="instagram",
            content=content,
            seed_content=seed or content,
            brand_context=bc,
            reference_image=reference_image,
            image_type="story",
        )
        if not result.get("status"):
            print(f"[Creative] content-engine image failed: {result.get('error')}", flush=True)
            return None
        image_url = (result.get("responseData") or {}).get("image_url")
        # The content engine returns a raw base64 data: URL, not a hosted one (confirmed
        # live: it's what caused Meta's ad-creative creation to fail with a generic,
        # unhelpful "code=1, unknown error" — Meta's crawler can't fetch a data URI as a
        # picture). Every OTHER caller of this same engine uploads to Cloudinary first;
        # this one didn't. Mirror that here so ads always get a real hosted URL.
        if image_url and image_url.startswith("data:"):
            from app.utils.cloudinary_upload import upload_base64
            try:
                image_url = await upload_base64(image_url, folder="uri-social/jane-ads")
            except Exception as e:
                print(f"[Creative] Cloudinary upload failed, cannot use for a Meta ad: {e}", flush=True)
                return None
        return image_url
    except Exception as e:
        print(f"[Creative] image error: {e}", flush=True)
        return None


async def describe_ad_image(image_url: str) -> str:
    """One-sentence description of what a generated/uploaded image actually shows, via a
    vision model — so the caption written afterward references the real visual instead of
    being written blind. Best-effort; '' on any failure."""
    if not settings.jane_ads_openai_key or not (image_url or "").strip():
        return ""
    try:
        client = openai.AsyncOpenAI(api_key=settings.jane_ads_openai_key)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": [
                {"type": "text", "text": (
                    "Describe what this image shows in ONE concrete sentence — subject, "
                    "setting, mood, and any visible text. This will be used to write a "
                    "matching ad caption.")},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]}],
            timeout=20,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"[Creative] image describe error: {e}", flush=True)
        return ""


async def write_ad_copy_for_image(image_summary: str, business_name: str, category: str,
                                  goal: str = "messages", description: str = "",
                                  brand_context: Optional[dict] = None, city: str = "",
                                  service_area: str = "", audience_segment: str = "",
                                  who_its_for: str = "",
                                  geo_pockets: Optional[list[str]] = None) -> AdCopy:
    """Write the headline + primary text to MATCH a specific image (its vision description),
    so the caption references what's actually on screen instead of a generic line. Returns
    an AdCopy with only headline + primary_text set.

    Same two-zone/leakage-check treatment as write_ad_copy (creative brief spec §2-4) —
    this path (upload/draft/recomposite) has the identical leakage risk since it also
    has geo/audience context available. `audience_segment`/`who_its_for`/`geo_pockets`
    — see write_ad_copy's docstring (Multi-Plan Audience Variants spec §8)."""
    if not settings.jane_ads_openai_key or not image_summary.strip():
        return AdCopy()
    bc = brand_context or {}
    brand_bits = _brand_prompt_bits(bc)
    geo_target = city or bc.get("region", "")
    audience_segment = audience_segment or bc.get("target_audience", "")
    # NOTE: `category` is deliberately NOT passed as the Zone A interest-category term
    # here — it's what the business SELLS (Zone B's "sells" line names it on purpose,
    # per the register rule "name what it actually is"), not an ad-targeting interest
    # label. Conflating the two would make the leak check strip the business's own
    # product name out of otherwise-correct copy. No distinct interest_categories
    # signal is computed yet (out of scope this pass — see plan §2), so this stays "".
    zone_a = _zone_a_block(geo_target, audience_segment, "", goal, geo_pockets=geo_pockets)
    zone_b = (
        "MESSAGE (headline/primary_text come from THIS section only):\n"
        f"- sells: {category or 'local business'}{(' — ' + description) if description else ''}\n"
        + (f"- who_its_for (phrase it exactly like this, in the customer's own words): "
           f"{who_its_for}\n" if who_its_for else
           "- who_its_for: phrase how the customer would recognise themselves — never "
           "the audience label above\n")
        + (f"- service_area (mention this, not the delivery-context location above): "
           f"{service_area}\n" if service_area else "")
        + "- the_action: message on WhatsApp\n"
    )
    prompt = (
        f"Write the copy for a Meta/Instagram ad for "
        f"'{business_name or bc.get('brand_name') or 'a business'}' (a "
        f"{category or 'local business'}) whose goal is {goal}.{(' ' + brand_bits) if brand_bits else ''}\n"
        f"{zone_a}{zone_b}"
        f"The ad's IMAGE shows: {image_summary}.\n"
        f"Context: {description or 'none'}\n"
        f"{_register_rules_block()}\n"
        "Write copy that clearly connects to what's in the image above — the headline and "
        "body should feel like they belong with that visual, not generic.\n"
        "Return JSON with:\n"
        "- headline: punchy, <= 5 words, no ALL CAPS, no emoji spam, from MESSAGE only\n"
        "- primary_text: 1–2 sentences in the brand voice above if given, following "
        "REGISTER, referencing what the image shows — from MESSAGE only, never the "
        "DELIVERY CONTEXT above\n"
        f"{_video_fit_fields_block()}"
        "Return ONLY the JSON."
    )
    d = await _call_ad_copy_model(prompt)
    if d is None:
        return AdCopy()
    copy = AdCopy(
        headline=str(d.get("headline", "")).strip(),
        primary_text=str(d.get("primary_text", "")).strip(),
        video_recommended=bool(d.get("video_recommended")),
        video_recommendation_reason=str(d.get("video_recommendation_reason", "")).strip(),
    )
    leak_terms = _leakage_terms(geo_target, audience_segment, "", geo_pockets=geo_pockets)
    leaked = _check_leakage(copy.headline, copy.primary_text, leak_terms)
    if leaked:
        retry_prompt = (
            prompt + f"\n\nCORRECTION: your last attempt wrote {leaked!r} directly into the "
            "copy — that's DELIVERY CONTEXT, forbidden in the message. Rewrite without it."
        )
        d2 = await _call_ad_copy_model(retry_prompt)
        if d2 is not None:
            copy.headline = str(d2.get("headline", "")).strip() or copy.headline
            copy.primary_text = str(d2.get("primary_text", "")).strip() or copy.primary_text
        leaked = _check_leakage(copy.headline, copy.primary_text, leak_terms)
        if leaked:
            copy.headline = _strip_leaked_terms(copy.headline, leaked)
            copy.primary_text = _strip_leaked_terms(copy.primary_text, leaked)
    return copy


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
                      is_video: Optional[bool] = None,
                      service_area: str = "") -> AdCreative:
    """Combine copy + media into a submittable creative. The WhatsApp CTA is always
    attached; `generated` is False when there's no media (copy-only fallback).
    `is_video` should be passed explicitly when the caller already knows the media
    type (e.g. from the upload's content-type); otherwise it's guessed from the URL.
    The video recommendation applies to ANY source whose media isn't already a video
    (creative brief spec §6.2/§9 pushback reconciliation) — a static upload/draft/
    recomposite that Jane judges would clearly work better as video gets the same
    heads-up as GENERATE, since the SHOULD (video would serve better) vs CAN (the
    user brought a photo) tension applies just as much there.

    Attribute tagging (creative brief spec §11) computed here, cheaply, from the
    final copy — never model-generated, so it's trustworthy for later analysis."""
    url = image_url or ""
    resolved_is_video = is_video if is_video is not None else _looks_like_video(url)
    combined = f"{copy.headline} {copy.primary_text}"
    return AdCreative(
        image_url=url,
        is_video=resolved_is_video,
        headline=copy.headline,
        primary_text=copy.primary_text,
        cta=WHATSAPP_CTA,
        source=source,
        generated=bool(url),
        video_recommendation=copy.video_recommendation_reason if (
            copy.video_recommended and not resolved_is_video
        ) else "",
        asset_path=source,
        shows_price="₦" in combined,
        shows_service_area=bool(service_area) and service_area.lower() in combined.lower(),
        copy_length=len(combined),
    )


# ── The three entry points ────────────────────────────────────────────────────

async def generate_ad_creative(
    business_name: str, category: str, goal: str = "messages", description: str = "",
    user_id: str = "", db=None, brand_id: Optional[str] = None, city: str = "",
    behaviour: str = "", service_area: str = "", audience_segment: str = "",
    who_its_for: str = "", geo_pockets: Optional[list[str]] = None,
) -> AdCreative:
    """SOURCE 1 (default) — Jane writes the copy and generates the image herself,
    using the brand playbook's colours/voice/region/industry, grounded in `city` (the
    campaign's geo target, e.g. from the decision engine's geo pins) so the scene
    reflects the real place, not a generic global look. `behaviour` (search/discover/
    mixed, from the decision engine) informs the creative-type reasoning (PRD §4.1) —
    see write_ad_copy. `service_area` (creative brief spec §4, from service_area_from_geo)
    is the ONLY location string allowed to reach the copy itself — `city` stays targeting
    context. `audience_segment`/`who_its_for` (Multi-Plan Audience Variants spec §8) —
    when a client selected a specific audience-plan variant, its segment/phrasing
    override the brand's generic target_audience for this one campaign. For "use my
    own photo", see SOURCE 2 (creative_from_upload) — a distinct source, not a
    reference nudge on generation. Never raises — falls back to copy-only if image
    generation fails."""
    brand_context = await get_brand_context(user_id, db, brand_id) if user_id else {}
    copy = await write_ad_copy(business_name, category, goal, description, brand_context,
                               city, behaviour, service_area, audience_segment, who_its_for,
                               geo_pockets=geo_pockets)
    # Image via the SAME content engine normal posts use (better visuals). Seed it with the
    # scene idea; content conveys the theme/message so the graphic is on-topic.
    content_for_image = copy.primary_text or copy.image_prompt or f"{business_name} — {description or category}"
    image_url = await generate_ad_image(content_for_image, brand_context, seed=copy.image_prompt)
    # Caption LAST, matched to the actual generated image (vision) so it references the
    # real visual instead of a generic line ("caption doesn't add up"). Falls back to the
    # original copy if the vision pass fails.
    if image_url:
        summary = await describe_ad_image(image_url)
        if summary:
            matched = await write_ad_copy_for_image(
                summary, business_name, category, goal, description, brand_context, city,
                service_area, audience_segment, who_its_for, geo_pockets=geo_pockets,
            )
            if matched.headline:
                copy.headline = matched.headline
            if matched.primary_text:
                copy.primary_text = matched.primary_text
    return assemble_creative(copy, image_url, source=CreativeSource.GENERATE, service_area=service_area)


async def creative_from_upload(
    business_name: str, category: str, image_url: str, goal: str = "messages",
    description: str = "", user_id: str = "", db=None, brand_id: Optional[str] = None,
    is_video: Optional[bool] = None, city: str = "", service_area: str = "",
    audience_segment: str = "", who_its_for: str = "", geo_pockets: Optional[list[str]] = None,
) -> AdCreative:
    """SOURCE 2 — the user's own uploaded photo OR video (uploaded via
    /jane-ads/creative/upload, or the existing /upload-user-content flow) becomes
    the creative directly; Jane still writes fresh copy to match it. No location
    grounding needed for the IMAGE — the media IS the real place already — but the
    copy still needs `service_area`/`city` for the same leakage-check treatment as
    every other path. `audience_segment`/`who_its_for`/`geo_pockets` — see
    write_ad_copy's docstring (Multi-Plan Audience Variants spec §8)."""
    brand_context = await get_brand_context(user_id, db, brand_id) if user_id else {}
    # Caption matched to what the uploaded photo actually shows (skip for video — no still
    # to describe), so the copy references the real visual.
    summary = "" if is_video else await describe_ad_image(image_url)
    if summary:
        copy = await write_ad_copy_for_image(
            summary, business_name, category, goal, description, brand_context, city,
            service_area, audience_segment, who_its_for, geo_pockets=geo_pockets,
        )
    else:
        copy = await write_ad_copy(business_name, category, goal, description, brand_context, city,
                                   service_area=service_area, audience_segment=audience_segment,
                                   who_its_for=who_its_for, geo_pockets=geo_pockets)
    return assemble_creative(copy, image_url, source=CreativeSource.UPLOAD, is_video=is_video,
                             service_area=service_area)


async def creative_from_draft(
    business_name: str, category: str, draft_id: str, user_id: str, db,
    goal: str = "messages", brand_id: Optional[str] = None, city: str = "", service_area: str = "",
    audience_segment: str = "", who_its_for: str = "", geo_pockets: Optional[list[str]] = None,
) -> Optional[AdCreative]:
    """SOURCE 3 — reuse a content draft the user already generated and liked.
    Returns None if the draft can't be found (caller should 404)."""
    draft = await get_draft_image(draft_id, user_id, db)
    if draft is None or not draft["image_url"]:
        return None
    brand_context = await get_brand_context(user_id, db, brand_id)
    # Caption matched to what the chosen draft image shows, falling back to the draft's
    # own text if the vision pass fails.
    summary = await describe_ad_image(draft["image_url"]) or draft["content"]
    copy = await write_ad_copy_for_image(
        summary, business_name, category, goal, draft["content"], brand_context, city,
        service_area, audience_segment, who_its_for, geo_pockets=geo_pockets,
    )
    return assemble_creative(copy, draft["image_url"], source=CreativeSource.DRAFT, service_area=service_area)


async def creative_from_recomposite(
    business_name: str, category: str, reference_image_url: str, goal: str = "messages",
    description: str = "", user_id: str = "", db=None, brand_id: Optional[str] = None,
    city: str = "", service_area: str = "", audience_segment: str = "", who_its_for: str = "",
    geo_pockets: Optional[list[str]] = None,
) -> AdCreative:
    """SOURCE 4 — the user's own real product photo, recomposited: background
    cleaned/replaced, the product itself preserved exactly (creative brief spec §7,
    product truthfulness rule — a physical good the customer will receive is never
    regenerated from text). Reuses the SAME pipeline the organic content engine
    already has for this (`ImageContentService._generate_platform_image` with a
    `reference_image` — background removal → forensic product analysis →
    preservation block, confirmed already built and battle-tested); this is new
    wiring, not a new image-generation capability."""
    brand_context = await get_brand_context(user_id, db, brand_id) if user_id else {}
    content_for_image = f"{business_name or category} — {description or category}"
    image_url = await generate_ad_image(
        content_for_image, brand_context, seed=description, reference_image=reference_image_url,
    )
    final_image = image_url or reference_image_url
    summary = await describe_ad_image(final_image)
    if summary:
        copy = await write_ad_copy_for_image(
            summary, business_name, category, goal, description, brand_context, city,
            service_area, audience_segment, who_its_for, geo_pockets=geo_pockets,
        )
    else:
        copy = await write_ad_copy(business_name, category, goal, description, brand_context, city,
                                   audience_segment=audience_segment, who_its_for=who_its_for,
                                   service_area=service_area, geo_pockets=geo_pockets)
    return assemble_creative(copy, final_image, source=CreativeSource.RECOMPOSITE, service_area=service_area)

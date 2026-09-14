"""
Layer 2 image generation for VSG-01-PROMPTS v3's generation-dependent formats
(§1, §2A/§2B/§2C) — deliberately NOT creative.py's existing generate_ad_image()/
ImageContentService._generate_platform_image().

That pipeline is built for organic content, and by its own documentation
may render brand text/graphics directly into the pixels ("the engine may
render brand text into the graphic (that's how organic content looks)" —
generate_ad_image's own docstring). §1.1 is a hard rule that binds every
format in this library: "Layer 2 never produces readable text... every
format here is text-bearing... Garbled type in a paid ad reads as a scam
artefact in a market already screening for them (SEED-048). All copy,
prices, stars, badges and brand marks are placed in Layer 4." Reusing the
organic engine's own prompt construction would risk exactly the failure
this document exists to prevent.

What IS safely reusable is the raw primitive underneath that engine:
ImageContentService._call_dalle_api(prompt, size, ...) — a
prompt-to-image call with no brand-text-baking prompt construction wrapped
around it (that construction lives in the organic engine's callers, not
in this function). generate_scene() calls it directly with a prompt this
module builds itself, appending §2A/§2B/§2C verbatim, in that order, per
v3 §1's assembly spec and §12's implementation notes.

_call_dalle_api returns a data: URL, not a hosted one —
DocumentRendererService's ai_generated_background layer fetches real
http(s) URLs only (confirmed: its _fetch_image has no data: URI support).
generate_scene uploads to Cloudinary first, mirroring the exact same
upload-then-use pattern generate_ad_image already established for the
same reason (Meta's ad-creative pipeline can't fetch a data: URI either).

v3 upgrade note: v2's prompts described only what's IN the photograph.
v3's central change is that every generated visual must additionally be
composed AS AN AD — focal hierarchy, a copy-safe zone reserved for Layer 4
typography, deliberate margins, mobile-scale readability. Rather than
rewrite that composition language into every format's own scene prompt,
it lives here once (§2A, §2B) and is appended to every generate_scene()
call automatically — the same one-choke-point reasoning §2C's negative
prompt already used. A format's own prompt (problem_solution.py,
work_in_progress.py, etc.) still owns what the scene IS; this module owns
how it must be composed to receive an ad's typography afterward.
"""
from typing import Optional

# §2A verbatim — appended to every Layer 2 generation call this module makes.
# "Compose as an ad, not a standalone photograph" — the core v3 upgrade.
COMPOSITION_DIRECTIVE = (
    "Compose the image as a finished static social-media advertisement rather than "
    "as a standalone photograph. Establish one dominant focal point and one clearly "
    "defined secondary information area. Create a deliberate visual hierarchy that "
    "can be understood quickly on a mobile screen. Use intentional negative space as "
    "part of the composition — do not fill empty areas simply because space is "
    "available. Maintain generous safe margins from every canvas edge; keep important "
    "subjects, faces, products, hands and other critical details away from crop "
    "boundaries. The composition must feel deliberately designed: balanced subject "
    "scale, controlled spacing, believable perspective, coherent lighting and a clear "
    "relationship between the focal subject and the future advertising copy. Avoid "
    "visual clutter — every object in the frame must have a functional visual purpose."
)

# §2B verbatim — appended to every Layer 2 generation call this module makes.
# Reserves the zone Layer 4 will actually draw typography into.
TYPOGRAPHY_DIRECTIVE = (
    "Advertising copy will be added separately in the final Layer 4 design. Do not "
    "generate readable advertising text, logos, brand marks, interface elements, "
    "buttons or promotional typography inside the image. Instead, deliberately compose "
    "the scene around the intended copy area. The copy-safe area must remain visually "
    "quiet: no important faces, hands, high-contrast objects, complex patterns, strong "
    "shadows, bright highlights, product details or competing focal points. Leave "
    "generous breathing room around the future headline, supporting copy, price and "
    "call-to-action. The background beneath the future typography should have "
    "sufficient tonal consistency and visual simplicity to support highly legible "
    "text. The final composition must remain attractive and intentional even before "
    "the advertising copy is added."
)

# §2C verbatim — appended to every Layer 2 generation call this module makes.
GLOBAL_NEGATIVE_PROMPT = (
    "no text, no lettering, no words, no numbers, no logos, no watermarks, no brand marks, "
    "no readable signage, no generated advertising copy, no user interface elements, "
    "no fake buttons, no clickable-looking controls, no icons unless specifically requested, "
    "no distorted hands, no extra fingers, no malformed anatomy, no plastic skin, "
    "no waxy texture, no over-smoothed faces, no artificial HDR halo, no lens flare, "
    "no generic stock-photo styling, no excessive cinematic grading, "
    "no unnecessary props, no visual clutter, no random decorative objects, "
    "no Western suburban setting unless explicitly required, "
    "no snow, no autumn foliage, "
    # Added after a live-confirmed failure mode: gpt-image-2 rendered "medium
    # brown" skin twice in a row on a real generation despite
    # REPRESENTATION_BLOCK's positive instruction below — a negative
    # alongside the positive gives the model two independent signals
    # instead of one. No-op (harmless) on any generation with no person.
    "no light or medium-brown skin, no fair or olive skin, no lightened skin tone"
)


# §3 verbatim — one of these three is resolved and inserted per generation
# by _resolve_ratio_clause() below, in the §12 assembly position (right
# after the format-specific prompt, before the representation/composition/
# typography/negative blocks). "Generate each required ratio independently
# where composition depends on negative space. Do not rely on cropping one
# composition into another ratio" — so this is picked from the actual pixel
# dimensions being requested, not assumed square.
RATIO_CLAUSE_1_1 = (
    "square composition with balanced outer margins, one dominant focal area, "
    "subject occupying approximately 40-55% of the visual field where appropriate, "
    "clear separation between the focal subject and the copy-safe area, "
    "no important detail touching the crop boundaries"
)
RATIO_CLAUSE_4_5 = (
    "vertical portrait advertising composition, strong visual hierarchy from upper "
    "area to lower information area, subject concentrated primarily in the upper "
    "or middle portion, approximately 25-35% of the lower frame kept calm and "
    "copy-safe where the format requires an offer or action, generous side margins, "
    "important details protected from the crop boundaries"
)
RATIO_CLAUSE_9_16 = (
    "tall vertical advertising composition with a deliberate top-to-bottom visual "
    "journey, primary subject concentrated around the middle 40-60% unless the "
    "format specifies another hierarchy, generous copy-safe space above or below, "
    "clear separation between visual zones, important details protected from the "
    "extreme top and bottom crop areas, generous breathing room around all major "
    "elements"
)

# §3's own three named ratios as (width, height) targets to measure distance
# against — not a hardcoded aspect-ratio-value table, so a caller passing an
# unusual size (e.g. a half-canvas zone like Problem/Solution's) still
# resolves to whichever of the three it's closest to, rather than needing a
# fourth case.
_RATIO_TARGETS = (
    (1, 1, RATIO_CLAUSE_1_1),
    (4, 5, RATIO_CLAUSE_4_5),
    (9, 16, RATIO_CLAUSE_9_16),
)


def _resolve_ratio_clause(size: str) -> str:
    """Picks the §3 ratio clause whose aspect ratio is closest to the actual
    width x height being generated. Falls back to 1:1 (the library's
    universal default before this ratio work) if size can't be parsed —
    matching generate_scene's own default rather than raising, since a
    malformed size string is still going somewhere and getting SOME
    composition guidance beats getting none."""
    try:
        width, height = map(int, size.lower().split("x"))
        aspect = width / height
    except (ValueError, ZeroDivisionError, AttributeError):
        return RATIO_CLAUSE_1_1
    return min(_RATIO_TARGETS, key=lambda t: abs(aspect - t[0] / t[1]))[2]


# §4 verbatim — appended by a format's own scene-prompt builder wherever a
# person appears in the generated scene (conditional, unlike §2A/§2B/§2C
# above, which apply to every generation regardless of subject). Shared here
# so every format that can show a person (Problem/Solution, Work In
# Progress, and any future one) uses identical wording rather than each
# hand-rolling its own — confirmed live this session that a shorter,
# skin-tone-only phrase still let a generation through at "medium brown";
# the fuller block (skin tone + explicit West African features + natural
# unretouched texture) is what skin_tone_check.py's own docstring
# recommends. Verify skin rendering on every generation regardless — models
# are known to lighten skin under bright-light prompts even when told not to.
REPRESENTATION_BLOCK = (
    "Any person shown must have deep brown to dark brown skin — a strict requirement, "
    "never light, fair, tan, medium-brown or olive. West African features, Nigerian "
    "setting where contextually appropriate, strong equatorial daylight or realistic "
    "Nigerian available light, natural unretouched skin texture, believable local "
    "environmental details, authentic contemporary Nigerian clothing and objects "
    "where relevant"
)


# §8 — appended by a format's own scene-prompt builder only when a seasonal
# context is actually supplied (the slot is optional; most generations pass
# none, per §8's own caution below). Deliberately phrased as a SUBTLE
# influence, not a decoration instruction, mirroring §8's exact wording:
# "should influence environmental cues, styling and relevance without
# overwhelming the core visual hierarchy" and "Do not add seasonal
# decorations merely because the slot is populated." Validated against
# visual_slots.SEASONAL_CONTEXTS before use — same closed-vocabulary
# reasoning as every other slot in this library.
def seasonal_context_clause(seasonal_context) -> str:
    """Returns "" when seasonal_context is None/empty (the common case —
    nothing is appended to the prompt), otherwise a single validated
    sentence. Raises visual_slots.InvalidSlotValue for a value outside
    §8's enumerated list, same fail-closed behaviour as
    resolve_nigerian_setting."""
    from .visual_slots import resolve_seasonal_context

    resolved = resolve_seasonal_context(seasonal_context)
    if not resolved:
        return ""
    return (
        f"Where it feels natural, let {resolved} subtly inform environmental cues, "
        "styling and mood. Do not add seasonal decorations or props simply because "
        "this context is present — it should read as a quiet, believable backdrop "
        "detail, never as the focus of the composition."
    )


# A general nearest-named-color lookup (CSS/X11-style basic palette) — not a
# per-brand or per-format hardcode. Works for any hex code any business's
# Brand Playbook happens to store, the same way organic content's own
# _generate_image_brief asks GPT to name brand colors in words ("say 'deep
# magenta' not '#CD1B78'") rather than pass hex straight through — an image
# model renders a literal hex string as on-canvas text if it ever leaks
# through, so naming is not cosmetic, it's a correctness requirement.
_NAMED_COLORS: dict[str, tuple[int, int, int]] = {
    "black": (0, 0, 0), "charcoal": (54, 54, 54), "dark grey": (89, 89, 89),
    "grey": (128, 128, 128), "silver": (192, 192, 192), "white": (255, 255, 255),
    "deep red": (139, 0, 0), "red": (220, 20, 20), "crimson": (200, 20, 60),
    "burgundy": (128, 0, 32), "maroon": (95, 33, 32), "rust": (183, 65, 14),
    "terracotta": (204, 78, 46), "orange": (230, 126, 34), "warm orange": (216, 100, 33),
    "amber": (230, 160, 20), "gold": (212, 175, 55), "saffron": (244, 196, 48),
    "mustard": (200, 164, 32), "yellow": (241, 196, 15), "olive": (110, 110, 30),
    "lime": (140, 200, 60), "dark green": (20, 90, 50), "forest green": (34, 90, 34),
    "green": (46, 160, 67), "emerald": (16, 160, 110), "teal": (20, 130, 130),
    "turquoise": (48, 190, 190), "cyan": (60, 200, 220), "deep blue": (20, 50, 130),
    "navy": (25, 40, 90), "blue": (41, 98, 189), "sky blue": (100, 170, 230),
    "indigo": (60, 40, 140), "deep purple": (85, 30, 120), "purple": (128, 60, 170),
    "violet": (150, 100, 200), "magenta": (200, 30, 140), "deep magenta": (150, 20, 100),
    "pink": (230, 100, 160), "hot pink": (230, 40, 130), "rose": (210, 100, 120),
    "brown": (110, 70, 40), "dark brown": (70, 45, 25), "tan": (200, 170, 130),
    "beige": (220, 200, 170), "cream": (240, 230, 200),
}


def _hex_to_color_word(hex_code: str) -> Optional[str]:
    """Nearest named color by Euclidean RGB distance. Returns None for
    anything that isn't a parseable 3/6-digit hex string — a caller should
    just skip that entry rather than fabricate a name."""
    code = (hex_code or "").strip().lstrip("#")
    if len(code) == 3:
        code = "".join(c * 2 for c in code)
    if len(code) != 6:
        return None
    try:
        r, g, b = (int(code[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None
    return min(
        _NAMED_COLORS.items(),
        key=lambda item: sum((a - b) ** 2 for a, b in zip((r, g, b), item[1])),
    )[0]


def brand_palette_clause(brand_context: Optional[dict]) -> str:
    """A general clause folding a business's REAL brand info into the
    scene as environmental influence — the same strategy organic content's
    _generate_image_brief already uses (brand colors "MUST appear...
    incorporate them in clothing, props, or environmental accents", plus a
    brand_block folding in business_description/industry so props and
    setting feel like THIS business, not a generic stock scene), adapted
    for this library's own absolute rule that Layer 2 never renders text,
    logos or brand marks (§1.1 — unaffected by this clause, still enforced
    separately by TYPOGRAPHY_DIRECTIVE + GLOBAL_NEGATIVE_PROMPT below).

    Reads generically from whatever brand_context dict is passed — works
    for any business's Playbook data, nothing here is specific to one
    format, one industry or one brand. Returns "" (no-op) when there's
    nothing usable at all, matching every other optional clause in this
    module (seasonal_context_clause does the same); either piece
    (colors/description) is independently optional."""
    # Capped, not passed through raw: business_description is free text a
    # business can write at any length, and this whole module assembles
    # prompts by plain string concatenation with a hard total-length budget
    # (see generate_scene's _DALLE_MAX_CHARS) — unlike organic content's own
    # _generate_image_brief, which safely absorbs arbitrary-length brand
    # fields because an LLM rewrites everything into a bounded brief before
    # it reaches the image model. An unbounded description here would eat
    # generate_scene's length budget unpredictably, in the worst case past
    # zero (a negative Python slice silently reads from the string's end,
    # not the failure this should surface as).
    _MAX_DESCRIPTION_CHARS = 200

    bc = brand_context or {}
    colors = bc.get("brand_colors") or []
    words = list(dict.fromkeys(w for w in (_hex_to_color_word(c) for c in colors[:3]) if w))
    description = (bc.get("business_description") or "").strip()
    if len(description) > _MAX_DESCRIPTION_CHARS:
        description = description[:_MAX_DESCRIPTION_CHARS].rsplit(" ", 1)[0] + "…"

    parts: list[str] = []
    if words:
        palette = ", ".join(words)
        parts.append(
            f"This brand's real color palette is {palette} — let it subtly and naturally "
            "inform the scene wherever it fits (environmental accents, props, wardrobe, "
            "signage-free surfaces), without forcing it or making the palette itself the "
            "subject."
        )
    if description:
        parts.append(
            f"Real business context, for authentic props/setting/styling choices only: "
            f"{description}"
        )
    if not parts:
        return ""
    parts.append("Do not render any text, logo, or brand mark of any kind.")
    return " ".join(parts)


class SceneGenerationFailed(RuntimeError):
    """Raised when the underlying generation call or the follow-up
    Cloudinary upload fails — a format's render() should not silently
    proceed with a missing background."""
    pass


async def generate_scene(
    prompt: str, size: str = "1080x1080", brand_context: Optional[dict] = None,
) -> str:
    """
    Generate a single Layer 2 scene image and return a real hosted URL.

    prompt: the scene description ONLY — this function appends the §3 ratio
    clause (resolved from size), the brand palette clause (if brand_context
    has real colors — see brand_palette_clause), COMPOSITION_DIRECTIVE,
    TYPOGRAPHY_DIRECTIVE and GLOBAL_NEGATIVE_PROMPT itself, in that order
    (v3 §12's assembly: format prompt, ratio clause, representation if a
    person appears — already inline in the format prompt itself for this
    library, see REPRESENTATION_BLOCK's own docstring — then composition,
    typography, negative), so callers should not duplicate any of them.
    size: "WIDTHxHEIGHT" — passed straight through to _call_dalle_api,
    which internally buckets to the nearest square/landscape/portrait
    generation size and crops to the exact requested dimensions. Also used
    here to resolve which of §3's three ratio clauses (1:1/4:5/9:16) best
    matches what's actually being generated.
    brand_context: the same dict BrandProfileService.to_brand_context
    already produces for organic content (brand_colors, etc.) — optional,
    and a no-op when omitted or when it has no usable brand_colors, so
    every existing caller that doesn't pass it keeps behaving exactly as
    before.

    Raises SceneGenerationFailed on any failure rather than returning None
    — every caller in this format library needs a real background to
    composite text onto; there is no meaningful "partial" result.
    """
    from app.agents.social_media_manager.services.image_content_service import ImageContentService

    # Explicitly pinned to gpt-image-2 (image_model="openai/gpt-image-2") —
    # without this, ImageContentService._call_dalle_api (the name predates
    # its current provider chain) falls through to Google Imagen 4.0 Ultra
    # ("nano-banana-2") first, then OpenAI gpt-image-1.5, neither of which is
    # gpt-image-2. This is the same image_model value every other
    # gpt-image-2 caller in the app already uses (product-photo edits,
    # brand-guide generation), just with no reference image, so it takes
    # the text-to-image branch of that same path.
    #
    # OpenAI's actual documented prompt limit for the gpt-image family is
    # 32,000 characters (confirmed: _call_dalle_api's gpt-image-2 branch
    # passes `prompt` straight through to images.edit/images.generate with
    # no truncation of its own) — 4000 here was a self-imposed, much more
    # conservative ceiling that turned out too tight once brand_context
    # additions (brand_palette_clause) started eating into it: the fixed
    # directives plus ratio/palette clauses can now run ~2650-3300 chars on
    # their own, and the longest format prompt (Work In Progress, with its
    # representation block and real slot text filled in) already runs
    # ~1250 chars unmodified — that combination was landing at or past the
    # old ceiling even before any enrichment, silently truncating the very
    # "fill the frame, no empty background" instruction meant to prevent
    # exactly the empty-gap failure this library exists to avoid. Raised to
    # a still-conservative fraction of the real 32,000-char limit, not the
    # limit itself — no evidence justifies going further than this library
    # actually needs. Trim the caller's scene description, never the fixed
    # directives — GLOBAL_NEGATIVE_PROMPT in particular is what keeps
    # garbled text/logos out of a text-bearing ad format; that must never
    # be the part that gets cut for space. budget is floored at 0 (never
    # negative) as a second, independent guard on top of
    # brand_palette_clause's own description-length cap — a negative
    # Python slice silently reads from the string's end instead of
    # surfacing as the length problem it is.
    _DALLE_MAX_CHARS = 8000
    _SAFETY_MARGIN = 100
    ratio_clause = _resolve_ratio_clause(size)
    palette_clause = brand_palette_clause(brand_context)
    fixed_suffix = (
        f" {ratio_clause}"
        f"{' ' + palette_clause if palette_clause else ''}"
        f" {COMPOSITION_DIRECTIVE} {TYPOGRAPHY_DIRECTIVE} {GLOBAL_NEGATIVE_PROMPT}"
    )
    scene_description = prompt.strip()
    budget = max(0, _DALLE_MAX_CHARS - _SAFETY_MARGIN - len(fixed_suffix))
    if len(scene_description) > budget:
        scene_description = scene_description[:budget].rsplit(" ", 1)[0] + "."

    full_prompt = f"{scene_description}{fixed_suffix}"
    result = await ImageContentService._call_dalle_api(full_prompt, size=size, image_model="openai/gpt-image-2")
    if not result.get("success"):
        raise SceneGenerationFailed(f"Layer 2 generation failed: {result.get('error')}")

    image_url = result["url"]
    if image_url.startswith("data:"):
        from app.utils.cloudinary_upload import upload_base64
        try:
            image_url = await upload_base64(image_url, folder="uri-social/jane-ads/vsg01")
        except Exception as e:
            raise SceneGenerationFailed(f"Cloudinary upload failed: {e}") from e

    return image_url

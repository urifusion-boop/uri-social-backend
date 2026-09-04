"""
Review Card — VSG-01 v3 §2.1 (SEED-093).

"The business has a genuine written review. Default proof asset; cheapest
in the library." A real product photo occupies the top ~58% of the canvas;
a `field` block beneath holds the star row (only if a real rating exists),
the quote, and attribution.

Asset source: `upload_as_is` (as-is upload of a real product photo) or
`recomposite` (same real product, background replaced — the product itself
is never regenerated, §1.2). Either way, this module receives a finished
`product_image_url` and places it — whichever pipeline stage produced that
URL (a plain upload, or an upstream Layer-2/3 background recomposite) is
outside this module's concern, exactly like Receipt's `brand_logo_url`.
**No Layer 2 product generation** — §1.2 prohibits it; where no product
photograph exists, the caller must request one or pick a different format.
This is why FORMAT.requires includes PRODUCT_PHOTO (§6 retrieval gate):
this format must never even surface for a business with no real photo to
build it from.

Hard checks (§2.1):

1. "Star block renders only against a real rating on a real platform — a
   fabricated star graphic is a misleading representation and locally the
   exact signal that gets a business dismissed as fraudulent." Mechanically
   enforced: `star_rating` defaults to None (no star row at all), and when
   given must be an integer 1-5 — `InvalidStarRating` otherwise.

2. "Quote verbatim." Caller-side guarantee, same category as Receipt's
   "every figure real and currently honoured" — this module has no way to
   verify a quote against its source.

3. "No timed outcome claims in health, skin, weight or appearance
   (SEED-078)." Mechanically enforced, best-effort: `TimedOutcomeClaim` is
   raised when the quote pairs a time-duration phrase ("in 2 weeks",
   "after 10 days") with an appearance/health-outcome word ("skin",
   "weight", "kg", "acne", "wrinkles"...). Same defense-in-depth framing as
   Us vs Them's brand-name guard — a heuristic that refuses the obvious
   case, not a substitute for retrieval-time curation.
"""
import re
from typing import Dict, Optional, Tuple

from ._text_metrics import wrap_text
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS
from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService

FORMAT = AdFormatDef(
    format_id="SEED-093",
    name="Review Card",
    asset_source="upload",  # CreativeSource.UPLOAD ("upload_as_is"); recomposite is
                            # the alternative path when the photo needs cleanup —
                            # both hand this module a single finished product_image_url
    layers_used="L4",
    requires=["product_photo"],  # Requirement.PRODUCT_PHOTO.value — §1.2/§6: never
                                  # surface this format for a business with no real
                                  # product photo to build it from
)

_FONT_BODY = 44   # §1.6 floor
_FONT_STARS = 48
_FONT_ATTRIBUTION = 44


class InvalidStarRating(ValueError):
    """§2.1: a star row must reflect a real rating (1-5) or not exist at all
    — never a fabricated/out-of-range value that would misrepresent."""
    pass


class TimedOutcomeClaim(ValueError):
    """§2.1 / SEED-078: no timed outcome claims in health, skin, weight or
    appearance. Best-effort, defense-in-depth — see module docstring."""
    pass


_TIME_PHRASE = re.compile(
    r"\b(?:in|within|after)\s+(?:just\s+)?(?:a|one|two|three|four|five|six|seven|"
    r"\d+)\s*(?:day|days|week|weeks|month|months)\b",
    re.IGNORECASE,
)
_OUTCOME_WORD = re.compile(
    r"\b(?:skin|complexion|wrinkle|wrinkles|acne|glow|glowing|fair(?:er|ness)?|"
    r"weight|kg|kilos?|fat|slim(?:mer|ming)?|cellulite|stretch\s*marks?|hair\s*"
    r"growth|bleach(?:ed|ing)?)\b",
    re.IGNORECASE,
)


def _makes_a_timed_outcome_claim(quote: str) -> bool:
    return bool(_TIME_PHRASE.search(quote)) and bool(_OUTCOME_WORD.search(quote))


def build_document(
    product_image_url: str,
    quote: str,
    attribution: str,
    star_rating: Optional[int] = None,
    brand_logo_url: Optional[str] = None,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> Dict:
    """
    product_image_url: the real, already-final photo (uploaded as-is, or
    already recomposited upstream) — never a generated stand-in (§1.2).
    quote: the review text, 12-18 words per §2.1's composition guidance
    (caller responsibility, not enforced here — unlike the two hard checks
    below, word count isn't a truthfulness question). Rendered wrapped in
    curly quotation marks regardless of whether the caller's string already
    has them, so the presentation requirement doesn't depend on caller
    formatting.
    attribution: "first name plus initial" per §2.1, e.g. "Ngozi A." —
    caller-formatted, not reformatted here.
    star_rating: 1-5, or None to omit the star row entirely (no real rating
    given: §2.1's single strictest rule for this format).
    """
    if star_rating is not None and (not isinstance(star_rating, int) or not (1 <= star_rating <= 5)):
        raise InvalidStarRating(
            f"star_rating must be an integer 1-5 or None, got {star_rating!r}"
        )
    if _makes_a_timed_outcome_claim(quote):
        raise TimedOutcomeClaim(
            f"quote {quote!r} pairs a time phrase with a health/appearance outcome — "
            "not permitted regardless of source truthfulness (VSG-01 §2.1, SEED-078)"
        )

    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    layers = []
    z = 0

    padding = 56
    max_text_width = width - 2 * padding
    line_height = int(_FONT_BODY * 1.3)
    logo_height = 56

    # Measure everything the field block needs to hold *before* deciding
    # the product/field split — a flat 58%-of-canvas product zone with the
    # logo pinned to a fixed height-96 offset overlapped the attribution
    # line the moment a realistic (not synthetic-short) 18-word quote
    # wrapped to three lines, found by actually rendering one, not assumed
    # safe from a one-line test string.
    quote_lines = wrap_text(f"“{quote}”", max_text_width, _FONT_BODY)
    needed_field_height = (
        2 * padding
        + (_FONT_STARS + 32 if star_rating is not None else 0)
        + len(quote_lines) * line_height + 24
        + _FONT_ATTRIBUTION
        + (24 + logo_height if brand_logo_url else 0)
    )

    # §2.1: "subject occupying 55-60%." Size the field block to exactly what
    # its content needs, but keep the product zone within that stated
    # range — clamped rather than shrunk arbitrarily on either end.
    ideal_product_zone = height - needed_field_height
    product_zone_height = max(int(height * 0.55), min(int(height * 0.60), ideal_product_zone))

    z += 1
    layers.append({
        "type": "composited_product", "z_index": z,
        "url": product_image_url,
        "x": 0, "y": 0, "width": width, "height": product_zone_height,
    })

    field_y = product_zone_height
    field_height = height - product_zone_height
    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "rect",
        "x": 0, "y": field_y, "width": width, "height": field_height,
        "fill_color": t["field"],
    })

    content_x = padding
    content_y = field_y + padding

    if star_rating is not None:
        stars = "★" * star_rating + "☆" * (5 - star_rating)
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": stars,
            "x": content_x, "y": content_y,
            "font_size": _FONT_STARS, "color": t["accent"],
        })
        content_y += _FONT_STARS + 32

    z += 1
    layers.append({
        "type": "text", "z_index": z, "content": "\n".join(quote_lines),
        "x": content_x, "y": content_y,
        "font_size": _FONT_BODY, "color": t["ink"],
    })
    content_y += len(quote_lines) * line_height + 24

    z += 1
    layers.append({
        "type": "text", "z_index": z, "content": attribution,
        "x": content_x, "y": content_y,
        "font_size": _FONT_ATTRIBUTION, "color": t["ink-quiet"],
    })
    content_y += _FONT_ATTRIBUTION

    # Brand mark (§2.1: "position per Brand Overlay Spec" — a simple
    # placement here, not a final one, same caveat as Receipt's logo
    # reservation). Flows immediately beneath the attribution rather than
    # anchoring to a fixed canvas-bottom offset, precisely so it can never
    # land on top of content whose height varies with quote length.
    if brand_logo_url:
        content_y += 24
        z += 1
        layers.append({
            "type": "brand_asset", "z_index": z,
            "url": brand_logo_url,
            "x": content_x, "y": content_y, "width": 140, "height": logo_height,
        })

    return {
        "canvas": {"width": width, "height": height, "background_color": t["surface"]},
        "layers": layers,
    }


async def render(*args, **kwargs) -> bytes:
    document = build_document(*args, **kwargs)
    return await DocumentRendererService.render_to_png(document)

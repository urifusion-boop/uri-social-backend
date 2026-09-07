"""
Testimonial + Offer — VSG-01 v3 §2.2 (SEED-074).

"Proof and a live offer must land in one impression, typically at low
budget where a sequence is unaffordable." Proof occupies the upper
two-thirds and reads first; the offer sits in a distinct `field` band
beneath — different background weight, not a new paragraph — with price
or terms in `accent`. "Order is not negotiable: an offer read before
belief is heard as pressure."

Two permitted constructions (§1.2 — a generated face paired with a quote
implies a customer who does not exist):

1. `build_document`/`render` — a real customer photograph, permission on
   file, quote attributed to them. asset_source `upload_as_is`.
2. `build_document_no_person`/`render_no_person` — no person at all: a
   generated no-people scene (Layer 2, using §2.2's own verbatim prompt
   template) with the quote on a `field` block, and an optional real
   product shown alongside via `upload_as_is`.

Hard checks (§2.2):

1. "Customer permission on file before words, voice or image enter paid
   media." Enforced, not just documented: `permission_on_file` is a
   required, no-default boolean on the person path — `PermissionNotOnFile`
   if False, rather than an implicit yes a caller could silently omit.

2. "Offer half states delivery timing and payment options rather than a
   discount where possible (SEED-063)." "Where possible" — soft guidance,
   caller-side, same category as Receipt's "every figure real and
   honoured."

3. "Every promise confirmed by the user." Caller-side guarantee.

4. The proof/offer split and its ordering are structural, not caller-
   configurable — always upper two-thirds proof, lower third offer, never
   the reverse. Same lesson as Problem/Solution's TextOverflowsScrim:
   measuring content first and raising (ContentOverflowsZone) rather than
   silently letting quote or offer copy spill past its allotted zone.
"""
from typing import Dict, Optional, Tuple

from ..visual_slots import resolve_nigerian_setting
from ..layer2_generation import generate_scene
from .legibility import assert_legible
from ._text_metrics import wrap_text
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS
from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService

FORMAT = AdFormatDef(
    format_id="SEED-074",
    name="Testimonial + Offer",
    asset_source="upload",  # upload_as_is for the person path (§2.2's stated primary
                            # construction); the no-person path uses `generate` for a
                            # people-free scene instead — see build_document_no_person
    layers_used="L4",
    requires=["real_customer_photo"],  # Requirement.REAL_CUSTOMER_PHOTO — §1.2: a
                                        # generated face paired with a quote implies a
                                        # customer who does not exist
)

_FONT_QUOTE = 44
_FONT_ATTRIBUTION = 44
_FONT_OFFER = 48
_FONT_PRICE = 52
_PADDING = 56
_LINE_HEIGHT = int(_FONT_QUOTE * 1.3)


class PermissionNotOnFile(ValueError):
    """§2.2: 'Customer permission on file before words, voice or image
    enter paid media.' A required, no-default parameter rather than an
    implicit yes — see module docstring."""
    pass


class ContentOverflowsZone(ValueError):
    """The proof/offer split is structural (upper two-thirds / lower
    third), not caller-configurable — copy that would spill past its
    allotted zone is rejected rather than silently rendered over the
    boundary, the same lesson Problem/Solution's TextOverflowsScrim
    already applies."""
    pass


def _quote_block_height(quote_lines, has_attribution: bool) -> int:
    height = 2 * _PADDING + len(quote_lines) * _LINE_HEIGHT
    if has_attribution:
        height += 24 + _FONT_ATTRIBUTION
    return height


def _check_proof_zone_fits(quote_block_height: int, proof_zone_height: int) -> None:
    if quote_block_height >= proof_zone_height:
        raise ContentOverflowsZone(
            f"quote block needs {quote_block_height}px, leaving no room for the photo/"
            f"scene within the proof zone ({proof_zone_height}px, §2.2: upper two-thirds) "
            "— shorten the quote"
        )


def _check_offer_zone_fits(offer_content_height: int, offer_zone_height: int) -> None:
    if offer_content_height > offer_zone_height:
        raise ContentOverflowsZone(
            f"offer content needs {offer_content_height}px, taller than the offer zone "
            f"({offer_zone_height}px) — shorten the offer text/price"
        )


def _offer_block_lines(offer_text: str, price_or_terms: Optional[str], max_width: int):
    offer_lines = wrap_text(offer_text, max_width, _FONT_OFFER, 700)
    price_lines = wrap_text(price_or_terms, max_width, _FONT_PRICE, 700) if price_or_terms else []
    return offer_lines, price_lines


def build_document(
    customer_photo_url: str,
    quote: str,
    attribution: str,
    offer_text: str,
    permission_on_file: bool,
    price_or_terms: Optional[str] = None,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> Dict:
    """
    customer_photo_url: a real, permission-cleared photograph of the
    actual customer being quoted — never generated (§1.2).
    quote / attribution: caller-formatted, rendered wrapped in curly
    quotation marks regardless of the caller's own formatting.
    """
    if not permission_on_file:
        raise PermissionNotOnFile(
            "customer permission must be on file before this renders (§2.2) — "
            "pass permission_on_file=True only once that is actually confirmed"
        )

    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    max_text_width = width - 2 * _PADDING

    proof_zone_height = (height * 2) // 3
    offer_zone_height = height - proof_zone_height

    quote_lines = wrap_text(f"“{quote}”", max_text_width, _FONT_QUOTE)
    quote_block_height = _quote_block_height(quote_lines, bool(attribution))
    _check_proof_zone_fits(quote_block_height, proof_zone_height)
    photo_height = proof_zone_height - quote_block_height

    offer_lines, price_lines = _offer_block_lines(offer_text, price_or_terms, max_text_width)
    offer_line_height = int(_FONT_OFFER * 1.3)
    price_line_height = int(_FONT_PRICE * 1.3)
    offer_content_height = (
        2 * _PADDING
        + len(offer_lines) * offer_line_height
        + (24 + len(price_lines) * price_line_height if price_lines else 0)
    )
    _check_offer_zone_fits(offer_content_height, offer_zone_height)

    layers = []
    z = 0

    z += 1
    layers.append({
        "type": "composited_product", "z_index": z,
        "url": customer_photo_url, "x": 0, "y": 0, "width": width, "height": photo_height,
    })

    quote_y = photo_height
    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "rect",
        "x": 0, "y": quote_y, "width": width, "height": quote_block_height,
        "fill_color": t["field"],
    })
    content_y = quote_y + _PADDING
    z += 1
    layers.append({
        "type": "text", "z_index": z, "content": "\n".join(quote_lines),
        "x": _PADDING, "y": content_y, "font_size": _FONT_QUOTE, "color": t["ink"],
    })
    content_y += len(quote_lines) * _LINE_HEIGHT + 24
    if attribution:
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": attribution,
            "x": _PADDING, "y": content_y, "font_size": _FONT_ATTRIBUTION, "color": t["ink-quiet"],
        })

    # Offer band — lower third, `field`, offer copy in `ink`, price/terms
    # in `accent`. Order fixed: this is always the LAST thing on the
    # canvas, never before the proof. Immediately adjacent to the quote
    # block above (also `field`) — §2.2 is explicit that the offer must
    # read as a different background weight, "not merely a new paragraph,"
    # so a divider line separates the two rather than leaning on spacing
    # alone (same fix as build_document_no_person).
    offer_y = proof_zone_height
    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "rect",
        "x": 0, "y": offer_y, "width": width, "height": offer_zone_height,
        "fill_color": t["field"],
    })
    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "line",
        "x1": 0, "y1": offer_y, "x2": width, "y2": offer_y,
        "color": t["edge"], "stroke_width": 2,
    })
    offer_content_y = offer_y + _PADDING
    z += 1
    layers.append({
        "type": "text", "z_index": z, "content": "\n".join(offer_lines),
        "x": _PADDING, "y": offer_content_y, "font_size": _FONT_OFFER, "font_weight": 700, "color": t["ink"],
    })
    if price_lines:
        offer_content_y += len(offer_lines) * offer_line_height + 24
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": "\n".join(price_lines),
            "x": _PADDING, "y": offer_content_y, "font_size": _FONT_PRICE, "font_weight": 700, "color": t["accent"],
        })

    document = {
        "canvas": {"width": width, "height": height, "background_color": t["surface"]},
        "layers": layers,
    }
    assert_legible(document, t)
    return document


def build_document_no_person(
    scene_image_url: str,
    quote: str,
    offer_text: str,
    price_or_terms: Optional[str] = None,
    product_image_url: Optional[str] = None,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> Dict:
    """
    No-person construction (§1.2, §2.2): scene_image_url is a generated
    no-people scene (see render_no_person/_scene_prompt for the Layer 2
    prompt), never a person. quote sits on a `field` block, matching
    §2.2's own wording exactly. product_image_url, if given, is a real
    product photo (upload_as_is) composited alongside — never generated.
    """
    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    max_text_width = width - 2 * _PADDING

    proof_zone_height = (height * 2) // 3
    offer_zone_height = height - proof_zone_height

    quote_lines = wrap_text(f"“{quote}”", max_text_width, _FONT_QUOTE)
    quote_block_height = _quote_block_height(quote_lines, has_attribution=False)
    _check_proof_zone_fits(quote_block_height, proof_zone_height)

    offer_lines, price_lines = _offer_block_lines(offer_text, price_or_terms, max_text_width)
    offer_line_height = int(_FONT_OFFER * 1.3)
    price_line_height = int(_FONT_PRICE * 1.3)
    offer_content_height = (
        2 * _PADDING
        + len(offer_lines) * offer_line_height
        + (24 + len(price_lines) * price_line_height if price_lines else 0)
    )
    _check_offer_zone_fits(offer_content_height, offer_zone_height)

    layers = []
    z = 0

    # Scene fills the whole proof zone; its own Layer 2 prompt reserves a
    # clear lower-third band (of the proof zone) for the quote — a solid
    # `field` scrim sits there, same reasoning as Problem/Solution: text
    # over photography needs a solid plate, never a floating overlay.
    z += 1
    layers.append({
        "type": "ai_generated_background", "z_index": z,
        "url": scene_image_url, "x": 0, "y": 0, "width": width, "height": proof_zone_height,
    })

    if product_image_url:
        product_size = int(width * 0.32)
        z += 1
        layers.append({
            "type": "composited_product", "z_index": z,
            "url": product_image_url,
            "x": width - product_size - _PADDING, "y": _PADDING,
            "width": product_size, "height": product_size,
        })

    quote_y = proof_zone_height - quote_block_height
    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "rect",
        "x": 0, "y": quote_y, "width": width, "height": quote_block_height,
        "fill_color": t["field"],
    })
    z += 1
    layers.append({
        "type": "text", "z_index": z, "content": "\n".join(quote_lines),
        "x": _PADDING, "y": quote_y + _PADDING, "font_size": _FONT_QUOTE, "color": t["ink"],
    })

    # Offer band — also `field` (matching the person path's single-field
    # convention), but immediately adjacent to the quote block above (also
    # `field`), so a divider line keeps the two readable as separate
    # sections rather than one blended block (same reasoning as Us vs
    # Them's/Day 1 → Day 30's dividers between same-coloured sections).
    offer_y = proof_zone_height
    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "rect",
        "x": 0, "y": offer_y, "width": width, "height": offer_zone_height,
        "fill_color": t["field"],
    })
    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "line",
        "x1": 0, "y1": offer_y, "x2": width, "y2": offer_y,
        "color": t["edge"], "stroke_width": 2,
    })
    offer_content_y = offer_y + _PADDING
    z += 1
    layers.append({
        "type": "text", "z_index": z, "content": "\n".join(offer_lines),
        "x": _PADDING, "y": offer_content_y, "font_size": _FONT_OFFER, "font_weight": 700, "color": t["ink"],
    })
    if price_lines:
        offer_content_y += len(offer_lines) * offer_line_height + 24
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": "\n".join(price_lines),
            "x": _PADDING, "y": offer_content_y, "font_size": _FONT_PRICE, "font_weight": 700, "color": t["accent"],
        })

    document = {
        "canvas": {"width": width, "height": height, "background_color": t["surface"]},
        "layers": layers,
    }
    assert_legible(document, t)
    return document


def _scene_prompt(nigerian_setting: str) -> str:
    return (
        f"{resolve_nigerian_setting(nigerian_setting)}, no people in frame, strong "
        "equatorial daylight, authentic documentary style, slight imperfection, not "
        "studio lit, clear empty space in the lower third, shot on a phone camera, "
        "natural colour"
    )


async def render(
    customer_photo_url: str,
    quote: str,
    attribution: str,
    offer_text: str,
    permission_on_file: bool,
    price_or_terms: Optional[str] = None,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> bytes:
    document = build_document(
        customer_photo_url, quote, attribution, offer_text,
        permission_on_file, price_or_terms, canvas_size, tokens,
    )
    return await DocumentRendererService.render_to_png(document)


async def render_no_person(
    nigerian_setting: str,
    quote: str,
    offer_text: str,
    price_or_terms: Optional[str] = None,
    product_image_url: Optional[str] = None,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> bytes:
    """Real Layer 2 generation for the no-people scene, then Layer 4
    template-fill — mirrors problem_solution.render()'s pattern."""
    width, height = canvas_size
    proof_zone_height = (height * 2) // 3
    scene_url = await generate_scene(_scene_prompt(nigerian_setting), size=f"{width}x{proof_zone_height}")

    document = build_document_no_person(
        scene_url, quote, offer_text, price_or_terms, product_image_url, canvas_size, tokens,
    )
    return await DocumentRendererService.render_to_png(document)

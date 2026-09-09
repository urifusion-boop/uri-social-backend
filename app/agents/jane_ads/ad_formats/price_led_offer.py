"""
Price-Led Offer — VSG-01-PROMPTS v2 §6.13.

"Probably the most common Nigerian SME ad and absent from v1." A real product
photo occupies the upper two-thirds; price is the largest element after the
product, in `accent`; delivery area and payment method sit below in `ink`;
an action line closes it out. The Receipt covers itemised quotes — this
covers the everyday single-item version.

Asset source: `upload_as_is` (as-is upload of a real product photo) or
`recomposite` (same real product, background replaced) — mirrors Review
Card exactly; no Layer 2 product generation (§1.2), a caller with no real
photo must request one or pick a different format.

Hard checks (§6.13, caller-side guarantees this module cannot itself verify
against reality): price must be current and genuinely honoured; delivery
area and payment method must be user-confirmed before this enters live
creative; a struck-through "was" price must have been genuinely charged.
This module's own mechanical guarantee is narrower — the `was_price` line,
if given, is only ever rendered struck through and secondary to the real
price, never presented as equal or larger.
"""
from typing import Dict, Optional, Tuple

from ._text_metrics import text_width, wrap_text
from .legibility import assert_legible
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS, logo_badge_layers

FORMAT = AdFormatDef(
    format_id="SEED-096",
    name="Price-Led Offer",
    asset_source="upload",  # CreativeSource.UPLOAD ("upload_as_is"); recomposite is
                            # the alternative when the photo needs cleanup — see
                            # Review Card's own module docstring for the same split
    layers_used="L4",
    brand_mark="required",  # VSG-01-PROMPTS v2 §6.13/§7
    requires=["product_photo"],
)

_FONT_PRICE = 88
_FONT_WAS_PRICE = 44
_FONT_LINE = 44
_FONT_ACTION = 44
_PADDING = 56


def build_document(
    product_image_url: str,
    price: str,
    delivery_line: Optional[str] = None,
    payment_line: Optional[str] = None,
    action_line: Optional[str] = None,
    was_price: Optional[str] = None,
    brand_logo_url: str = None,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> Dict:
    """
    product_image_url: the real, already-final photo (uploaded as-is, or
    already recomposited upstream) — never a generated stand-in (§1.2).
    price: caller-formatted (naira sign, thousands separators) — this module
    does no currency formatting, same contract as Receipt.
    was_price: a genuinely-charged prior price, rendered smaller and struck
    through beside the real price — omit entirely rather than pass a
    fabricated "discount".
    """
    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    max_text_width = width - 2 * _PADDING

    # Same "measure the field block before deciding the photo split" logic
    # Review Card already uses — a fixed product-zone height silently
    # overlapped the field content the moment a real delivery+payment+action
    # combination ran longer than a one-line test string.
    delivery_lines = wrap_text(delivery_line, max_text_width, _FONT_LINE) if delivery_line else []
    payment_lines = wrap_text(payment_line, max_text_width, _FONT_LINE) if payment_line else []
    action_lines = wrap_text(action_line, max_text_width, _FONT_ACTION, 700) if action_line else []
    line_height = int(_FONT_LINE * 1.3)

    # The logo (if any) is a bottom-right floating badge added after this
    # layout is decided — see logo_badge_layers below — so it doesn't factor
    # into the field's own content height.
    needed_field_height = (
        2 * _PADDING
        + _FONT_PRICE + 16
        + len(delivery_lines) * line_height
        + len(payment_lines) * line_height
        + (24 + len(action_lines) * int(_FONT_ACTION * 1.3) if action_lines else 0)
    )
    ideal_product_zone = height - needed_field_height
    product_zone_height = max(int(height * 0.58), min(int(height * 0.66), ideal_product_zone))
    field_height = height - product_zone_height
    # Centre the actual content within whatever field height results, rather
    # than padding from the top only — the 58-66% clamp above is a safety
    # range, not a promise that needed_field_height lands exactly on it, and
    # anchoring at a fixed top offset left real dead space in the field zone
    # whenever the clamp gave more room than a short offer actually needed
    # (found by rendering one, not assumed safe from the formula alone).
    content_block_height = needed_field_height - 2 * _PADDING
    top_pad = max(_PADDING, (field_height - content_block_height) // 2)

    layers = []
    z = 0

    z += 1
    layers.append({
        "type": "composited_product", "z_index": z,
        "url": product_image_url, "x": 0, "y": 0, "width": width, "height": product_zone_height,
    })

    field_y = product_zone_height
    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "rect",
        "x": 0, "y": field_y, "width": width, "height": field_height,
        "fill_color": t["field"],
    })

    content_y = field_y + top_pad

    # Price row — the largest element after the product itself (§6.13). A
    # genuine "was" price sits beside it, smaller, in ink-quiet, with a
    # single horizontal strike drawn across its own measured width — never
    # rendered at equal size/weight to the real price.
    z += 1
    layers.append({
        "type": "text", "z_index": z, "content": price,
        "x": _PADDING, "y": content_y, "font_size": _FONT_PRICE, "font_weight": 700, "color": t["accent"],
    })
    if was_price:
        was_x = _PADDING + text_width(price, _FONT_PRICE, 700) + 24
        was_y = content_y + (_FONT_PRICE - _FONT_WAS_PRICE)
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": was_price,
            "x": was_x, "y": was_y, "font_size": _FONT_WAS_PRICE, "color": t["ink-quiet"],
        })
        was_w = text_width(was_price, _FONT_WAS_PRICE)
        strike_y = was_y + _FONT_WAS_PRICE // 2
        z += 1
        layers.append({
            "type": "shape", "z_index": z, "shape": "line",
            "x1": was_x, "y1": strike_y, "x2": was_x + was_w, "y2": strike_y,
            "color": t["ink-quiet"], "stroke_width": 3,
        })
    content_y += _FONT_PRICE + 16

    for lines in (delivery_lines, payment_lines):
        if not lines:
            continue
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": "\n".join(lines),
            "x": _PADDING, "y": content_y, "font_size": _FONT_LINE, "color": t["ink"],
        })
        content_y += len(lines) * line_height

    if action_lines:
        content_y += 24
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": "\n".join(action_lines),
            "x": _PADDING, "y": content_y, "font_size": _FONT_ACTION, "font_weight": 700, "color": t["ink"],
        })

    badge_layers, z = logo_badge_layers(brand_logo_url, width, height, z)
    layers.extend(badge_layers)

    document = {
        "canvas": {"width": width, "height": height, "background_color": t["surface"]},
        "layers": layers,
    }
    assert_legible(document, t)
    return document


async def render(*args, **kwargs) -> bytes:
    """Build + render in one call — the only entry point most callers need."""
    from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService
    document = build_document(*args, **kwargs)
    return await DocumentRendererService.render_to_png(document)

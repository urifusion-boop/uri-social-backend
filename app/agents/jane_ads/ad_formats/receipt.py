"""
The Receipt — VSG-01 v3 §2.5 (SEED-081).

"The offer has separable components and price transparency is an
advantage. Strongest fit in this library for the local market — it answers
'how much?' before it is asked."

Composition: item names left-aligned, naira amounts right-aligned, a
dotted `edge` leader between each pair, a rule above the total, total in
`accent`. Delivery/payment lines below. Brand mark reserved at the head
(composited separately via a real brand_asset layer — logo compositing
belongs to whichever brand actually owns one, not fabricated here).

Hard checks (§2.5, enforced by the caller building `items`/`delivery_line`/
`payment_line`, not by this module): must never resemble a bank transfer
alert or payment confirmation — no bank names, account numbers, reference
codes, success ticks, or "transaction successful" language. This is the
seller's own itemised quotation, clearly branded. Every figure real and
currently honoured.
"""
from typing import Dict, List, Optional, Tuple

from ._text_metrics import wrap_text
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS
from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService

FORMAT = AdFormatDef(
    format_id="SEED-081",
    name="The Receipt",
    asset_source="drawn",  # no photography at all — pure Layer 4 composite
    layers_used="L4",
    brand_mark="required",  # VSG-01-PROMPTS v2 §6.5 — load-bearing: distinguishes a
                             # seller's quotation from a payment/transfer alert
    requires=[],  # nothing to gate — a drawn format needs no photo of anything
)

# §1.6 floors — retrofitted after legibility.py's automated check found the
# original 24-40px sizes here all below the 42px minimum (this format
# predates that discipline). Item names were previously unbounded/
# unmeasured single-line text; at the old 32px that mostly got away with
# it, but bumping to 44px makes a realistic item name run straight into
# the leader line/price column, so item names now wrap too (wrap_text,
# the same helper Borrowed Interface/Review Card/Us vs Them use).
_FONT_BUSINESS_NAME = 48
_FONT_ITEM = 44
_FONT_TOTAL_LABEL = 44
_FONT_TOTAL_AMOUNT = 48
_FONT_FOOTER = 44


def build_document(
    items: List[Tuple[str, str]],
    total_label: str,
    total_amount: str,
    business_name: str = "",
    delivery_line: Optional[str] = None,
    payment_line: Optional[str] = None,
    brand_logo_url: Optional[str] = None,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> Dict:
    """
    items: [(name, formatted_price), ...] — price already formatted with the
    naira sign/thousands separators by the caller (this module does no
    currency formatting — every figure must be real and currently honoured
    per §2.5, which is a caller-side guarantee, not something derivable
    here).
    """
    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size

    layers = []
    z = 0

    # Space consumed before the first item row starts, relative to the card's
    # own content origin (content_top below) — NOT an absolute canvas
    # position. Must match the header_offset used at row_y's actual layout
    # further down, or the pre-measured card height and the real content
    # drift apart (undersized card, or the extra empty space this redesign
    # exists to remove creeping back in).
    header_space = 100 if (brand_logo_url or business_name) else 24
    name_col_max_width = 348 - 20  # gap before the leader line starts
    item_line_height = int(_FONT_ITEM * 1.3)
    single_line_row_height = 88

    # Pre-measure the whole receipt (header + items + total + footer) so it
    # can sit inside a real "paper" card, centred on the canvas, instead of
    # floating text pinned to a fixed y=72 — the pre-card version left the
    # bottom third of a 1080px canvas empty for a typical 2-3 item order and
    # had no visual boundary distinguishing "the receipt" from the ad's own
    # background at all.
    row_heights = []
    for name, _price in items:
        name_lines = wrap_text(name, name_col_max_width, _FONT_ITEM)
        row_heights.append(max(single_line_row_height, len(name_lines) * item_line_height + 30))
    footer_lines_count = sum(1 for line in (delivery_line, payment_line) if line)
    content_height = (
        header_space + sum(row_heights) + 8  # items + rule gap
        + 24 + _FONT_TOTAL_AMOUNT  # total row
        + (footer_lines_count * 64 if footer_lines_count else 0)
    )
    card_pad = 56
    card_h = content_height + 2 * card_pad
    card_w = width - 96
    card_x = (width - card_w) // 2
    card_y = max(48, (height - card_h) // 2)

    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "rounded_rect",
        "x": card_x, "y": card_y, "width": card_w, "height": card_h,
        "corner_radius": 24, "fill_color": t["field"],
    })

    content_left = card_x + card_pad
    price_column_x = card_x + card_w - card_pad

    # Reserve the head for a brand mark — composited only if a real logo URL
    # is given; VSG-01 §1.5 defers actual position/size/treatment to the
    # Brand Overlay Spec, so this is a simple top-left placement, not a
    # final one.
    content_top = card_y + card_pad
    if brand_logo_url:
        z += 1
        layers.append({
            "type": "brand_asset", "z_index": z,
            "url": brand_logo_url, "x": content_left, "y": content_top, "width": 160, "height": 64,
        })
    elif business_name:
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": business_name,
            "x": content_left, "y": content_top, "font_size": _FONT_BUSINESS_NAME, "font_weight": 700,
            "color": t["ink"],
        })

    # Item rows: name left (wrapped — an unbounded single line at 44px runs
    # straight into the leader/price zone the moment a name is realistic
    # rather than a short test string), dotted leader, price right-aligned
    # at a fixed column so every price lines up regardless of name length.
    row_y = content_top + header_space
    for name, price in items:
        name_lines = wrap_text(name, name_col_max_width, _FONT_ITEM)
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": "\n".join(name_lines),
            "x": content_left, "y": row_y, "font_size": _FONT_ITEM, "color": t["ink"],
        })
        # Leader + price stay pinned to the name's first line, same as a
        # real POS receipt where the price sits beside the top line of a
        # wrapped item description. Exact leader end points are still
        # approximate at placeholder-font-metrics precision — decorative,
        # not load-bearing.
        z += 1
        layers.append({
            "type": "shape", "z_index": z, "shape": "line",
            "x1": content_left + 348, "y1": row_y + 26, "x2": price_column_x - 90, "y2": row_y + 26,
            "color": t["edge"], "stroke_width": 2, "dashed": True,
            "dash_length": 5, "gap_length": 6,
        })
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": price,
            "x": price_column_x, "y": row_y, "font_size": _FONT_ITEM, "color": t["ink"],
            "text_align": "ra",
        })
        row_height = max(single_line_row_height, len(name_lines) * item_line_height + 30)
        row_y += row_height

    # Rule above the total.
    rule_y = row_y + 8
    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "line",
        "x1": content_left, "y1": rule_y, "x2": price_column_x, "y2": rule_y,
        "color": t["ink"], "stroke_width": 3,
    })

    # Total row — larger, in accent.
    total_y = rule_y + 24
    z += 1
    layers.append({
        "type": "text", "z_index": z, "content": total_label,
        "x": content_left, "y": total_y, "font_size": _FONT_TOTAL_LABEL, "font_weight": 700, "color": t["ink"],
    })
    z += 1
    layers.append({
        "type": "text", "z_index": z, "content": total_amount,
        "x": price_column_x, "y": total_y, "font_size": _FONT_TOTAL_AMOUNT, "font_weight": 700,
        "color": t["accent"], "text_align": "ra",
    })

    # Delivery / payment lines — ink-quiet, below the total.
    footer_y = total_y + 88
    for line in (delivery_line, payment_line):
        if not line:
            continue
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": line,
            "x": content_left, "y": footer_y, "font_size": _FONT_FOOTER, "color": t["ink-quiet"],
        })
        footer_y += 64

    return {
        "canvas": {"width": width, "height": height, "background_color": t["surface"]},
        "layers": layers,
    }


async def render(*args, **kwargs) -> bytes:
    """Build + render in one call — the only entry point most callers need."""
    document = build_document(*args, **kwargs)
    return await DocumentRendererService.render_to_png(document)

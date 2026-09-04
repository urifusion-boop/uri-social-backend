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

from .tokens import AdFormatDef, PLACEHOLDER_TOKENS
from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService

FORMAT = AdFormatDef(
    format_id="SEED-081",
    name="The Receipt",
    asset_source="drawn",  # no photography at all — pure Layer 4 composite
    layers_used="L4",
    requires=[],  # nothing to gate — a drawn format needs no photo of anything
)


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

    # Reserve the head for a brand mark — composited only if a real logo URL
    # is given; VSG-01 §1.5 defers actual position/size/treatment to the
    # Brand Overlay Spec, so this is a simple top-left placement, not a
    # final one.
    content_top = 72
    if brand_logo_url:
        z += 1
        layers.append({
            "type": "brand_asset", "z_index": z,
            "url": brand_logo_url, "x": 72, "y": 56, "width": 160, "height": 64,
        })
        content_top = 148
    elif business_name:
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": business_name,
            "x": 72, "y": 64, "font_size": 40, "font_weight": 700, "color": t["ink"],
        })
        content_top = 140

    # Item rows: name left, dotted leader, price right-aligned at a fixed
    # column so every price lines up regardless of name length.
    price_column_x = width - 72
    row_height = 64
    row_y = content_top + 24
    for name, price in items:
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": name,
            "x": 72, "y": row_y, "font_size": 32, "color": t["ink"],
        })
        # Leader runs from just after the name to just before the price —
        # exact end points are approximate at placeholder-font-metrics
        # precision (no live text-width measurement here); a real font pass
        # can tighten this, but the leader is decorative, not load-bearing.
        z += 1
        layers.append({
            "type": "shape", "z_index": z, "shape": "line",
            "x1": 420, "y1": row_y + 22, "x2": price_column_x - 90, "y2": row_y + 22,
            "color": t["edge"], "stroke_width": 2, "dashed": True,
            "dash_length": 5, "gap_length": 6,
        })
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": price,
            "x": price_column_x, "y": row_y, "font_size": 32, "color": t["ink"],
            "text_align": "ra",
        })
        row_y += row_height

    # Rule above the total.
    rule_y = row_y + 8
    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "line",
        "x1": 72, "y1": rule_y, "x2": width - 72, "y2": rule_y,
        "color": t["ink"], "stroke_width": 3,
    })

    # Total row — larger, in accent.
    total_y = rule_y + 24
    z += 1
    layers.append({
        "type": "text", "z_index": z, "content": total_label,
        "x": 72, "y": total_y, "font_size": 38, "font_weight": 700, "color": t["ink"],
    })
    z += 1
    layers.append({
        "type": "text", "z_index": z, "content": total_amount,
        "x": price_column_x, "y": total_y, "font_size": 40, "font_weight": 700,
        "color": t["accent"], "text_align": "ra",
    })

    # Delivery / payment lines — small, ink-quiet, below the total.
    footer_y = total_y + 72
    for line in (delivery_line, payment_line):
        if not line:
            continue
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": line,
            "x": 72, "y": footer_y, "font_size": 24, "color": t["ink-quiet"],
        })
        footer_y += 36

    return {
        "canvas": {"width": width, "height": height, "background_color": t["surface"]},
        "layers": layers,
    }


async def render(*args, **kwargs) -> bytes:
    """Build + render in one call — the only entry point most callers need."""
    document = build_document(*args, **kwargs)
    return await DocumentRendererService.render_to_png(document)

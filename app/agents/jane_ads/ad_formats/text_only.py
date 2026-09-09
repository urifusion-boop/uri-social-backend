"""
Text-Only — VSG-01-PROMPTS v2 §6.14.

"No image at all. Cheapest asset in the library, always legible under
compression, and the only format available to a business with zero
photographs." One line of `ink` on a `surface` field, occupying the middle
half of the canvas; an optional short second line in `ink-quiet`; a
price/action line in `accent` near the base. Nothing else in frame.

Asset source: `drawn` (L4 only) — no generation prompt, matching Us vs
Them/Receipt/Borrowed Interface.

Hard checks (§6.14): the headline must carry real information — a price, a
delivery area, a specific offer, a real fact — never sentiment alone;
abstraction fails harder here than anywhere else in the library because
there is nothing else on the canvas to carry the ad. Mechanically enforced
via the same §1.6 legibility floor every format in this package already
uses (minimum size, 7:1 contrast) — the "carries real information" rule
itself is a caller-side guarantee, not something derivable from a string.
"""
from typing import Dict, Optional, Tuple

from ._text_metrics import wrap_text
from .legibility import assert_legible
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS, logo_badge_layers

FORMAT = AdFormatDef(
    format_id="SEED-097",
    name="Text-Only",
    asset_source="drawn",
    layers_used="L4",
    brand_mark="required",  # VSG-01-PROMPTS v2 §6.14/§7
    requires=[],  # a drawn format needs no photo of anything
)

# §1.6's own floor is 42px; §6.14 states a stricter 72px minimum specifically
# for this format ("highest legibility burden in the library") since there's
# no image to carry any of the weight if the type fails.
_FONT_HEADLINE = 80
_FONT_SUBLINE = 44
_FONT_ACTION = 56
_PADDING = 88


def build_document(
    headline: str,
    subline: Optional[str] = None,
    action_line: Optional[str] = None,
    brand_logo_url: str = None,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> Dict:
    """
    headline: the one real fact this ad exists to say — a price, a delivery
    area, a specific offer. Rendered centred in the middle half of the
    canvas (§6.14's own composition instruction), never the full height, so
    it reads as a considered statement rather than a filled-in placeholder.
    subline / action_line: optional supporting line and closing price/action
    — both genuinely optional per §6.14; omitting either is a normal,
    correct use of this format, not a partial one.
    """
    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    max_text_width = width - 2 * _PADDING

    headline_lines = wrap_text(headline, max_text_width, _FONT_HEADLINE, 700)
    subline_lines = wrap_text(subline, max_text_width, _FONT_SUBLINE) if subline else []
    action_lines = wrap_text(action_line, max_text_width, _FONT_ACTION, 700) if action_line else []

    headline_line_h = int(_FONT_HEADLINE * 1.25)
    subline_line_h = int(_FONT_SUBLINE * 1.3)
    action_line_h = int(_FONT_ACTION * 1.3)

    block_height = (
        len(headline_lines) * headline_line_h
        + (24 + len(subline_lines) * subline_line_h if subline_lines else 0)
        + (48 + len(action_lines) * action_line_h if action_lines else 0)
    )
    block_top = (height - block_height) // 2

    layers = []
    z = 0

    y = block_top
    z += 1
    layers.append({
        "type": "text", "z_index": z, "content": "\n".join(headline_lines),
        "x": width // 2, "y": y, "font_size": _FONT_HEADLINE, "font_weight": 700, "color": t["ink"],
        "text_align": "ma",
    })
    y += len(headline_lines) * headline_line_h

    if subline_lines:
        y += 24
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": "\n".join(subline_lines),
            "x": width // 2, "y": y, "font_size": _FONT_SUBLINE, "color": t["ink-quiet"],
            "text_align": "ma",
        })
        y += len(subline_lines) * subline_line_h

    if action_lines:
        y += 48
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": "\n".join(action_lines),
            "x": width // 2, "y": y, "font_size": _FONT_ACTION, "font_weight": 700, "color": t["accent"],
            "text_align": "ma",
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

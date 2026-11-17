"""
Offer Spotlight — a real discount or deal on a lifestyle photo (SEED-100).

Built after the "Get 50% off all of our creative services" reference: a warm, everyday photo
fills the frame; the business's services sit as bright tags down one side; a tilted ticket
carries the offer; the headline sits at the bottom on a dark wash with the offer itself
("50%") in the brand colour.

Asset source `generate`: the photo shows a situation (someone relaxed, on their phone), not
the product and not a customer — the same rule Problem / Solution uses (§1.2). The offer, the
services and the numbers are always the business's own words; the orchestrator only fills
this format when the brief states a real offer.
"""
from typing import Dict, List, Optional, Tuple

from ._type import adjust_to_contrast, block_with_text, emphasis_spans, fit_headline, lines_with_emphasis
from .brand_tokens import contrast_ratio, darken_to_contrast
from .legibility import assert_legible
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS
from ..layer2_generation import REPRESENTATION_BLOCK

FORMAT = AdFormatDef(
    format_id="SEED-100",
    name="Offer Spotlight",
    asset_source="generate",
    layers_used="L2-L4",
    brand_mark="optional",
    requires=[],
)

DEFAULT_FAMILY = "Poppins"
BODY_FAMILY = "Inter"
PAD = 60
WASH = "#0B0B0F"

# Tag colours after the reference's bright stack. The brand colour leads; the rest are fixed,
# saturated colours, each deepened (same hue) until white text clears the 7:1 floor.
_TAG_COLOURS = ["#D81B60", "#1E40AF", "#C2410C", "#047857", "#B91C1C", "#6D28D9"]


def scene_prompt(scene: str) -> str:
    # REPRESENTATION_BLOCK is the same §1.7 representation instruction every other format's
    # photo prompt carries; without it the model drew lighter skin and the skin-tone check
    # rejected both attempts (seen on the first staging run).
    return (
        f"A warm, natural lifestyle photograph of a real Nigerian scene: {scene}. "
        f"{REPRESENTATION_BLOCK}. One person, relaxed and genuinely "
        "smiling, sitting comfortably indoors in soft daylight — think a living room with a "
        "plant and a sofa. The person sits in the centre-right of the frame, from the knees up. "
        "Keep the left third of the frame and the bottom third calm and uncluttered (text and "
        "tags are placed there). Editorial advertising quality, real skin texture, no text, "
        "no logos, no devices showing readable screens."
    )


def build_document(
    scene_url: str,
    headline: str,
    emphasis: Optional[str],
    ticket_text: Optional[str],
    services: List[str],
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
    brand_logo_url: Optional[str] = None,
    headline_font: str = DEFAULT_FAMILY,
    brand_color: Optional[str] = None,
) -> Dict:
    """
    headline: "Get 50% off all of our creative services".
    emphasis: the part of the headline shown in the brand colour ("50%") — must appear in it.
    ticket_text: the tilted ticket ("50% off all services"); omitted when None.
    services: up to 5 short tags ("Graphic design", "Video editing").
    """
    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    brand = brand_color or t["accent"]
    panel, panel_text = block_with_text(brand, t["ink"])
    accent_on_dark = adjust_to_contrast(brand, WASH, 4.5)
    layers: List[Dict] = []
    z = 0

    z += 1
    layers.append({"type": "ai_generated_background", "z_index": z, "url": scene_url,
                   "x": 0, "y": 0, "width": width, "height": height, "fit": "cover", "focus": (0.6, 0.4)})
    # Light wash at the top so the logo/top line read; the deep one under the headline.
    z += 1
    layers.append({"type": "shape", "z_index": z, "shape": "vgradient", "x": 0, "y": 0,
                   "width": width, "height": 220, "color": WASH, "from_alpha": 150, "to_alpha": 0})

    size, lines = fit_headline(headline, headline_font, 800, width - 2 * PAD, 3, 96, 56)
    pitch = int(size * 1.08)
    block_h = pitch * len(lines)
    head_top = height - PAD - block_h
    # A soft fade down to the headline, then a deep wash behind it — at least 90% where the
    # first line starts, so the white type always has a real backing (§1.6 gradient scrim).
    fade_top = max(int(height * 0.38), head_top - 300)
    z += 1
    layers.append({"type": "shape", "z_index": z, "shape": "vgradient", "x": 0, "y": fade_top,
                   "width": width, "height": head_top - 16 - fade_top, "color": WASH,
                   "from_alpha": 0, "to_alpha": 232})
    z += 1
    layers.append({"type": "shape", "z_index": z, "shape": "vgradient", "x": 0, "y": head_top - 16,
                   "width": width, "height": height - head_top + 16, "color": WASH,
                   "from_alpha": 232, "to_alpha": 250})
    per_line = lines_with_emphasis(lines, emphasis)
    for i, line in enumerate(lines):
        z += 1
        y = head_top + i * pitch
        layers.append({"type": "text", "z_index": z, "content": line,
                       "spans": emphasis_spans(line, per_line[i], "#FFFFFF", accent_on_dark),
                       "font_family": headline_font, "font_size": size, "font_weight": 800,
                       "color": "#FFFFFF", "x": PAD, "y": y})

    # Service tags down the left, above the headline.
    tags = [s.strip() for s in services if s and s.strip()][:5]
    tag_h, gap = 64, 14
    tag_y = head_top - 70 - len(tags) * (tag_h + gap)
    tag_y = max(tag_y, 250)
    colours = [panel] + [c for c in _TAG_COLOURS if c.upper() != panel.upper()]
    from app.utils.fonts import text_width
    for i, tag in enumerate(tags):
        label = f"• {tag}"
        w = int(text_width(label, BODY_FAMILY, 42, 600)) + 44
        fill = colours[i % len(colours)]
        if contrast_ratio("#FFFFFF", fill) < 7.0:
            fill = darken_to_contrast(fill, "#FFFFFF", 7.0)  # same hue, deep enough for white text
        fg = "#FFFFFF"
        z += 1
        layers.append({"type": "shape", "z_index": z, "shape": "rect", "x": PAD, "y": tag_y,
                       "width": w, "height": tag_h, "fill_color": fill})
        z += 1
        layers.append({"type": "text", "z_index": z, "content": label, "font_family": BODY_FAMILY,
                       "font_size": 42, "font_weight": 600, "color": fg, "x": PAD + 22, "y": tag_y + 8})
        tag_y += tag_h + gap

    if ticket_text:
        z += 1
        layers.append({"type": "badge", "z_index": z, "text": ticket_text, "font_family": headline_font,
                       "font_size": 48, "font_weight": 800, "fill_color": panel, "text_color": panel_text,
                       "padding_x": 34, "padding_y": 18, "corner_radius": 14, "rotation": 16,
                       # Right side, just above the headline: the scene prompt keeps the
                       # person's face in the upper middle, so the ticket lands on their lap or
                       # the sofa, never across a face.
                       "cx": int(width * 0.74), "cy": head_top - 90})

    if brand_logo_url:
        z += 1
        layers.append({"type": "brand_asset", "z_index": z, "url": brand_logo_url,
                       "x": PAD, "y": PAD - 10, "width": 170, "height": 68})

    document = {"canvas": {"width": width, "height": height, "background_color": WASH}, "layers": layers}
    assert_legible(document, t)
    return document

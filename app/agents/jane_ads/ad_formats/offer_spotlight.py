"""
Offer Spotlight — a real discount or deal on a lifestyle photo (SEED-100).

Built after the "Get 50% off all of our creative services" reference: a warm, everyday photo
fills the frame (someone relaxed on a sofa with their phone); the business's services sit as a
stack of bright tags down the left; a tilted paper ticket ("50% Off All Services", five stars on
its stub) leans in beside the person; a dark tilted tag names the occasion ("Anniversary
Discounted Offer"); the headline sits bottom-left with the offer itself ("Get 50%") in the brand
colour; a brand-colour asterisk and a big brand-colour "V" frame the picture, with a small white
sparkle; the brand's handle sits in a search-style pill under the headline.

Asset source `generate`: the photo shows a situation, not the product and not a customer — the
same rule Problem / Solution uses (§1.2). The offer, the services, the occasion and the handle
are always the business's own; the orchestrator only fills this format when the brief states a
real offer.
"""
from typing import Dict, List, Optional, Tuple

from app.utils.fonts import text_width
from ._type import adjust_to_contrast, emphasis_spans, fit_headline, lines_with_emphasis, vivid_block
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
HEAD_WEIGHT = 500
HEAD_TRACKING = -0.045
BODY_FAMILY = "Inter"
PAD = 56
WASH = "#0B0B0F"
INK = "#111114"

# The reference's bright tag stack. Each is light enough for dark text at 7:1, so the colours
# stay vivid instead of being deepened for white text. The brand colour leads.
_TAG_COLOURS = ["#FF6B9E", "#FFD43B", "#4C9BFF", "#FF8A3D", "#2BD67B", "#FF6A5C"]


def scene_prompt(scene: str) -> str:
    # REPRESENTATION_BLOCK is the same §1.7 representation instruction every other format's
    # photo prompt carries; without it the model drew lighter skin and the skin-tone check
    # rejected both attempts (seen on the first staging run).
    return (
        f"A warm, natural lifestyle photograph: {scene}. {REPRESENTATION_BLOCK}. One person, "
        "relaxed and genuinely pleased, sitting cross-legged on a soft sofa in a calm living room, "
        "looking down at their phone. Soft daylight, a plain warm wall behind, a tall green plant "
        "at the far left edge. The person sits in the centre-right of the frame, seen from head to "
        "knees, their face in the upper-middle of the frame. Keep the left third and the bottom "
        "quarter of the frame calm (plain wall and sofa) — tags and a headline are placed there. "
        "Editorial advertising quality, shallow depth of field, real skin texture, no text, no "
        "logos, no readable screens."
    )


def _tag_colours(brand: str) -> List[Tuple[str, str]]:
    """(fill, text colour) for each tag: the brand colour first, then the bright set."""
    first = vivid_block(brand, INK)
    rest = [vivid_block(c, INK) for c in _TAG_COLOURS if c.upper() != brand.upper()]
    return [first] + rest


def _sparkle(cx: int, cy: int, r: int, color: str) -> Dict:
    """A small four-point star."""
    k = 0.22
    pts = [[cx, cy - r], [cx + r * k, cy - r * k], [cx + r, cy], [cx + r * k, cy + r * k],
           [cx, cy + r], [cx - r * k, cy + r * k], [cx - r, cy], [cx - r * k, cy - r * k]]
    return {"type": "shape", "shape": "polygon", "points": pts, "fill_color": color}


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
    occasion: Optional[str] = None,
    handle: Optional[str] = None,
) -> Dict:
    """
    headline: "Get 50% off all of our creative services".
    emphasis: the part of the headline shown in the brand colour ("Get 50%") — must appear in it.
    ticket_text: the tilted ticket ("50% Off All Services"); omitted when None.
    services: up to 6 short tags ("Graphic Design Services").
    occasion: what the offer celebrates, for the dark tag ("Anniversary Discounted Offer").
    handle: the brand's @handle or website, in the pill under the headline.
    """
    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    brand = brand_color or t["accent"]
    accent_on_dark = adjust_to_contrast(brand, WASH, 4.5)
    deep_brand = brand if contrast_ratio("#FFFFFF", brand) >= 7 else darken_to_contrast(brand, "#FFFFFF", 7.0)
    layers: List[Dict] = []

    layers.append({"type": "ai_generated_background", "url": scene_url, "x": 0, "y": 0,
                   "width": width, "height": height, "fit": "cover", "focus": (0.6, 0.42)})
    # A light wash at the top for the logo; a deep one under the headline.
    layers.append({"type": "shape", "shape": "vgradient", "x": 0, "y": 0, "width": width, "height": 200,
                   "color": WASH, "from_alpha": 110, "to_alpha": 0})

    hw, tr = HEAD_WEIGHT, HEAD_TRACKING
    max_w = int(width * 0.62)
    size, lines = fit_headline(headline, headline_font, hw, max_w, 3, 84, 56, tr)
    pitch = int(size * 1.04)
    pill_h = 58 if handle else 0
    head_top = height - PAD - (pill_h + 26 if handle else 0) - pitch * len(lines)
    fade_top = max(int(height * 0.42), head_top - 300)
    layers.append({"type": "shape", "shape": "vgradient", "x": 0, "y": fade_top, "width": width,
                   "height": head_top - 16 - fade_top, "color": WASH, "from_alpha": 0, "to_alpha": 214})
    layers.append({"type": "shape", "shape": "vgradient", "x": 0, "y": head_top - 16, "width": width,
                   "height": height - head_top + 16, "color": WASH, "from_alpha": 214, "to_alpha": 245})

    # The big brand-colour "V" in the bottom-right corner, cut by the frame, under the type.
    stroke = int(width * 0.075)
    layers.append({"type": "shape", "shape": "path", "color": brand, "stroke_width": stroke,
                   "points": [[width * 0.70, height * 0.83], [width * 0.84, height * 1.03],
                              [width * 1.0, height * 0.79]]})

    per_line = lines_with_emphasis(lines, emphasis)
    for i, line in enumerate(lines):
        layers.append({"type": "text", "content": line,
                       "spans": emphasis_spans(line, per_line[i], "#FFFFFF", accent_on_dark),
                       "font_family": headline_font, "font_size": size, "font_weight": hw, "tracking": tr,
                       "color": "#FFFFFF", "x": PAD, "y": head_top + i * pitch})

    if handle:
        hs = 28
        label = handle.strip()
        pw = int(text_width(label, BODY_FAMILY, hs, 500)) + 96
        py = height - PAD - pill_h
        layers.append({"type": "shape", "shape": "rounded_rect", "x": PAD, "y": py, "width": pw, "height": pill_h,
                       "corner_radius": pill_h // 2, "fill_color": "#1C1C21", "border_color": "#3A3A42",
                       "border_width": 2})
        layers.append({"type": "icon", "name": "search" if label.startswith("@") else "globe", "x": PAD + 22,
                       "y": py + (pill_h - 26) // 2, "size": 26, "color": "#CFCFD6"})
        layers.append({"type": "text", "content": label, "font_family": BODY_FAMILY, "font_size": hs,
                       "font_weight": 500, "color": "#FFFFFF", "x": PAD + 62, "y": py + (pill_h - hs) // 2 - 3,
                       "secondary": True})

    # Service tags down the left, above the headline.
    tags = [s.strip() for s in services if s and s.strip()][:6]
    tag_h, gap, ts = 48, 12, 28
    colours = _tag_colours(brand)
    # Everything left of the person must fit between the logo and the headline: fewer tags
    # first (never fewer than two), then the occasion tag, rather than crowd the headline.
    room = head_top - 60 - 200
    while tags and len(tags) * (tag_h + gap) + (150 if occasion else 0) > room:
        if len(tags) > 2:
            tags = tags[:-1]
        elif occasion:
            occasion = None
        else:
            tags = []
    tag_block = len(tags) * (tag_h + gap)
    occasion_room = 150 if occasion else 0
    tag_y = max(200, head_top - 60 - occasion_room - tag_block)
    stack_top, stack_right = tag_y, PAD
    for i, tag in enumerate(tags):
        label = f"•  {tag}"
        w = int(text_width(label, BODY_FAMILY, ts, 500)) + 36
        fill, fg = colours[i % len(colours)]
        layers.append({"type": "shape", "shape": "rect", "x": PAD + 20, "y": tag_y, "width": w, "height": tag_h,
                       "fill_color": fill})
        stack_right = max(stack_right, PAD + 20 + w)
        layers.append({"type": "text", "content": label, "font_family": BODY_FAMILY, "font_size": ts,
                       "font_weight": 500, "color": fg, "x": PAD + 38, "y": tag_y + (tag_h - ts) // 2 - 4,
                       "secondary": True})
        tag_y += tag_h + gap

    if occasion:
        # The dark tilted tag under the stack, leaning down to the right.
        words = occasion.split()
        label = occasion if len(words) < 3 else " ".join(words[:len(words) // 2]) + "\n" + " ".join(words[len(words) // 2:])
        tag_w = max(text_width(l, headline_font, 34, 700) for l in label.split("\n")) + 48
        layers.append({"type": "badge", "text": label, "font_family": headline_font, "font_size": 34,
                       "font_weight": 700, "tracking": -0.02, "fill_color": "#101512", "text_color": "#FFFFFF",
                       "padding_x": 24, "padding_y": 12, "corner_radius": 4, "rotation": -11,
                       "cx": int(PAD + 30 + tag_w / 2), "cy": tag_y + 82})

    if ticket_text:
        words = ticket_text.split()
        two = [" ".join(words[: (len(words) + 1) // 2]), " ".join(words[(len(words) + 1) // 2:])] \
            if len(words) > 2 else [ticket_text]
        # Right side, beside the person: the scene keeps the face in the upper middle, so the
        # ticket lands by their hands and lap, never across a face.
        layers.append({"type": "badge", "style": "ticket", "text": "\n".join(l for l in two if l),
                       "font_family": headline_font, "font_size": 40, "font_weight": 700, "tracking": -0.03,
                       "fill_color": deep_brand, "text_color": "#FFFFFF", "star_color": "#FFD43B",
                       "padding_x": 22, "padding_y": 18, "rotation": 22,
                       # its lowest corner stays clear of the headline's first line
                       "cx": int(width * 0.79), "cy": min(int(height * 0.56), head_top - 160)})

    # The brand-colour asterisk and a small sparkle, as in the reference.
    layers.append({"type": "text", "content": "*", "font_family": "Inter", "font_size": 200, "font_weight": 900,
                   "color": brand, "x": int(width * 0.9), "y": int(height * 0.2), "text_align": "mm",
                   "decorative": True})
    # The sparkle sits just off the top-right corner of the tag stack, never on a tag.
    layers.append(_sparkle(min(int(width * 0.5), stack_right + 46), max(110, stack_top - 34), 18, "#FFFFFF"))

    if brand_logo_url:
        layers.append({"type": "brand_asset", "url": brand_logo_url, "x": PAD, "y": PAD - 14,
                       "width": 190, "height": 76})

    for z, layer in enumerate(layers, start=1):
        layer["z_index"] = z
    document = {"canvas": {"width": width, "height": height, "background_color": WASH}, "layers": layers}
    assert_legible(document, t)
    return document

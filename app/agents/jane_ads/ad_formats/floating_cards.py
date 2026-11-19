"""
Floating Cards — a lifestyle photo with small app-style cards floating on it (SEED-103).

Built after the "Running out of stock shouldn't surprise you." reference: someone at ease using
their laptop or phone fills the photo; the brand's logo sits top-left; a big, tightly set
headline is right-aligned across the top with a short line under it; two white cards float over
the picture like pieces of the product's own screen (a label with a big value; a row with a
status tag on its corner); two arrow-ended pills run along the bottom — the brand colour saying
what to do with the brand ("Track your inventory with daash."), the second colour carrying the
website.

Asset source `generate` — a situation, never a named customer. The cards show what the product
does in the business's own words; no figure appears on them unless the business gave it.
"""
import math
from typing import Dict, List, Optional, Tuple

from app.utils.fonts import load_font, text_width
from ._type import fit_headline, vivid_block, wrap_words
from .legibility import assert_legible
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS
from ..layer2_generation import REPRESENTATION_BLOCK

FORMAT = AdFormatDef(
    format_id="SEED-103",
    name="Floating Cards",
    asset_source="generate",
    layers_used="L2-L4",
    brand_mark="required",
    requires=[],
)

DEFAULT_FAMILY = "Poppins"
HEAD_WEIGHT = 600
HEAD_TRACKING = -0.05
BODY_FAMILY = "Inter"
PAD = 56
WASH = "#0C0D10"
CARD = "#FFFFFF"
CARD_INK = "#16161A"
CARD_QUIET = "#3C3C44"


def scene_prompt(person: str, device: str, setting: str) -> str:
    return (
        f"A bright, natural lifestyle photograph of {person}, relaxed and smiling, sitting "
        f"comfortably and using {device}. {REPRESENTATION_BLOCK}. Setting: {setting}, with warm "
        "wooden shelves or a wall in soft shade across the top of the frame. Framing: square; the "
        "person sits in the lower-middle of the frame seen from head to knees, their face at about "
        "the middle height; the top third of the frame is calm background in shade (a headline is "
        "placed there). Soft daylight, editorial advertising quality, real skin texture. No text, "
        "no logos, no readable screens."
    )


def _arrow_pill(x: int, y: int, w: int, h: int, point: str, fill: str) -> Dict:
    """A pill with one rounded end and one pointed end: point="right" (the brand pill) or
    "left" (the second pill, notched so it points back at the first)."""
    r = h / 2
    pts: List[List[float]] = []
    tip = h * 0.42
    if point == "right":
        for k in range(13):  # rounded left end, top to bottom round the left
            a = math.pi / 2 + k * math.pi / 12
            pts.append([x + r + r * math.cos(a), y + r - r * math.sin(a)])
        pts += [[x + w - tip, y + h], [x + w, y + r], [x + w - tip, y]]
    else:
        pts += [[x + tip, y], [x, y + r], [x + tip, y + h]]
        for k in range(13):  # rounded right end, bottom to top round the right
            a = -math.pi / 2 + k * math.pi / 12
            pts.append([x + w - r + r * math.cos(a), y + r - r * math.sin(a)])
    return {"type": "shape", "shape": "polygon", "points": pts, "fill_color": fill}


def build_document(
    scene_url: str,
    headline: str,
    subline: Optional[str],
    card_one: Optional[Tuple[str, str]],
    card_two: Optional[Tuple[str, str]],
    cta_line: str,
    website: Optional[str],
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
    brand_logo_url: Optional[str] = None,
    headline_font: str = DEFAULT_FAMILY,
    brand_color: Optional[str] = None,
    second_color: Optional[str] = None,
) -> Dict:
    """
    headline: "Running out of stock shouldn't surprise you." (<= 3 lines, right-aligned).
    subline: "Know what's selling, know what's left." or None.
    card_one: (label, value) — "Items out of stock" / "6".
    card_two: (text, status) — "Asymmetric gold ring" / "Low in stock!".
    cta_line: the brand pill — "Track your inventory with daash."
    website: the second pill — "daashapp.co".
    """
    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    brand = brand_color or t["accent"]
    second = second_color or brand
    cta_fill, cta_ink = vivid_block(brand, CARD_INK)
    web_fill, web_ink = vivid_block(second, CARD_INK)
    tag_fill, tag_ink = vivid_block(second if second != brand else brand, CARD_INK)
    layers: List[Dict] = []

    layers.append({"type": "ai_generated_background", "url": scene_url, "x": 0, "y": 0, "width": width,
                   "height": height, "fit": "cover", "focus": (0.5, 0.55)})

    hw, tr = HEAD_WEIGHT, HEAD_TRACKING
    max_w = int(width * 0.72)
    size, lines = fit_headline(headline, headline_font, hw, max_w, 3, 96, 60, tr)
    pitch = int(size * 1.0)
    head_top = PAD + 8
    sub_lines = wrap_words(subline, BODY_FAMILY, 34, 600, max_w) if subline else []
    sub_lines = sub_lines[:2]
    # Placed from where the headline's ink really ends: the last line's descenders (the p and y of
    # "surprise you.") hang well below its line pitch and used to cut into the subline.
    sub_top = head_top + pitch * len(lines) + 22
    if sub_lines:
        head_ink_bottom = load_font(headline_font, size, hw).getbbox(lines[-1], anchor="la")[3]
        sub_ink_top = load_font(BODY_FAMILY, 34, 600).getbbox(sub_lines[0], anchor="la")[1]
        sub_top = max(sub_top, head_top + pitch * (len(lines) - 1) + head_ink_bottom + 18 - sub_ink_top)
    text_bottom = sub_top + (len(sub_lines) * 42 if sub_lines else 0)
    # A deep wash from the top through the type, then fading out — §1.6's scrim, tinted dark.
    layers.append({"type": "shape", "shape": "vgradient", "x": 0, "y": 0, "width": width,
                   "height": text_bottom + 10, "color": WASH, "from_alpha": 228, "to_alpha": 208})
    layers.append({"type": "shape", "shape": "vgradient", "x": 0, "y": text_bottom + 10, "width": width,
                   "height": 230, "color": WASH, "from_alpha": 208, "to_alpha": 0})
    for i, line in enumerate(lines):
        layers.append({"type": "text", "content": line, "font_family": headline_font, "font_size": size,
                       "font_weight": hw, "tracking": tr, "color": "#FFFFFF", "x": width - PAD,
                       "y": head_top + i * pitch, "text_align": "ra"})
    for j, sl in enumerate(sub_lines):
        layers.append({"type": "text", "content": sl, "font_family": BODY_FAMILY, "font_size": 34,
                       "font_weight": 600, "tracking": -0.02, "color": "#FFFFFF", "x": width - PAD,
                       "y": sub_top + j * 42, "text_align": "ra", "secondary": True})

    # The bottom pills: a soft dark fade behind them keeps them crisp on any photo.
    pill_h = 92
    pill_y = height - PAD - pill_h
    layers.append({"type": "shape", "shape": "vgradient", "x": 0, "y": pill_y - 140, "width": width,
                   "height": height - pill_y + 140, "color": WASH, "from_alpha": 0, "to_alpha": 150})
    cta_size = 32
    cta_w = int(text_width(cta_line, BODY_FAMILY, cta_size, 600)) + 110
    web_w = 0
    if website:
        web_w = int(text_width(website, BODY_FAMILY, cta_size, 600)) + 120
    gap = 24
    while cta_w + (web_w + gap if website else 0) > width - 2 * PAD and cta_size > 28:
        cta_size -= 2
        cta_w = int(text_width(cta_line, BODY_FAMILY, cta_size, 600)) + 110
        web_w = int(text_width(website, BODY_FAMILY, cta_size, 600)) + 120 if website else 0
    if cta_w + (web_w + gap if website else 0) > width - 2 * PAD:
        raise ValueError("the call to action doesn't fit — shorten it")
    total = cta_w + (web_w + gap if website else 0)
    x0 = (width - total) // 2
    layers.append(_arrow_pill(x0, pill_y, cta_w, pill_h, "right", cta_fill))
    layers.append({"type": "text", "content": cta_line, "font_family": BODY_FAMILY, "font_size": cta_size,
                   "font_weight": 600, "color": cta_ink, "x": x0 + 44, "y": pill_y + pill_h // 2,
                   "text_align": "lm", "secondary": True})
    if website:
        wx = x0 + cta_w + gap
        layers.append(_arrow_pill(wx, pill_y, web_w, pill_h, "left", web_fill))
        layers.append({"type": "text", "content": website, "font_family": BODY_FAMILY, "font_size": cta_size,
                       "font_weight": 600, "color": web_ink, "x": wx + web_w // 2 + 14, "y": pill_y + pill_h // 2,
                       "text_align": "mm", "secondary": True})

    # The two floating cards, left-middle and right-lower, like pieces of the product's screen.
    if card_one:
        label, value = card_one
        lw = max(text_width(label, BODY_FAMILY, 30, 500), text_width(value, BODY_FAMILY, 46, 700))
        cw, ch = int(lw) + 72, 150
        cx, cy = PAD - 6, max(text_bottom + 90, int(height * 0.47))
        layers.append({"type": "shape", "shape": "rounded_rect", "x": cx + 4, "y": cy + 8, "width": cw, "height": ch,
                       "corner_radius": 22, "fill_color": "#00000033"})
        layers.append({"type": "shape", "shape": "rounded_rect", "x": cx, "y": cy, "width": cw, "height": ch,
                       "corner_radius": 22, "fill_color": CARD})
        layers.append({"type": "text", "content": label, "font_family": BODY_FAMILY, "font_size": 30,
                       "font_weight": 500, "color": CARD_QUIET, "x": cx + 36, "y": cy + 30, "secondary": True})
        layers.append({"type": "text", "content": value, "font_family": BODY_FAMILY, "font_size": 46,
                       "font_weight": 700, "color": CARD_INK, "x": cx + 36, "y": cy + 76})
    if card_two:
        text, status = card_two
        row_w = int(text_width(text, BODY_FAMILY, 30, 600)) + 140
        cw, ch = max(row_w, 360), 96
        cx = width - PAD - cw - 30
        cy = pill_y - 70 - ch
        layers.append({"type": "shape", "shape": "rounded_rect", "x": cx + 4, "y": cy + 8, "width": cw, "height": ch,
                       "corner_radius": 20, "fill_color": "#00000033"})
        layers.append({"type": "shape", "shape": "rounded_rect", "x": cx, "y": cy, "width": cw, "height": ch,
                       "corner_radius": 20, "fill_color": CARD})
        # A brand-colour mark where the reference shows the product thumbnail.
        layers.append({"type": "shape", "shape": "rounded_rect", "x": cx + 22, "y": cy + 20, "width": 56,
                       "height": 56, "corner_radius": 14, "fill_color": cta_fill})
        layers.append({"type": "icon", "name": "check", "x": cx + 34, "y": cy + 32, "size": 32, "color": cta_ink})
        layers.append({"type": "text", "content": text, "font_family": BODY_FAMILY, "font_size": 30,
                       "font_weight": 600, "color": CARD_INK, "x": cx + 100, "y": cy + ch // 2,
                       "text_align": "lm", "secondary": True})
        if status:
            sw = int(text_width(status, BODY_FAMILY, 28, 600)) + 32
            sx, sy = cx + cw - sw - 18, cy - 26
            layers.append({"type": "shape", "shape": "rounded_rect", "x": sx, "y": sy, "width": sw, "height": 46,
                           "corner_radius": 12, "fill_color": tag_fill})
            layers.append({"type": "text", "content": status, "font_family": BODY_FAMILY, "font_size": 28,
                           "font_weight": 600, "color": tag_ink, "x": sx + 16, "y": sy + 23,
                           "text_align": "lm", "secondary": True})

    if brand_logo_url:
        layers.append({"type": "brand_asset", "url": brand_logo_url, "x": PAD - 8, "y": PAD, "width": 120,
                       "height": 120})

    for z, layer in enumerate(layers, start=1):
        layer["z_index"] = z
    document = {"canvas": {"width": width, "height": height, "background_color": WASH}, "layers": layers}
    assert_legible(document, t)
    return document

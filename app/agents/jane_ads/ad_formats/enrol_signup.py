"""
Enrol / Sign-up — "secure your seat" for a school, course, programme or membership (SEED-104).

Built after the "Campus, Career & Beyond" reference: a happy learner or member fills the photo
(the setting softly blurred behind them); the logo sits top-left and the website top-right; a
short kicker ("Your Future Begins Here") sits over a big, heavy capitals headline whose lines
are underlined in the brand colour, with a short line beside the last one; a brand-colour
swoosh with a loop runs across the picture; a light button says what to do ("SECURE YOUR SEAT
→") above a dark contact pill carrying the business's real phone number and email.

Asset source `generate` — a situation, never a named student or member. Every contact detail is
the business's own (the brief or its brand profile); none is ever made up.
"""
from typing import Dict, List, Optional, Tuple

from app.utils.fonts import text_width
from ._type import block_with_text, fit_headline, tint, wrap_words
from .brand_tokens import contrast_ratio, darken_to_contrast
from .legibility import assert_legible
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS
from ..layer2_generation import REPRESENTATION_BLOCK

FORMAT = AdFormatDef(
    format_id="SEED-104",
    name="Enrol / Sign-up",
    asset_source="generate",
    layers_used="L2-L4",
    brand_mark="required",
    requires=[],
)

DEFAULT_FAMILY = "Montserrat"
KICKER_FAMILY = "Poppins"
BODY_FAMILY = "Inter"
PAD = 56
WASH = "#0A0D12"


def scene_prompt(person: str, setting: str, doing: str) -> str:
    return (
        f"A bright, joyful lifestyle photograph of {person}, {doing}, laughing naturally. "
        f"{REPRESENTATION_BLOCK}. Setting: {setting} — the background softly blurred with shallow "
        "depth of field, gentle greenery and a building behind. Framing: square; the person sits "
        "on wide stone steps in the centre of the lower half of the frame, seen whole from head to "
        "shoes; the upper half of the frame is calm, blurred background (a headline goes there). "
        "Natural daylight, editorial advertising quality, real skin texture. No text, no logos, no "
        "signs, no readable screens."
    )


def _swoosh(width: int, height: int) -> List[List[float]]:
    """The reference's ribbon: in from the left edge, then (past the person) a loop on the right
    and out of the frame — kept to the sides so it never crosses a face."""
    y = height * 0.66
    return [[-20, y + 30], [width * 0.14, y - 10], [width * 0.27, y + 6]], \
        [[width * 0.70, y + 40], [width * 0.80, y + 4], [width * 0.88, y - 70], [width * 0.84, y - 120],
         [width * 0.77, y - 92], [width * 0.80, y - 30], [width * 0.90, y + 6], [width + 30, y - 6]]


def build_document(
    scene_url: str,
    kicker: Optional[str],
    headline: str,
    side_line: Optional[str],
    button: str,
    contacts: List[Tuple[str, str]],
    website: Optional[str],
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
    brand_logo_url: Optional[str] = None,
    headline_font: str = DEFAULT_FAMILY,
    brand_color: Optional[str] = None,
) -> Dict:
    """
    kicker: "Your Future Begins Here" (above the headline) or None.
    headline: "Campus, Career & Beyond" — set in capitals, <= 2 lines.
    side_line: a short line beside the last headline line, or None.
    button: "Secure your seat".
    contacts: up to two (kind, value) — ("phone", "+234 803 000 0000"), ("email", "hello@…").
    website: shown top-right.
    """
    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    brand = brand_color or t["accent"]
    bar = brand
    deep = brand if contrast_ratio("#FFFFFF", brand) >= 7 else darken_to_contrast(brand, "#FFFFFF", 7.0)
    button_fill = tint(brand, 0.82)
    button_ink = deep if contrast_ratio(deep, button_fill) >= 7 else "#0E1A10"
    layers: List[Dict] = []

    layers.append({"type": "ai_generated_background", "url": scene_url, "x": 0, "y": 0, "width": width,
                   "height": height, "fit": "cover", "focus": (0.5, 0.6)})

    big_text = headline.upper()
    hw = 800
    left = int(width * 0.14)
    size, big_lines = fit_headline(big_text, headline_font, hw, width - left - PAD - 40, 2, 92, 58, -0.01)
    kick_lines = wrap_words(kicker, KICKER_FAMILY, 44, 600, int(width * 0.5))[:2] if kicker else []
    kick_pitch = 46
    top = 190
    big_top = top + (len(kick_lines) * kick_pitch + 14 if kick_lines else 0)
    big_pitch = int(size * 1.22)
    block_bottom = big_top + big_pitch * len(big_lines)
    # Deep wash through the top and the headline block, fading out above the person (§1.6).
    layers.append({"type": "shape", "shape": "vgradient", "x": 0, "y": 0, "width": width,
                   "height": block_bottom + 10, "color": WASH, "from_alpha": 224, "to_alpha": 206})
    layers.append({"type": "shape", "shape": "vgradient", "x": 0, "y": block_bottom + 10, "width": width,
                   "height": 220, "color": WASH, "from_alpha": 206, "to_alpha": 0})

    first, second = _swoosh(width, height)
    for pts in (first, second):
        layers.append({"type": "shape", "shape": "path", "points": pts, "smooth": True, "color": bar,
                       "stroke_width": 10})

    for i, kl in enumerate(kick_lines):
        layers.append({"type": "text", "content": kl, "font_family": KICKER_FAMILY, "font_size": 44,
                       "font_weight": 600, "tracking": -0.03, "color": "#FFFFFF", "x": left, "y": top + i * kick_pitch})
    for i, bl in enumerate(big_lines):
        y = big_top + i * big_pitch
        w = text_width(bl, headline_font, size, hw, -0.01)
        layers.append({"type": "shape", "shape": "rect", "x": left, "y": y + int(size * 0.98), "width": int(w),
                       "height": 9, "fill_color": bar})
        layers.append({"type": "text", "content": bl, "font_family": headline_font, "font_size": size,
                       "font_weight": hw, "tracking": -0.01, "color": "#FFFFFF", "x": left, "y": y})
    if side_line:
        last_w = text_width(big_lines[-1], headline_font, size, hw, -0.01)
        sx = int(left + last_w + 24)
        room = width - PAD - sx
        side = wrap_words(side_line, KICKER_FAMILY, 36, 600, room) if room > 200 else []
        if 0 < len(side) <= 2:
            sy = big_top + (len(big_lines) - 1) * big_pitch + (int(size * 0.5) if len(side) == 1 else 0)
            for j, sl in enumerate(side):
                layers.append({"type": "text", "content": sl, "font_family": KICKER_FAMILY, "font_size": 36,
                               "font_weight": 600, "tracking": -0.03, "color": "#FFFFFF", "x": sx,
                               "y": sy + j * 40, "secondary": True})

    # Bottom: the button, then the contact pill under it — on a soft fade so both stay crisp.
    contacts = [(k, v) for k, v in contacts if v][:2]
    pill_h = 62
    pill_y = height - 40 - pill_h
    btn_h = 70
    btn_y = (pill_y - 22 - btn_h) if contacts else height - 56 - btn_h
    layers.append({"type": "shape", "shape": "vgradient", "x": 0, "y": btn_y - 160, "width": width,
                   "height": height - btn_y + 160, "color": WASH, "from_alpha": 0, "to_alpha": 170})
    label = f"{button.upper()}"
    bs = 32
    bw = int(text_width(label, headline_font, bs, 800)) + 120
    bx = (width - bw) // 2
    layers.append({"type": "shape", "shape": "rounded_rect", "x": bx, "y": btn_y, "width": bw, "height": btn_h,
                   "corner_radius": 14, "fill_color": button_fill, "border_color": deep, "border_width": 3})
    layers.append({"type": "text", "content": label, "font_family": headline_font, "font_size": bs,
                   "font_weight": 800, "color": button_ink, "x": bx + 34, "y": btn_y + btn_h // 2,
                   "text_align": "lm", "secondary": True})
    layers.append({"type": "icon", "name": "arrow-right", "x": bx + bw - 34 - 40, "y": btn_y + (btn_h - 40) // 2,
                   "size": 40, "color": button_ink})

    if contacts:
        cs = 28
        items = []
        for kind, value in contacts:
            items.append((kind, value, int(text_width(value, BODY_FAMILY, cs, 500))))
        pw = sum(56 + w for _, _, w in items) + 44 * (len(items) - 1) + 40
        px = (width - pw) // 2
        layers.append({"type": "shape", "shape": "rounded_rect", "x": px, "y": pill_y, "width": pw, "height": pill_h,
                       "corner_radius": pill_h // 2, "fill_color": deep})
        x = px + 16
        for kind, value, w in items:
            layers.append({"type": "shape", "shape": "rounded_rect", "x": x, "y": pill_y + 11, "width": 40,
                           "height": 40, "corner_radius": 10, "fill_color": "#FFFFFF"})
            layers.append({"type": "icon", "name": "mail" if kind == "email" else ("globe" if kind == "web" else "phone"),
                           "x": x + 8, "y": pill_y + 19, "size": 24, "color": deep})
            layers.append({"type": "text", "content": value, "font_family": BODY_FAMILY, "font_size": cs,
                           "font_weight": 500, "color": "#FFFFFF", "x": x + 56, "y": pill_y + pill_h // 2,
                           "text_align": "lm", "secondary": True})
            x += 56 + w + 44

    if website:
        layers.append({"type": "text", "content": website, "font_family": BODY_FAMILY, "font_size": 30,
                       "font_weight": 600, "color": "#FFFFFF", "x": width - PAD, "y": PAD + 18, "text_align": "ra",
                       "secondary": True})
    if brand_logo_url:
        layers.append({"type": "brand_asset", "role": "brand_logo", "url": brand_logo_url, "x": PAD, "y": PAD - 6, "width": 220,
                       "height": 84, "tint": "#FFFFFF"})

    for z, layer in enumerate(layers, start=1):
        layer["z_index"] = z
    document = {"canvas": {"width": width, "height": height, "background_color": WASH}, "layers": layers}
    assert_legible(document, t)
    return document

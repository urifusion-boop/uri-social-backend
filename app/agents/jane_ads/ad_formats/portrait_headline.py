"""
Portrait + Headline — a person filling the frame, a big headline across them (SEED-102).

Built after the "Fresh Crops for Healthy Living" reference: a warm, close portrait fills the
picture (a smiling person holding what the business is about, the setting softly blurred
behind); a thin white rounded frame sits inset over the photo; a big, tightly set headline runs
across the lower half with one word in the brand's second colour; a short description sits
beside the last line; a two-tone pill straddles the frame's bottom edge — the brand's own
colour carrying its logo, the second colour carrying where to buy ("Shop from us at:
vfarmers.com").

The person is either generated (asset source `generate` — a situation, never presented as a
named customer) or the owner's own photo (owner_photo attestation). A customer's photo never
reaches this format: the headline is the business's words, and across a real customer's face it
would read as theirs.
"""
from typing import Dict, List, Optional, Tuple

from app.utils.fonts import text_width
from ._type import adjust_to_contrast, emphasis_spans, vivid_block, fit_headline, lines_with_emphasis, wrap_words
from .legibility import assert_legible
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS
from ..layer2_generation import REPRESENTATION_BLOCK

FORMAT = AdFormatDef(
    format_id="SEED-102",
    name="Portrait + Headline",
    asset_source="generate",
    layers_used="L2-L4",
    brand_mark="required",
    requires=[],
)

DEFAULT_FAMILY = "Poppins"
HEAD_WEIGHT = 600
HEAD_TRACKING = -0.05
BODY_FAMILY = "Inter"
WASH = "#0A0C0A"
INK = "#121212"


def scene_prompt(person: str, holding: str, setting: str) -> str:
    return (
        f"A warm, close editorial portrait photograph of {person}, smiling broadly and looking "
        f"straight at the camera, {holding}. {REPRESENTATION_BLOCK}. Setting: {setting}, softly "
        "blurred behind them (shallow depth of field). Framing: head and shoulders fill the upper "
        "two-thirds of a square frame, the face slightly right of centre and in the upper third; "
        "what they hold sits close to the camera across the bottom of the frame. Golden, natural "
        "light, rich colour, real skin texture, premium advertising photography. No text, no "
        "logos, no watermarks."
    )


def build_document(
    photo_url: str,
    headline: str,
    emphasis: Optional[str],
    description: Optional[str],
    cta_lead: Optional[str],
    cta_target: Optional[str],
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
    brand_logo_url: Optional[str] = None,
    brand_name: Optional[str] = None,
    headline_font: str = DEFAULT_FAMILY,
    brand_color: Optional[str] = None,
    second_color: Optional[str] = None,
) -> Dict:
    """
    headline: "Fresh Crops for Healthy Living" (<= 3 lines).
    emphasis: the word in the second colour ("Healthy") — must appear in the headline.
    description: one short sentence beside the last line, or None.
    cta_lead / cta_target: the right half of the pill — "Shop from us at:" / "vfarmers.com".
    """
    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    brand = brand_color or t["accent"]
    second = second_color or brand
    left_fill, left_ink = vivid_block(brand, INK)
    right_fill, right_ink = vivid_block(second, INK)
    emph = adjust_to_contrast(second, WASH, 4.5)
    layers: List[Dict] = []

    layers.append({"type": "ai_generated_background", "url": photo_url, "x": 0, "y": 0, "width": width,
                   "height": height, "fit": "cover", "focus": (0.55, 0.3)})

    # The frame, inset over the photo.
    fx0, fy0, fx1, fy1 = int(width * 0.085), int(height * 0.1), int(width * 0.915), int(height * 0.86)
    pad = fx0 + 46

    pill_h = 112
    pill_y = fy1 - pill_h // 2 + 6
    hw, tr = HEAD_WEIGHT, HEAD_TRACKING
    # A narrow column, as in the reference: the headline stacks into three big lines and
    # leaves room for the description beside the last one. It never starts above the middle of
    # the frame, so it sits on the person's chest, not their face.
    col_w = int(width * 0.6)
    lowest_top = int(height * 0.48)
    for largest in range(116, 63, -4):
        size, lines = fit_headline(headline, headline_font, hw, col_w, 3, largest, 64, tr)
        pitch = int(size * 0.98)
        head_top = pill_y - 44 - pitch * len(lines)
        if head_top >= lowest_top:
            break
    # Description beside the last line when there's room, else under the headline.
    desc_lines: List[str] = []
    desc_size = 30
    last_w = text_width(lines[-1], headline_font, size, hw, tr)
    desc_x = int(pad + last_w + 26)
    desc_w = fx1 - 30 - desc_x
    if description:
        if desc_w >= 250:
            desc_lines = wrap_words(description, BODY_FAMILY, desc_size, 500, desc_w)
            if len(desc_lines) > 4:
                desc_lines = []
        if not desc_lines:
            # Not enough room beside it: keep the headline, drop the description rather than crowd it.
            description = None

    fade_top = max(int(height * 0.28), head_top - 260)
    layers.append({"type": "shape", "shape": "vgradient", "x": 0, "y": fade_top, "width": width,
                   "height": head_top - 10 - fade_top, "color": WASH, "from_alpha": 0, "to_alpha": 210})
    layers.append({"type": "shape", "shape": "vgradient", "x": 0, "y": head_top - 10, "width": width,
                   "height": height - head_top + 10, "color": WASH, "from_alpha": 210, "to_alpha": 240})
    layers.append({"type": "shape", "shape": "rounded_rect", "x": fx0, "y": fy0, "width": fx1 - fx0,
                   "height": fy1 - fy0, "corner_radius": 40, "border_color": "#F4F4F4", "border_width": 2})

    per_line = lines_with_emphasis(lines, emphasis)
    for i, line in enumerate(lines):
        layers.append({"type": "text", "content": line,
                       "spans": emphasis_spans(line, per_line[i], "#FFFFFF", emph),
                       "font_family": headline_font, "font_size": size, "font_weight": hw, "tracking": tr,
                       "color": "#FFFFFF", "x": pad, "y": head_top + i * pitch})
    if desc_lines:
        last_top = head_top + (len(lines) - 1) * pitch
        dy = last_top + int(size * 0.2)
        for j, dl in enumerate(desc_lines):
            layers.append({"type": "text", "content": dl, "font_family": BODY_FAMILY, "font_size": desc_size,
                           "font_weight": 500, "color": "#FFFFFF", "x": desc_x, "y": dy + j * int(desc_size * 1.08),
                           "secondary": True})

    # The two-tone pill: the second colour runs underneath; the brand half overlaps it.
    left_w = 300
    right_text_w = 0
    lead_size, target_size = 30, 48
    if cta_target:
        while text_width(cta_target, headline_font, target_size, 700, -0.02) > 470 and target_size > 34:
            target_size -= 2
        right_text_w = max(text_width(cta_lead or "", BODY_FAMILY, lead_size, 500),
                           text_width(cta_target, headline_font, target_size, 700, -0.02))
    total_w = left_w + (int(right_text_w) + 90 if cta_target else 0)
    px = (width - total_w) // 2
    if cta_target:
        layers.append({"type": "shape", "shape": "rounded_rect", "x": px + left_w - 60, "y": pill_y + 10,
                       "width": total_w - left_w + 60, "height": pill_h - 20, "corner_radius": (pill_h - 20) // 2,
                       "fill_color": right_fill})
        tx = px + left_w + 34
        block_h = (int(lead_size * 1.2) if cta_lead else 0) + target_size
        ty = pill_y + (pill_h - block_h) // 2 - 4
        if cta_lead:
            layers.append({"type": "text", "content": cta_lead, "font_family": BODY_FAMILY, "font_size": lead_size,
                           "font_weight": 500, "color": right_ink, "x": tx, "y": ty, "secondary": True})
            ty += int(lead_size * 1.2)
        layers.append({"type": "text", "content": cta_target, "font_family": headline_font,
                       "font_size": target_size, "font_weight": 700, "tracking": -0.02, "color": right_ink,
                       "x": tx, "y": ty, "secondary": target_size < 42})
    layers.append({"type": "shape", "shape": "rounded_rect", "x": px, "y": pill_y, "width": left_w,
                   "height": pill_h, "corner_radius": pill_h // 2, "fill_color": left_fill})
    if brand_logo_url:
        layers.append({"type": "brand_asset", "url": brand_logo_url, "x": px + 40, "y": pill_y + 20,
                       "width": left_w - 80, "height": pill_h - 40, "tint": left_ink})
    elif brand_name:
        ns = 40
        while text_width(brand_name, headline_font, ns, 700) > left_w - 60 and ns > 28:
            ns -= 2
        layers.append({"type": "text", "content": brand_name, "font_family": headline_font, "font_size": ns,
                       "font_weight": 700, "color": left_ink, "x": px + left_w // 2, "y": pill_y + pill_h // 2,
                       "text_align": "mm", "secondary": ns < 42})

    for z, layer in enumerate(layers, start=1):
        layer["z_index"] = z
    document = {"canvas": {"width": width, "height": height, "background_color": WASH}, "layers": layers}
    assert_legible(document, t)
    return document

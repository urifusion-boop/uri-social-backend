"""
Problems vs Solution — a split page (SEED-101).

Built after the "your problems | our solution" reference: the customer's everyday frustrations
as chat bubbles down the left, the answer on the right with "solution" ringed by hand. Unlike
Problem / Solution (SEED-080), which tells one story in two photos, this one is drawn — fast,
cheap, and it reads like a note rather than an advert.

Two versions:
- With a real product photo (product_photo attestation): the cut-out product stands on the
  right, like the reference.
- Without one: the right side lists what changes for the customer as brand-colour bubbles
  with a tick. No product is ever generated (§1.2 product truthfulness).

The frustrations come from the brand's own pain points or the brief; the outcomes are stated
as results, never as invented numbers or guarantees.
"""
from typing import Dict, List, Optional, Tuple

from app.utils.fonts import text_width
from ._type import adjust_to_contrast, block_with_text, wrap_words
from .legibility import assert_legible
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS, logo_badge_layers

FORMAT = AdFormatDef(
    format_id="SEED-101",
    name="Problems vs Solution",
    asset_source="drawn",
    layers_used="L4",
    brand_mark="optional",
    requires=[],
)

DEFAULT_FAMILY = "Archivo Black"
BODY_FAMILY = "Inter"
SURFACE = "#F4F4F6"
BUBBLE = "#E4E4E8"
PAD = 48
BUBBLE_SIZE = 42


class TooMuchCopy(ValueError):
    pass


def _bubbles(items: List[str], x0: int, col_w: int, top: int, bottom: int, fill: str, ink: str,
             z: int, stagger: bool, prefix: str = "") -> Tuple[List[Dict], int]:
    layers: List[Dict] = []
    blocks = []
    for item in items:
        lines = wrap_words(prefix + item, BODY_FAMILY, BUBBLE_SIZE, 500, col_w - 64)
        w = max(text_width(l, BODY_FAMILY, BUBBLE_SIZE, 500) for l in lines) + 56
        h = len(lines) * 52 + 36
        blocks.append((lines, int(w), h))
    total = sum(h for _, _, h in blocks)
    gap = (bottom - top - total) / max(1, len(blocks))
    if gap < 18:
        raise TooMuchCopy("the bubbles don't fit — use fewer or shorter points")
    y = top + gap / 2
    for i, (lines, w, h) in enumerate(blocks):
        shift = (col_w - w) if (stagger and i % 2) else 0
        x = x0 + max(0, min(shift, col_w - w))
        z += 1
        layers.append({"type": "shape", "z_index": z, "shape": "rounded_rect", "x": x, "y": int(y),
                       "width": w, "height": h, "corner_radius": 28, "fill_color": fill})
        for j, line in enumerate(lines):
            z += 1
            layers.append({"type": "text", "z_index": z, "content": line, "font_family": BODY_FAMILY,
                           "font_size": BUBBLE_SIZE, "font_weight": 500, "color": ink,
                           "x": x + 28, "y": int(y) + 16 + j * 52})
        y += h + gap
    return layers, z


def build_document(
    problems: List[str],
    solutions: Optional[List[str]] = None,
    product_cutout_url: Optional[str] = None,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
    brand_logo_url: Optional[str] = None,
    headline_font: str = DEFAULT_FAMILY,
    labels: Tuple[str, str, str, str] = ("your", "problems", "our", "solution"),
    brand_color: Optional[str] = None,
) -> Dict:
    """
    problems: 3–4 short frustrations in the customer's words ("Skin feels dry by noon").
    solutions: 2–3 outcomes, used when there's no product photo.
    product_cutout_url: the real product with its background removed.
    """
    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    ink = t["ink"]
    brand = brand_color or t["accent"]
    red = adjust_to_contrast(brand, SURFACE, 4.5)
    panel, panel_text = block_with_text(brand, ink)
    half = width // 2
    col_w = half - 2 * PAD
    layers: List[Dict] = []
    z = 0

    z += 1
    layers.append({"type": "shape", "z_index": z, "shape": "line", "x1": half, "y1": 70,
                   "x2": half, "y2": height - 70, "color": "#1A1A1A", "stroke_width": 2})

    # Headings: small dark word over a large brand-colour word, centred in each half.
    for i, (small, big) in enumerate(((labels[0], labels[1]), (labels[2], labels[3]))):
        cx = half // 2 + i * half
        z += 1
        layers.append({"type": "text", "z_index": z, "content": small, "font_family": headline_font,
                       "font_size": 74, "font_weight": 400, "color": ink, "x": cx, "y": 70,
                       "text_align": "ma"})
        big_size = 92
        while text_width(big, headline_font, big_size, 400) > col_w and big_size > 60:
            big_size -= 4
        z += 1
        layers.append({"type": "text", "z_index": z, "content": big, "font_family": headline_font,
                       "font_size": big_size, "font_weight": 400, "color": red, "x": cx, "y": 156,
                       "text_align": "ma"})
        if i == 1:
            w = text_width(big, headline_font, big_size, 400)
            z += 1
            layers.append({"type": "shape", "z_index": z, "shape": "ellipse",
                           "x": int(cx - w / 2 - 30), "y": 150, "width": int(w + 60),
                           "height": big_size + 30, "border_color": ink, "border_width": 4})

    body_top, body_bottom = 300, height - (120 if brand_logo_url else 60)
    bubble_layers, z = _bubbles(problems[:4], PAD, col_w, body_top, body_bottom, BUBBLE, ink, z, stagger=True)
    layers.extend(bubble_layers)

    if product_cutout_url:
        z += 1
        layers.append({"type": "composited_product", "z_index": z, "url": product_cutout_url,
                       "x": half + PAD, "y": body_top + 30, "width": col_w,
                       "height": body_bottom - body_top - 60, "fit": "contain",
                       "shadow": {"color": "#000000", "opacity": 0.18, "blur": 18, "offset_x": 10, "offset_y": 14}})
    else:
        sol_layers, z = _bubbles((solutions or [])[:3], half + PAD, col_w, body_top, body_bottom,
                                 panel, panel_text, z, stagger=False, prefix="✓ ")
        layers.extend(sol_layers)

    badge_layers, z = logo_badge_layers(brand_logo_url, width, height, z)
    layers.extend(badge_layers)

    document = {"canvas": {"width": width, "height": height, "background_color": SURFACE}, "layers": layers}
    assert_legible(document, {**t, "surface": SURFACE, "accent": red})
    return document

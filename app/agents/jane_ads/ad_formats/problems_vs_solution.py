"""
Problems vs Solution — a split page (SEED-101).

Built after the "your problems | our solution" reference: a cool light-grey page split by one
thin rule; "your / problems" and "our / solution" as tight, heavy lowercase headings with the
second word in the brand colour and "solution" ringed by a loose hand-drawn loop; the
customer's frustrations as grey chat bubbles (each with its little tail) staggered down the
left; the answer on the right.

Two versions:
- With a real product photo (product_photo attestation): the cut-out product stands in the
  middle of the right half, like the reference.
- Without one: the right side answers each frustration as a reply bubble in the brand colour.
  No product is ever generated (§1.2 product truthfulness).

The frustrations come from the brand's own pain points or the brief; the outcomes are stated
as results, never as invented numbers or guarantees. Copy that won't fit is set smaller, and
then the last points are dropped — the format never fails just because a list is long.
"""
import math
from typing import Dict, List, Optional, Tuple

from app.utils.fonts import load_font, text_width
from ._type import adjust_to_contrast, block_with_text, wrap_words
from .legibility import assert_legible
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS

FORMAT = AdFormatDef(
    format_id="SEED-101",
    name="Problems vs Solution",
    asset_source="drawn",
    layers_used="L4",
    brand_mark="optional",
    requires=[],
)

DEFAULT_FAMILY = "Inter"
HEAD_WEIGHT = 800
HEAD_TRACKING = -0.05
BODY_FAMILY = "Inter"
SURFACE = "#EDEFF3"
BUBBLE = "#DFE1E6"
INK = "#121214"
RULE = "#2A2A2E"
PAD = 40
MIN_POINTS = 3
MAX_POINTS = 5
# Bubble text sizes tried in turn, largest first; then points are dropped (down to MIN_POINTS).
BUBBLE_SIZES = (32, 30, 28)
BUBBLE_WEIGHT = 400


class TooMuchCopy(ValueError):
    pass


def _layout_bubbles(items: List[str], col_w: int, size: int, top: int, bottom: int):
    """[(lines, w, h)] for each item and the gap between bubbles, or None if they don't fit."""
    pitch = int(size * 1.3)
    blocks = []
    for item in items:
        lines = wrap_words(item, BODY_FAMILY, size, BUBBLE_WEIGHT, col_w - 2 * int(size * 0.8))
        if len(lines) > 2:
            return None
        w = int(max(text_width(l, BODY_FAMILY, size, BUBBLE_WEIGHT) for l in lines)) + 2 * int(size * 0.8)
        h = pitch * len(lines) + int(size * 0.95)
        blocks.append((lines, w, h))
    tail = int(size * 0.6)
    gap = (bottom - top - sum(h + tail for _, _, h in blocks)) / max(1, len(blocks))
    if gap < size * 0.9:
        return None
    return blocks, gap


def _bubble_layers(items: List[str], x0: int, col_w: int, top: int, bottom: int, fill: str, ink: str,
                   pattern: List[str]) -> List[Dict]:
    """Chat bubbles down a column. pattern[i % len] places each one: "in" (indented, tail on its
    left — like the reference's 1st/3rd/5th), "left" (flush left, tail on its right) or "right"
    (flush right, tail on its right)."""
    for count in range(len(items), MIN_POINTS - 1, -1):
        for size in BUBBLE_SIZES:
            fitted = _layout_bubbles(items[:count], col_w, size, top, bottom)
            if fitted:
                break
        if fitted:
            break
    else:
        raise TooMuchCopy("the points don't fit even at the smallest size — use shorter points")
    blocks, gap = fitted
    pitch = int(size * 1.3)
    tail = int(size * 0.6)
    layers: List[Dict] = []
    y = top + gap / 2
    for i, (lines, w, h) in enumerate(blocks):
        place = pattern[i % len(pattern)]
        if place == "in":
            x = x0 + min(int(col_w * 0.12), col_w - w)
        elif place == "right":
            x = x0 + col_w - w
        else:
            x = x0
        yy = int(y)
        layers.append({"type": "shape", "shape": "rounded_rect", "x": x, "y": yy, "width": w, "height": h,
                       "corner_radius": min(h // 2, int(size * 0.9)), "fill_color": fill})
        # The tail: under the bubble, near the side it "comes from".
        if place == "in":
            tx = x + int(size * 1.1)
            pts = [[tx, yy + h - 4], [tx + tail + 6, yy + h - 4], [tx + 4, yy + h + tail]]
        else:
            tx = x + w - int(size * 1.1)
            pts = [[tx - tail - 6, yy + h - 4], [tx, yy + h - 4], [tx - 4, yy + h + tail]]
        layers.append({"type": "shape", "shape": "polygon", "points": pts, "fill_color": fill})
        ty = yy + (h - pitch * len(lines)) // 2 + int(size * 0.08)
        for j, line in enumerate(lines):
            layers.append({"type": "text", "content": line, "font_family": BODY_FAMILY, "font_size": size,
                           "font_weight": BUBBLE_WEIGHT, "color": ink, "x": x + int(size * 0.8), "y": ty + j * pitch,
                           "secondary": True})
        y += h + tail + gap
    return layers


def _ring(cx: float, cy: float, rx: float, ry: float, color: str) -> Dict:
    """A loose, hand-drawn loop: a little more than one turn, slightly tilted and drifting, with
    the pen running past where it started — the reference's ring around "solution"."""
    pts = []
    turns, n = 1.08, 44
    for k in range(n + 1):
        t = k / n
        a = math.radians(-35) + t * turns * 2 * math.pi
        wobble = 1 + 0.035 * math.sin(3 * a + 0.6)
        drift = (t - 0.5) * ry * 0.22
        x = cx + rx * wobble * math.cos(a)
        y = cy + ry * wobble * math.sin(a) + drift
        # a slight tilt
        pts.append([cx + (x - cx) * math.cos(-0.05) - (y - cy) * math.sin(-0.05),
                    cy + (x - cx) * math.sin(-0.05) + (y - cy) * math.cos(-0.05)])
    return {"type": "shape", "shape": "path", "points": pts, "smooth": True, "color": color, "stroke_width": 4}


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
    problems: 3–5 short frustrations in the customer's words ("Skin feels dry by noon").
    solutions: 3–5 outcomes, used when there's no product photo.
    product_cutout_url: the real product with its background removed.
    """
    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    brand = brand_color or t["accent"]
    second = adjust_to_contrast(brand, SURFACE, 4.5)
    reply_fill, reply_ink = block_with_text(brand, INK)
    half = width // 2
    col_w = half - 2 * PAD
    problems = [p.strip() for p in problems if p and p.strip()][:MAX_POINTS]
    if len(problems) < MIN_POINTS:
        raise TooMuchCopy(f"needs at least {MIN_POINTS} problems")
    layers: List[Dict] = []

    rule_bottom = height - (118 if brand_logo_url else 56)
    layers.append({"type": "shape", "shape": "line", "x1": half, "y1": 56, "x2": half, "y2": rule_bottom,
                   "color": RULE, "stroke_width": 2})

    # Headings: the small dark word sits tight on top of the large coloured one, both set
    # heavy with tight letter spacing.
    head_bottom = 0
    hw, tr = HEAD_WEIGHT, HEAD_TRACKING
    for i, (small, big) in enumerate(((labels[0], labels[1]), (labels[2], labels[3]))):
        cx = half // 2 + i * half
        big_size = 112
        while text_width(big, headline_font, big_size, hw, tr) > col_w - (56 if i else 0) and big_size > 64:
            big_size -= 4
        small_size = big_size
        top = 70
        layers.append({"type": "text", "content": small, "font_family": headline_font, "font_size": small_size,
                       "font_weight": hw, "tracking": tr, "color": INK, "x": cx, "y": top, "text_align": "ma"})
        big_y = top + int(small_size * 0.8)
        layers.append({"type": "text", "content": big, "font_family": headline_font, "font_size": big_size,
                       "font_weight": hw, "tracking": tr, "color": second, "x": cx, "y": big_y, "text_align": "ma"})
        head_bottom = max(head_bottom, big_y + int(big_size * 1.15))
        if i == 1:
            # The ring hugs the word's own outline (its inked top and bottom, not the font box).
            w = text_width(big, headline_font, big_size, hw, tr)
            _, g_top, _, g_bottom = load_font(headline_font, big_size, hw).getbbox(big, anchor="ma")
            layers.append(_ring(cx + 4, big_y + (g_top + g_bottom) / 2, w / 2 + 16,
                                (g_bottom - g_top) / 2 + 18, INK))

    body_top, body_bottom = head_bottom + 50, rule_bottom - 16
    layers.extend(_bubble_layers(problems, PAD, col_w, body_top, body_bottom, BUBBLE, INK,
                                 ["in", "left", "in", "right", "in"]))

    if product_cutout_url:
        layers.append({"type": "composited_product", "url": product_cutout_url, "trim": True,
                       "x": half + PAD, "y": body_top + 40, "width": col_w,
                       "height": body_bottom - body_top - 80, "fit": "contain", "align_y": 0.6,
                       "shadow": {"color": "#000000", "opacity": 0.16, "blur": 22, "offset_x": 12, "offset_y": 16}})
    else:
        answers = [s.strip() for s in (solutions or []) if s and s.strip()][:MAX_POINTS]
        if len(answers) < MIN_POINTS:
            raise TooMuchCopy(f"needs at least {MIN_POINTS} outcomes without a product photo")
        layers.extend(_bubble_layers(answers, half + PAD, col_w, body_top, body_bottom, reply_fill, reply_ink,
                                     ["right", "in", "right", "in", "right"]))

    if brand_logo_url:
        layers.append({"type": "brand_asset", "url": brand_logo_url, "x": half - 80, "y": height - 98,
                       "width": 160, "height": 64})

    for z, layer in enumerate(layers, start=1):
        layer["z_index"] = z
    document = {"canvas": {"width": width, "height": height, "background_color": SURFACE}, "layers": layers}
    assert_legible(document, {**t, "surface": SURFACE, "accent": second})
    return document

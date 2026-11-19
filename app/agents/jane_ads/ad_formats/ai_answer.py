"""
AI Answer — the customer's question in an AI chat, and an answer that names the business
(SEED-106).

Built after the "what do I sort out next?" reference: a dark chat screen — menu, a plain title,
the profile and new-chat marks along the top; the customer's own question in a grey bubble on
the right with copy / edit marks under it; the answer below: one bold sentence of genuinely
useful advice, then a plain sentence saying what the business offers; the "Ask anything" bar at
the bottom.

Drawn entirely (asset source `drawn`). It is a GENERIC chat look: no assistant's name, logo,
colours or wording from any real AI product, so it never claims a third party recommends the
business (Meta rejects implied third-party endorsements, and it would mislead). The answer only
states what the business itself offers, in its own words.
"""
from typing import Dict, List, Optional, Tuple

from app.utils.fonts import text_width
from ._type import wrap_words
from .legibility import assert_legible
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS

FORMAT = AdFormatDef(
    format_id="SEED-106",
    name="AI Answer",
    asset_source="drawn",
    layers_used="L4",
    # A logo on the screen would break the look of a real chat — the business is named in the
    # answer instead.
    brand_mark="prohibited",
    requires=[],
)

FAMILY = "Inter"
SCREEN = "#171717"
BUBBLE = "#2F2F2F"
INPUT = "#262626"
TEXT = "#ECECEC"
QUIET = "#C9C9C9"
PAD = 64
TITLE = "AI Chat"


class TooMuchCopy(ValueError):
    pass


def build_document(
    question: str,
    advice: str,
    offer: str,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> Dict:
    """
    question: the customer's question, lowercase and casual as people type it.
    advice: one bold sentence of real, general advice.
    offer: one plain sentence naming the business and what it offers.
    """
    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    layers: List[Dict] = []

    # Top bar: menu, title with its chevron, then the profile and new-chat marks on the right.
    layers.append({"type": "icon", "name": "menu", "x": PAD - 6, "y": 58, "size": 50, "color": TEXT})
    layers.append({"type": "text", "content": TITLE, "font_family": FAMILY, "font_size": 44, "font_weight": 700,
                   "color": TEXT, "x": PAD + 76, "y": 60})
    tw = text_width(TITLE, FAMILY, 44, 700)
    chev_x, chev_y = PAD + 76 + tw + 14, 80
    layers.append({"type": "shape", "shape": "path", "color": QUIET, "stroke_width": 3,
                   "points": [[chev_x, chev_y], [chev_x + 10, chev_y + 10], [chev_x + 20, chev_y]]})
    layers.append({"type": "icon", "name": "user", "x": width - PAD - 150, "y": 56, "size": 54, "color": TEXT})
    layers.append({"type": "icon", "name": "dashed-circle", "x": width - PAD - 54, "y": 56, "size": 54,
                   "color": TEXT})

    size = 42
    pitch = int(size * 1.36)
    bubble_max = int(width * 0.64)
    q_lines = wrap_words(question, FAMILY, size, 400, bubble_max - 2 * 44)
    a_lines = wrap_words(advice, FAMILY, size, 700, width - 2 * PAD)
    o_lines = wrap_words(offer, FAMILY, size, 400, width - 2 * PAD)
    if len(q_lines) > 3 or len(a_lines) > 4 or len(o_lines) > 3:
        raise TooMuchCopy("the question or answer is too long for one screen")

    # The question bubble, right-aligned.
    bw = int(max(text_width(l, FAMILY, size, 400) for l in q_lines)) + 2 * 44
    bh = pitch * len(q_lines) + 2 * 30
    bx, by = width - PAD + 10 - bw, 168
    layers.append({"type": "shape", "shape": "rounded_rect", "x": bx, "y": by, "width": bw, "height": bh,
                   "corner_radius": min(48, bh // 2), "fill_color": BUBBLE})
    for i, line in enumerate(q_lines):
        layers.append({"type": "text", "content": line, "font_family": FAMILY, "font_size": size,
                       "font_weight": 400, "color": TEXT, "x": bx + 44, "y": by + 26 + i * pitch})
    # copy / edit marks under the bubble
    my = by + bh + 22
    layers.append({"type": "icon", "name": "copy", "x": width - PAD - 120, "y": my, "size": 40, "color": QUIET})
    layers.append({"type": "icon", "name": "pencil", "x": width - PAD - 48, "y": my, "size": 40, "color": QUIET})

    # The answer: the bold advice, then the plain sentence naming the business.
    ay = my + 40 + 46
    for i, line in enumerate(a_lines):
        layers.append({"type": "text", "content": line, "font_family": FAMILY, "font_size": size,
                       "font_weight": 700, "tracking": -0.01, "color": "#FFFFFF", "x": PAD - 6, "y": ay + i * pitch})
    oy = ay + pitch * len(a_lines) + 34
    for i, line in enumerate(o_lines):
        layers.append({"type": "text", "content": line, "font_family": FAMILY, "font_size": size,
                       "font_weight": 400, "color": TEXT, "x": PAD - 6, "y": oy + i * pitch})

    # The "Ask anything" bar.
    bar_h = 112
    bar_y = height - 40 - bar_h
    if oy + pitch * len(o_lines) > bar_y - 20:
        raise TooMuchCopy("the answer runs into the input bar")
    layers.append({"type": "shape", "shape": "rounded_rect", "x": 36, "y": bar_y, "width": width - 72,
                   "height": bar_h, "corner_radius": bar_h // 2, "fill_color": INPUT})
    layers.append({"type": "icon", "name": "plus", "x": 70, "y": bar_y + (bar_h - 48) // 2, "size": 48,
                   "color": TEXT})
    layers.append({"type": "text", "content": "Ask anything", "font_family": FAMILY, "font_size": 40,
                   "font_weight": 400, "color": QUIET, "x": 156, "y": bar_y + bar_h // 2, "text_align": "lm",
                   "secondary": True})
    layers.append({"type": "icon", "name": "mic", "x": width - 246, "y": bar_y + (bar_h - 46) // 2, "size": 46,
                   "color": TEXT})
    cr = 42
    ccx, ccy = width - 36 - 22 - cr, bar_y + bar_h // 2
    layers.append({"type": "shape", "shape": "ellipse", "x": ccx - cr, "y": ccy - cr, "width": 2 * cr,
                   "height": 2 * cr, "fill_color": "#FFFFFF"})
    layers.append({"type": "icon", "name": "waveform", "x": ccx - 22, "y": ccy - 22, "size": 44, "color": "#111111"})

    for z, layer in enumerate(layers, start=1):
        layer["z_index"] = z
    document = {"canvas": {"width": width, "height": height, "background_color": SCREEN}, "layers": layers}
    assert_legible(document, {**t, "surface": SCREEN})
    return document

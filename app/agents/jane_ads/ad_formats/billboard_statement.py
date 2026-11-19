"""
Billboard Statement — one bold line on a billboard against the sky (SEED-105).

Built after the "Posting is not Marketing" reference: a tall roadside billboard on its steel
frame (lamps on top, a catwalk below) stands against a blue sky with soft clouds; its face is the
brand colour, and on it a big, tight statement in three lines with the middle phrase on a solid
highlight block; under it a short line with its last words in the highlight colour.

The photo comes from the image model with the billboard face left completely blank; find_panel()
locates that face in the picture and the words are set on it by our own renderer — so every word
is the business's, spelled exactly, and nothing is painted by the model. If the face can't be
found cleanly the format isn't used (the orchestrator moves on).
"""
from typing import Dict, List, Optional, Tuple

from PIL import Image

from app.utils.fonts import text_width
from ._type import adjust_to_contrast, block_with_text, emphasis_spans, fit_headline, lines_with_emphasis, wrap_words
from .brand_tokens import contrast_ratio
from .legibility import assert_legible
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS

FORMAT = AdFormatDef(
    format_id="SEED-105",
    name="Billboard Statement",
    asset_source="generate",
    layers_used="L2-L4",
    brand_mark="optional",
    requires=[],
)

DEFAULT_FAMILY = "Saira Condensed"
HEAD_WEIGHT = 700
HEAD_TRACKING = -0.01
BODY_FAMILY = "Inter"
# The highlight block's colour when the brand has no second colour that stands out on its panel.
HIGHLIGHT_FALLBACKS = ("#E3262D", "#FFC93C", "#111111", "#FFFFFF")


def scene_prompt(panel_hex: str) -> str:
    return (
        "A photograph looking slightly up at a single tall, portrait-shaped roadside billboard on a "
        "dark steel frame, standing against a clear bright blue sky with a few soft white clouds. "
        "The billboard is seen straight on, centred, its face filling the middle of the frame — "
        f"about 55% of the image width and 65% of its height. The face is ONE completely flat, "
        f"evenly lit, solid colour ({panel_hex}), perfectly blank: no text, no letters, no logo, no "
        "image, no pattern, no frame lines on it. Three small lamps on thin arms sit above the top "
        "edge, entirely above the face. A metal catwalk with railings runs under the bottom edge and "
        "a single thick pole goes down out of the frame; a steel ladder structure shows down the "
        "left side. Crisp daylight, realistic advertising photography. No text anywhere in the "
        "image."
    )


def find_panel(image: Image.Image, expected_hex: str) -> Optional[Tuple[Tuple[int, int, int, int], str]]:
    """((x0, y0, x1, y1), measured colour) of the billboard's blank face, or None.

    The face is the large, flat, near-uniform area at the centre of the picture. Its colour is
    taken from the centre itself (the model never paints the brand colour exactly); the box is
    the run of rows and columns where that colour dominates. None unless the result is a large,
    solid, roughly rectangular area near the expected colour — a cluttered or wrongly coloured
    face isn't used.
    """
    im = image.convert("RGB")
    w, h = im.size
    small = im.resize((200, max(1, int(200 * h / w))))
    sw, sh = small.size
    px = small.load()
    cx, cy = sw // 2, sh // 2
    patch = [px[x, y] for x in range(cx - 6, cx + 7) for y in range(cy - 6, cy + 7)]
    centre = tuple(sorted(c[i] for c in patch)[len(patch) // 2] for i in range(3))
    exp = tuple(int(expected_hex.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    if sum((a - b) ** 2 for a, b in zip(centre, exp)) ** 0.5 > 150:
        return None

    def close(c) -> bool:
        return sum((a - b) ** 2 for a, b in zip(c, centre)) ** 0.5 < 46

    rows = [sum(1 for x in range(sw) if close(px[x, y])) / sw for y in range(sh)]
    y0 = y1 = cy
    while y0 > 0 and rows[y0 - 1] > 0.22:
        y0 -= 1
    while y1 < sh - 1 and rows[y1 + 1] > 0.22:
        y1 += 1
    cols = [sum(1 for y in range(y0, y1 + 1) if close(px[x, y])) / max(1, y1 - y0 + 1) for x in range(sw)]
    x0 = x1 = cx
    while x0 > 0 and cols[x0 - 1] > 0.5:
        x0 -= 1
    while x1 < sw - 1 and cols[x1 + 1] > 0.5:
        x1 += 1
    area = (x1 - x0 + 1) * (y1 - y0 + 1)
    if area < 0.18 * sw * sh or (x1 - x0) < 0.35 * sw or (y1 - y0) < 0.35 * sh:
        return None
    filled = sum(1 for x in range(x0, x1 + 1) for y in range(y0, y1 + 1) if close(px[x, y])) / area
    if filled < 0.86:
        return None
    k = w / sw
    box = (int((x0 + 0.5) * k), int((y0 + 0.5) * k), int((x1 + 0.5) * k), int((y1 + 0.5) * k))
    return box, "#{:02X}{:02X}{:02X}".format(*centre)


def highlight_colour(panel: str, second: Optional[str]) -> Tuple[str, str]:
    """(block fill, text colour on it) for the highlighted phrase: the brand's second colour when
    it stands out on the panel, else red, yellow, black or white — the first that stands out."""
    for fill in ([second] if second else []) + list(HIGHLIGHT_FALLBACKS):
        if contrast_ratio(fill, panel) < 1.6:
            continue  # wouldn't stand out on the face
        ink = "#FFFFFF" if contrast_ratio("#FFFFFF", fill) >= contrast_ratio("#111111", fill) else "#111111"
        # 4.5:1, not the 7:1 body floor: the highlighted words are headline-size display type
        # (54px+), the same bar the accent role uses.
        if contrast_ratio(ink, fill) >= 4.5:
            return fill, ink
    return ("#111111", "#FFFFFF") if contrast_ratio("#111111", panel) >= contrast_ratio("#FFFFFF", panel) \
        else ("#FFFFFF", "#111111")

def _statement_lines(statement: str, highlight: Optional[str], family: str, weight: int, tracking: float,
                     max_w: int, largest: int) -> Tuple[int, List[str]]:
    """The statement in at most three lines, the highlighted phrase on a line of its own when it
    fits ("Posting" / "is not" / "Marketing"), as large as the face allows."""
    i = statement.lower().find((highlight or "").lower()) if highlight else -1
    if i >= 0:
        parts = [statement[:i].strip(), statement[i:i + len(highlight)].strip(), statement[i + len(highlight):].strip()]
        parts = [p for p in parts if p]
        if len(parts) <= 3:
            size = largest
            while size >= 54:
                if all(text_width(p, family, size, weight, tracking) <= max_w for p in parts):
                    return size, parts
                size -= 4
    return fit_headline(statement, family, weight, max_w, 3, largest, 54, tracking)


def build_document(
    scene_url: str,
    panel_box: Tuple[int, int, int, int],
    panel_color: str,
    statement: str,
    highlight: Optional[str],
    subline: Optional[str],
    sub_emphasis: Optional[str],
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
    website: Optional[str] = None,
    headline_font: str = DEFAULT_FAMILY,
    second_color: Optional[str] = None,
) -> Dict:
    """
    panel_box: the billboard face in canvas pixels (from find_panel, scaled to the canvas).
    statement: "Posting is not Marketing" (<= 3 lines).
    highlight: the phrase on the highlight block ("is not") — must appear in the statement.
    subline: "Take your business to the next level." or None; sub_emphasis its coloured end.
    website: shown at the bottom of the face, or None.
    """
    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    x0, y0, x1, y1 = panel_box
    pw, ph = x1 - x0, y1 - y0
    # The face is repainted in its own colour, deepened (same hue) only as far as the words
    # need to read at 7:1.
    measured = panel_color
    panel_color, ink = block_with_text(panel_color, "#111111")
    hl_fill, hl_ink = highlight_colour(panel_color, second_color)
    sub_accent = hl_fill if contrast_ratio(hl_fill, panel_color) >= 4.5 else panel_color
    layers: List[Dict] = []

    # The photo, with the face (and whatever of it shows between the railings) shifted to the
    # checked colour; then a flat field of that colour under the words themselves — the solid
    # backing §1.6 requires, and a clean cover over any mark the model left on the face.
    pad_box = (x0 - 6, y0 - 6, x1 + 6, y1 + int(ph * 0.1))
    layers.append({"type": "ai_generated_background", "url": scene_url, "x": 0, "y": 0, "width": width,
                   "height": height,
                   "recolor": {"from": measured, "to": panel_color, "box": pad_box, "tolerance": 44}})
    layers.append({"type": "shape", "shape": "rect", "x": x0 + 4, "y": y0 + 4, "width": pw - 8, "height": ph - 8,
                   "fill_color": panel_color})

    inner = int(pw * 0.1)
    hw, tr = HEAD_WEIGHT, HEAD_TRACKING
    # The usable face: clear of the top edge, and of the bottom tenth that the catwalk railing
    # usually hides; the website (if any) takes the last line of it.
    ws = 30
    face_top, face_bottom = y0 + int(ph * 0.1), y1 - int(ph * 0.12)
    if website:
        while text_width(website, BODY_FAMILY, ws, 600) > pw - 2 * inner and ws > 28:
            ws -= 1
        face_bottom -= ws + int(ph * 0.04)
    largest = int(ph * 0.22)
    while True:
        size, lines = _statement_lines(statement, highlight, headline_font, hw, tr, pw - 2 * inner, largest)
        pitch = int(size * 1.1)
        sub_size = max(30, min(48, int(size * 0.36)))
        sub_lines = wrap_words(subline, BODY_FAMILY, sub_size, 600, pw - 2 * inner)[:2] if subline else []
        block_h = pitch * len(lines) + (int(sub_size * 1.2) * len(sub_lines) + int(size * 0.45) if sub_lines else 0)
        if block_h <= face_bottom - face_top or size <= 54:
            break
        largest = size - 4
    if block_h > face_bottom - face_top:
        raise ValueError("the statement doesn't fit on this billboard face")
    top = face_top + (face_bottom - face_top - block_h) // 2
    per_line = lines_with_emphasis(lines, highlight)
    for i, line in enumerate(lines):
        spans = emphasis_spans(line, per_line[i], ink, hl_fill, highlight=True)
        if per_line[i]:
            for sp in spans:
                if sp.get("highlight"):
                    sp["color"] = hl_ink
        layers.append({"type": "text", "content": line, "spans": spans, "font_family": headline_font,
                       "font_size": size, "font_weight": hw, "tracking": tr, "color": ink,
                       "x": x0 + inner, "y": top + i * pitch})
    sy = top + pitch * len(lines) + int(size * 0.45)
    sub_per = lines_with_emphasis(sub_lines, sub_emphasis)
    as_block = contrast_ratio(hl_fill, panel_color) < 4.5
    for j, sl in enumerate(sub_lines):
        spans = emphasis_spans(sl, sub_per[j], ink, hl_fill if as_block else hl_fill, highlight=as_block)
        if as_block:
            for sp in spans:
                if sp.get("highlight"):
                    sp["color"] = hl_ink
        layers.append({"type": "text", "content": sl, "spans": spans,
                       "font_family": BODY_FAMILY, "font_size": sub_size, "font_weight": 600, "tracking": -0.01,
                       "color": ink, "x": x0 + inner, "y": sy + j * int(sub_size * 1.2),
                       "secondary": sub_size < 42})
    if website:
        layers.append({"type": "text", "content": website, "font_family": BODY_FAMILY, "font_size": ws,
                       "font_weight": 600, "color": ink, "x": x0 + inner, "y": face_bottom + int(ph * 0.04),
                       "secondary": True})

    for z, layer in enumerate(layers, start=1):
        layer["z_index"] = z
    document = {"canvas": {"width": width, "height": height, "background_color": "#4A8FE0"}, "layers": layers}
    assert_legible(document, {**t, "surface": panel_color, "accent": sub_accent})
    return document

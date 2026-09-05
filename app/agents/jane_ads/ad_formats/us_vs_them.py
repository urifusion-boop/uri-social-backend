"""
Us vs Them — VSG-01 v3 §2.4 (SEED-075).

"Displacing an established habit or method." Two columns, equal width,
shared row labels, `edge` rules between rows. Left column is the displaced
METHOD in `ink-quiet` on `surface`; right is the offer in `ink` on `field`.
Identical row labels both sides or it is not a comparison.

Hard checks — strictest in the library. The left column names a method,
never an identified business; the field must reject brand names at input
rather than relying on Jane or the user to self-censor (SEED-050). Safe
local comparisons: buying at the market vs delivered to you; generator vs
solar; notebook vs system; guesswork vs measured fitting.
"""
import re
from typing import Dict, List, Tuple

from ._text_metrics import wrap_text
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS
from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService

FORMAT = AdFormatDef(
    format_id="SEED-075",
    name="Us vs Them",
    asset_source="drawn",
    layers_used="L4",
    requires=[],
)

# §1.6 floors — retrofitted after legibility.py's automated check found the
# original 20/28/34px sizes here all below the 42px minimum (this format
# predates that discipline; Borrowed Interface/Review Card/Day1→Day30 were
# built with it from the start). Bumping font size means row/us values also
# need real word-wrapping now (wrap_text, the same helper Borrowed
# Interface/Review Card use) — unwrapped text was already a latent overflow
# risk at the old smaller sizes and gets materially worse at 44px.
_FONT_HEADER = 44
_FONT_LABEL = 42
_FONT_VALUE = 44


class BrandNameRejected(ValueError):
    """Raised when the left ("them") column looks like it names a specific
    business rather than a generic method. This is best-effort, defense-in-
    depth — a heuristic, not a brand-name database — matching VSG-01's own
    framing: retrieval-time corpus curation (§6, the actual named-brand-
    input-field design) is the real control; this just refuses to render a
    document that got past it with something obviously wrong, rather than
    silently rendering a named competitor's name into an ad."""
    pass


# 2+ consecutive "brand-shaped" words, anywhere including the very start,
# reads as a proper noun ("Jumia Food", "Chicken Republic", "Coca-Cola
# Nigeria", "MTN Nigeria", "Domino's Pizza") — a genuine method description
# ("buying at the market", "guesswork vs measured fitting") is lowercase
# prose, brand name or not. A "brand-shaped" word is either Title-Case
# (letters plus an internal apostrophe, so "Domino's" counts as one word,
# not broken by the ') or an all-caps acronym of 2+ letters (MTN, GTB, UBA —
# common in Nigerian brand names and otherwise invisible to a Title-Case-
# only check). Deliberately NOT excluding position 0 — that's exactly where
# most real brand names start, and excluding it would let the single most
# common shape of brand name straight through. False positive: a method
# description someone happens to Title-Case for style. False negative: a
# single-word brand ("Uber", "Bolt"). Not a substitute for retrieval-time
# curation — only a last-resort refusal.
_BRAND_WORD = r"(?:[A-Z][a-z']+|[A-Z]{2,})"
_TITLE_CASE_RUN = re.compile(rf"(?:{_BRAND_WORD}\s+){{1,}}{_BRAND_WORD}")
_TRADEMARK_MARK = re.compile(r"[™®©]")


def _looks_like_a_brand_name(text: str) -> bool:
    if _TRADEMARK_MARK.search(text):
        return True
    return bool(_TITLE_CASE_RUN.search(text))


def build_document(
    rows: List[Tuple[str, str, str]],
    them_label: str = "The old way",
    us_label: str = "With us",
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> Dict:
    """
    rows: [(row_label, them_value, us_value), ...] — row_label is shared
    across both columns ("Delivery", "Price", "Setup time"...); them_value
    must name a METHOD, never a business — raises BrandNameRejected if it
    looks like one.
    """
    for _, them_value, _us in rows:
        if _looks_like_a_brand_name(them_value):
            raise BrandNameRejected(
                f"'{them_value}' looks like a named business, not a method — "
                "Us vs Them can only compare against a generic method (VSG-01 §2.4)"
            )

    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    layers = []
    z = 0

    col_gap = 16
    col_width = (width - 144 - col_gap) // 2
    left_x = 72
    right_x = left_x + col_width + col_gap
    header_y = 72
    rows_top = header_y + 96

    label_height = int(_FONT_LABEL * 1.2)
    label_gap = 8
    value_line_height = int(_FONT_VALUE * 1.3)
    row_gap_after = 32

    # Column headers.
    for label, x in ((them_label, left_x), (us_label, right_x)):
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": label,
            "x": x, "y": header_y, "font_size": _FONT_HEADER, "font_weight": 700, "color": t["ink"],
        })

    # Pre-measure every row's wrapped content so both columns can share one
    # row height each while still fitting whichever side wraps to more
    # lines — a fixed row_height (the pre-wrap design) silently overflowed
    # once font size grew, the identical bug class Borrowed Interface's
    # chat bubbles had before they got the same treatment.
    measured_rows = []
    for row_label, them_value, us_value in rows:
        them_lines = wrap_text(them_value, col_width, _FONT_VALUE)
        us_lines = wrap_text(us_value, col_width, _FONT_VALUE)
        n_lines = max(len(them_lines), len(us_lines))
        row_height = label_height + label_gap + n_lines * value_line_height + row_gap_after
        measured_rows.append((row_label, them_lines, us_lines, row_height))

    # Column backgrounds (fields the row content sits on) — left on
    # `surface` (already the canvas colour, so no separate fill needed),
    # right on `field`, running the full height of the rows.
    total_rows_height = sum(row_height for *_, row_height in measured_rows)
    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "rect",
        "x": right_x - 24, "y": rows_top - 16, "width": col_width + 48, "height": total_rows_height + 16,
        "fill_color": t["field"],
    })

    row_y = rows_top
    for i, (row_label, them_lines, us_lines, row_height) in enumerate(measured_rows):
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": row_label,
            "x": left_x, "y": row_y, "font_size": _FONT_LABEL, "color": t["ink-quiet"],
        })

        value_y = row_y + label_height + label_gap
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": "\n".join(them_lines),
            "x": left_x, "y": value_y, "font_size": _FONT_VALUE, "color": t["ink-quiet"],
        })

        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": row_label,
            "x": right_x, "y": row_y, "font_size": _FONT_LABEL, "color": t["ink-quiet"],
        })

        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": "\n".join(us_lines),
            "x": right_x, "y": value_y, "font_size": _FONT_VALUE, "font_weight": 700, "color": t["ink"],
        })

        if i > 0:
            z += 1
            layers.append({
                "type": "shape", "z_index": z, "shape": "line",
                "x1": left_x, "y1": row_y - 16, "x2": left_x + col_width, "y2": row_y - 16,
                "color": t["edge"], "stroke_width": 2,
            })

        row_y += row_height

    return {
        "canvas": {"width": width, "height": height, "background_color": t["surface"]},
        "layers": layers,
    }


async def render(*args, **kwargs) -> bytes:
    document = build_document(*args, **kwargs)
    return await DocumentRendererService.render_to_png(document)

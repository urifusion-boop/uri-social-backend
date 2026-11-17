"""
Event Promo — webinars, classes, challenges, launches, workshops (SEED-099).

The poster a coach, school or community runs for a dated event: a huge stacked title, what
kind of event it is, the date on a pill, who's hosting, and one action. Built after the
"Master Your Money Mindset — 5 Days WhatsApp Challenge" reference.

Two versions:
- With the host's own photo (owner_photo attestation): the photo is cut out and stands on
  a block of the brand colour on the right, like the reference.
- Without a photo: the right side becomes a large date card in the brand colour. A generated
  "host" would be a person who isn't running the event, so a host is never generated.

Truthfulness: the event, its date and the host are always the business's own words (the
orchestrator only fills this format when the brief states a dated event). Nothing here
invents a date, price or speaker.
"""
from typing import Dict, List, Optional, Tuple

from ._type import adjust_to_contrast, block_with_text, fit_headline, wrap_words
from .legibility import assert_legible
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS, logo_badge_layers

FORMAT = AdFormatDef(
    format_id="SEED-099",
    name="Event Promo",
    asset_source="drawn",
    layers_used="L4",
    brand_mark="optional",
    requires=[],  # the photo version is chosen when an owner photo is supplied
)

DEFAULT_FAMILY = "Montserrat"
BODY_FAMILY = "Inter"
PAD = 64


def build_document(*args, **kwargs) -> Dict:
    """Lays the poster out at the largest title size whose column still fits the canvas
    (a long title, a host line and a button all compete for the same left column)."""
    last: Optional[Exception] = None
    for cap in range(120, 51, -8):
        try:
            return _build(*args, title_cap=cap, **kwargs)
        except (EventPromoOverflow, ValueError) as e:  # ValueError: title won't wrap at this size
            last = e
    raise EventPromoOverflow(str(last))


def _build(
    title: str,
    event_kind: str,
    date_text: str,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
    host_cutout_url: Optional[str] = None,
    host_photo_url: Optional[str] = None,
    host_name: Optional[str] = None,
    host_role: Optional[str] = None,
    action_line: Optional[str] = None,
    contact_line: Optional[str] = None,
    brand_logo_url: Optional[str] = None,
    headline_font: str = DEFAULT_FAMILY,
    brand_color: Optional[str] = None,
    title_cap: int = 120,
) -> Dict:
    """
    title: the event's name ("Master your money mindset"), drawn uppercase and stacked.
    event_kind: what it is ("5-Day WhatsApp Challenge", "Free live webinar").
    date_text: the date exactly as the business gave it ("19th – 23rd February").
    host_cutout_url: the host's photo with its background removed (preferred);
    host_photo_url: the original photo, used framed if no cut-out could be made.
    """
    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    surface = "#FFFFFF"
    ink = t["ink"]
    panel, panel_text = block_with_text(brand_color or t["accent"], t["ink"])
    has_photo = bool(host_cutout_url or host_photo_url)
    col_w = int(width * (0.52 if has_photo else 0.54)) - PAD
    layers: List[Dict] = []
    z = 0

    # Right side: the brand-colour block the host stands on, or the date card.
    if has_photo:
        z += 1
        layers.append({"type": "shape", "z_index": z, "shape": "ellipse",
                       "x": int(width * 0.55), "y": int(height * 0.12),
                       "width": int(width * 0.78), "height": int(width * 0.78), "fill_color": panel})
        z += 1
        if host_cutout_url:
            layers.append({"type": "composited_product", "z_index": z, "url": host_cutout_url,
                           "x": int(width * 0.52), "y": int(height * 0.22),
                           "width": int(width * 0.48), "height": int(height * 0.78), "fit": "contain"})
        else:
            layers.append({"type": "ai_generated_background", "z_index": z, "url": host_photo_url,
                           "x": int(width * 0.58), "y": int(height * 0.24),
                           "width": int(width * 0.36), "height": int(height * 0.52), "fit": "cover",
                           "focus": (0.5, 0.3)})
    else:
        card_x, card_y = int(width * 0.60), int(height * 0.16)
        card_w = width - card_x - PAD + 20
        date_size, date_lines = _fit_date_card(date_text, headline_font, card_w - 56)
        date_pitch = int(date_size * 1.22)
        card_h = 120 + len(date_lines) * date_pitch
        z += 1
        layers.append({"type": "shape", "z_index": z, "shape": "rounded_rect", "x": card_x, "y": card_y,
                       "width": card_w, "height": card_h, "corner_radius": 36, "fill_color": panel})
        for i, line in enumerate(date_lines):
            z += 1
            layers.append({"type": "text", "z_index": z, "content": line, "font_family": headline_font,
                           "font_size": date_size, "font_weight": 900, "color": panel_text,
                           "x": card_x + card_w // 2, "y": card_y + 60 + i * date_pitch, "text_align": "ma"})

    # Left column: title, kind of event, date pill, host, action.
    title_size, title_lines = fit_headline(title.upper(), headline_font, 900, col_w, 4, title_cap, 52)
    y = PAD + (90 if brand_logo_url else 30)
    pitch = int(title_size * 1.02)
    for line in title_lines:
        z += 1
        layers.append({"type": "text", "z_index": z, "content": line, "font_family": headline_font,
                       "font_size": title_size, "font_weight": 900, "color": ink, "x": PAD, "y": y})
        y += pitch
    y += 26

    kind_lines = wrap_words(event_kind, BODY_FAMILY, 46, 700, col_w - 24)[:2]
    z += 1
    layers.append({"type": "shape", "z_index": z, "shape": "rect", "x": PAD, "y": y + 4,
                   "width": 10, "height": len(kind_lines) * 56 - 8, "fill_color": panel})
    for line in kind_lines:
        z += 1
        layers.append({"type": "text", "z_index": z, "content": line, "font_family": BODY_FAMILY,
                       "font_size": 46, "font_weight": 700, "color": ink, "x": PAD + 28, "y": y})
        y += 56
    y += 28

    if has_photo:  # the date card carries the date in the no-photo version
        pill_w = min(col_w, int(_width(date_text, 44, 800)) + 72)
        z += 1
        layers.append({"type": "shape", "z_index": z, "shape": "rounded_rect", "x": PAD, "y": y,
                       "width": pill_w, "height": 84, "corner_radius": 42, "fill_color": panel})
        z += 1
        layers.append({"type": "text", "z_index": z, "content": date_text, "font_family": BODY_FAMILY,
                       "font_size": 44, "font_weight": 800, "color": panel_text,
                       "x": PAD + pill_w // 2, "y": y + 16, "text_align": "ma"})
        y += 84 + 30

    if host_name:
        z += 1
        layers.append({"type": "text", "z_index": z, "content": host_name, "font_family": BODY_FAMILY,
                       "font_size": 44, "font_weight": 700, "color": ink, "x": PAD, "y": y})
        y += 54
        if host_role:
            z += 1
            layers.append({"type": "text", "z_index": z, "content": host_role, "font_family": BODY_FAMILY,
                           "font_size": 42, "font_weight": 500, "color": t["ink-quiet"], "x": PAD, "y": y})
            y += 54

    if action_line:
        btn_w = min(col_w, int(_width(action_line, 44, 800)) + 96)
        # With a photo the button anchors the bottom; without one the details are short, so the
        # button follows them and the whole poster is centred below (no empty band mid-canvas).
        btn_y = (y + 36) if not has_photo else max(y + 20, height - PAD - 92 - (60 if contact_line else 0))
        if btn_y + 92 > height - PAD - (60 if contact_line else 0) + 4:
            raise EventPromoOverflow("the action button doesn't fit under the event details")
        z += 1
        layers.append({"type": "shape", "z_index": z, "shape": "rounded_rect", "x": PAD, "y": btn_y,
                       "width": btn_w, "height": 92, "corner_radius": 46, "fill_color": ink})
        z += 1
        layers.append({"type": "text", "z_index": z, "content": action_line, "font_family": BODY_FAMILY,
                       "font_size": 44, "font_weight": 800, "color": "#FFFFFF",
                       "x": PAD + btn_w // 2, "y": btn_y + 20, "text_align": "ma"})
    if contact_line:
        z += 1
        layers.append({"type": "text", "z_index": z, "content": contact_line, "font_family": BODY_FAMILY,
                       "font_size": 42, "font_weight": 600,
                       "color": adjust_to_contrast(t["ink-quiet"], surface, 7.0),
                       "x": PAD, "y": (btn_y + 92 + 28) if (action_line and not has_photo) else height - PAD - 46})

    if not has_photo:
        _centre_vertically(layers, top=PAD + (70 if brand_logo_url else 0), bottom=height - PAD)
    if brand_logo_url:
        z += 1
        layers.append({"type": "brand_asset", "z_index": z, "url": brand_logo_url,
                       "x": PAD, "y": PAD - 20, "width": 160, "height": 64})

    document = {"canvas": {"width": width, "height": height, "background_color": surface}, "layers": layers}
    _check_vertical_fit(layers, height)
    assert_legible(document, {**t, "surface": surface})
    return document


def _centre_vertically(layers: List[Dict], top: int, bottom: int) -> None:
    """Move the whole poster down so its content sits centred between `top` and `bottom`."""
    def extent(l):
        return l["y"] + (l.get("height") or l.get("font_size", 48))
    used_top = min(l["y"] for l in layers if "y" in l)
    used_bottom = max(extent(l) for l in layers if "y" in l)
    shift = int(((bottom - top) - (used_bottom - used_top)) / 2 + top - used_top)
    if shift > 0:
        for l in layers:
            if "y" in l:
                l["y"] += shift


def _fit_date_card(date_text: str, family: str, max_width: int) -> Tuple[int, List[str]]:
    """The date on the card: the time (after the last comma) on its own line, and the largest
    size at which every line — including a single long word like 'DECEMBER' — fits the card."""
    from app.utils.fonts import text_width
    head, _, tail = date_text.rpartition(",")
    parts = [head.strip(), tail.strip()] if head and tail.strip() else [date_text.strip()]
    for size in range(64, 43, -4):
        lines = [l for part in parts for l in wrap_words(part.upper(), family, size, 900, max_width)]
        if len(lines) <= 5 and all(text_width(l, family, size, 900) <= max_width for l in lines):
            return size, lines
    raise EventPromoOverflow("the date doesn't fit its card")


def _width(text: str, size: int, weight: int) -> float:
    from app.utils.fonts import text_width
    return text_width(text, BODY_FAMILY, size, weight)


class EventPromoOverflow(ValueError):
    pass


def _check_vertical_fit(layers: List[Dict], height: int) -> None:
    """Too much copy pushes the left column into the action button or off the canvas —
    refuse rather than ship overlapping text."""
    texts = [l for l in layers if l.get("type") == "text"]
    bottom = max((l["y"] + l.get("font_size", 48) for l in texts), default=0)
    if bottom > height - 20:
        raise EventPromoOverflow("event copy runs off the canvas — shorten the title or event line")

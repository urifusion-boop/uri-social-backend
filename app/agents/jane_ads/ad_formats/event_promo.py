"""
Event Promo — webinars, classes, challenges, launches, workshops (SEED-099).

The poster a coach, school or community runs for a dated event, built after the "Master Your
Money Mindset — 5 Days WhatsApp Challenge" reference: a soft brand-colour arc on the right, a
stacked title in mixed weights ("MASTER your MONEY MINDSET"), the kind of event (with a big
numeral when it has one — "5 Days WhatsApp Challenge"), the date on a pill with a calendar mark,
the host, one action, the link, and a faint oversized word from the title along the bottom.

Two versions:
- With the host's own photo (owner_photo attestation): the photo is cut out and stands on the
  arc, like the reference.
- Without a photo: the arc holds a date card (weekday, big day, month, time). A generated "host"
  would be a person who isn't running the event, so a host is never generated.

Truthfulness: the event, its date and the host are always the business's own words (the
orchestrator only fills this format when the brief states a dated event). Nothing here invents a
date, price or speaker — the date card only re-arranges the date text it was given.
"""
import re
from typing import Dict, List, Optional, Tuple

from app.utils.fonts import spans_width, text_width
from ._type import adjust_to_contrast, block_with_text, wrap_words
from .legibility import assert_legible
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS

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
SURFACE = "#FBFAF8"
INK = "#141014"
QUIET = "#4A4245"

# Words drawn smaller and lighter inside the title, as in "MASTER your MONEY MINDSET".
SMALL_WORDS = {
    "a", "an", "and", "at", "by", "for", "from", "in", "into", "of", "on", "or", "our", "the", "to",
    "with", "your", "my", "is", "it", "its", "you", "we",
}
MONTHS = (
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december",
)
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class EventPromoOverflow(ValueError):
    pass


def build_document(*args, **kwargs) -> Dict:
    """Lays the poster out at the largest title size whose column still fits the canvas."""
    last: Optional[Exception] = None
    for cap in range(120, 51, -6):
        try:
            return _build(*args, title_cap=cap, **kwargs)
        except (EventPromoOverflow, ValueError) as e:
            last = e
    raise EventPromoOverflow(str(last))


def scene_prompt(title: str, event_kind: str, brand_color: str) -> str:
    """The artwork for the no-photo version (gpt-image-2): objects that stand for the event's
    topic on a soft disc in the brand colour, all on the right, the left half left calm for
    the text. No people — a generated person would be a host who isn't running the event —
    and no text, which is drawn by us."""
    return (
        f"A refined, premium advertising background for an event: \"{title}\" ({event_kind}). "
        "On the RIGHT HALF only: three to five elegant 3D objects that clearly stand for the "
        f"event's topic, arranged around a large soft circular disc in the colour {brand_color} "
        "(softened slightly), with a few small floating accents. The LEFT HALF of the frame is "
        "calm, smooth and nearly empty — a plain, very light off-white surface — because a "
        "headline sits there. Editorial 3D render, soft studio light, gentle shadows, crisp "
        "detail. No people, no faces, no hands, no screens with readable content, and no "
        "text, letters or numbers anywhere."
    )


def _ghost_word(title: str) -> str:
    key_words = [w for w in title.split() if w.lower().strip(".,!?:;") not in SMALL_WORDS] or title.split()
    return " ".join(key_words[-2:]).title()


def _mix(hex_color: str, other: str, amount: float) -> str:
    """hex_color moved `amount` (0..1) of the way towards `other`."""
    a = [int(hex_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    b = [int(other.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)]
    return "#{:02X}{:02X}{:02X}".format(*(round(x + (y - x) * amount) for x, y in zip(a, b)))


def _title_lines(title: str, family: str, size: int, max_width: int) -> List[List[Dict]]:
    """The title as lines of spans: key words heavy and full size, small linking words lighter
    and about half size — wrapped so no line is wider than the column."""
    small = max(28, int(size * 0.5))
    words = []
    for w in title.split():
        if w.lower().strip(".,!?:;") in SMALL_WORDS:
            words.append({"text": w.lower(), "font_size": small, "font_weight": 600})
        else:
            words.append({"text": w.upper(), "font_size": size, "font_weight": 900})
    lines: List[List[Dict]] = []
    line: List[Dict] = []
    for w in words:
        trial = line + ([{"text": " ", "font_size": int(size * 0.6), "font_weight": 900}] if line else []) + [w]
        if line and spans_width(trial, family, size, 900) > max_width:
            lines.append(line)
            line = [w]
        else:
            line = trial
    if line:
        lines.append(line)
    for ln in lines:
        if spans_width(ln, family, size, 900) > max_width:
            raise ValueError("a word is wider than the column")
    # "your MONEY" reads wrong — the small word belongs at the end of the line before
    # ("MASTER your"). Smaller sizes are tried first; at the smallest it's accepted as is.
    if size > 64 and any(ln[0]["font_weight"] == 600 for ln in lines[1:]):
        raise ValueError("a small word would start a line")
    return lines


def _date_parts(date_text: str) -> Optional[Dict[str, str]]:
    """Weekday / day(s) / month / time pulled out of the business's own date text, for the date
    card. None when the text doesn't read as a calendar date — the card then shows it as given."""
    low = date_text.lower()
    month = next((m for m in MONTHS if m in low), None)
    time = re.search(r"\b(\d{1,2}(?::\d{2})?\s?(?:am|pm))\b", low)
    scrubbed = low.replace(time.group(1), " ") if time else low
    days = re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", scrubbed)
    if not month or not days:
        return None
    weekday = next((w for w in WEEKDAYS if w in low), None)
    return {
        "weekday": weekday.capitalize() if weekday else "",
        "day": "–".join(days[:2]),
        "month": month.capitalize(),
        "time": time.group(1).upper().replace(" ", "") if time else "",
    }


def _number_lead(event_kind: str) -> Optional[Tuple[str, List[str]]]:
    """"5-Day WhatsApp Challenge" -> ("5", ["Days", "WhatsApp Challenge"]); None without a count."""
    m = re.search(r"\b(\d{1,2})[\s-]?(day|days|week|weeks|hour|hours|session|sessions|class|classes)\b",
                  event_kind, re.I)
    if not m:
        return None
    unit = m.group(2).lower()
    if not unit.endswith("s") and m.group(1) != "1":
        unit += "s"
    rest = re.sub(r"\s{2,}", " ", (event_kind[: m.start()] + event_kind[m.end():])).strip(" -·,")
    return m.group(1), [unit.capitalize()] + ([rest] if rest else [])


def _calendar_mark(x: int, y: int, size: int, color: str) -> List[Dict]:
    return [
        {"type": "shape", "shape": "rounded_rect", "x": x, "y": y + 4, "width": size, "height": size - 4,
         "corner_radius": 5, "border_color": color, "border_width": 3},
        {"type": "shape", "shape": "rect", "x": x, "y": y + 4, "width": size, "height": int(size * 0.28),
         "fill_color": color},
        {"type": "shape", "shape": "line", "x1": x + size // 3, "y1": y, "x2": x + size // 3, "y2": y + 9,
         "color": color, "stroke_width": 3},
        {"type": "shape", "shape": "line", "x1": x + 2 * size // 3, "y1": y, "x2": x + 2 * size // 3,
         "y2": y + 9, "color": color, "stroke_width": 3},
    ]


def _globe_mark(x: int, y: int, size: int, color: str) -> List[Dict]:
    return [
        {"type": "shape", "shape": "ellipse", "x": x, "y": y, "width": size, "height": size,
         "border_color": color, "border_width": 2},
        {"type": "shape", "shape": "ellipse", "x": x + size // 4, "y": y, "width": size // 2, "height": size,
         "border_color": color, "border_width": 2},
        {"type": "shape", "shape": "line", "x1": x, "y1": y + size // 2, "x2": x + size, "y2": y + size // 2,
         "color": color, "stroke_width": 2},
    ]


def _shift(layer: Dict, dy: int) -> Dict:
    for k in ("y", "y1", "y2"):
        if k in layer:
            layer[k] += dy
    return layer


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
    scene_url: Optional[str] = None,
    scene_surface: Optional[str] = None,
    title_cap: int = 112,
    social_platforms: Optional[List[str]] = None,
) -> Dict:
    """
    social_platforms: networks the contact handle is on ("whatsapp", "instagram", …), drawn as
        icons before it, as in the reference; empty -> a plain "@" mark.
    title: the event's name ("Master your money mindset").
    event_kind: what it is ("5-Day WhatsApp Challenge", "Free live webinar on Zoom").
    date_text: the date exactly as the business gave it ("19th – 23rd February, 7pm").
    host_cutout_url: the host's photo with its background removed (preferred);
    host_photo_url: the original photo, used framed if no cut-out could be made.
    scene_url: (no-photo version) generated event artwork — subject on the right, calm on the left,
        no people and no text. It fills the canvas and the date moves to a pill.
    """
    t = tokens or PLACEHOLDER_TOKENS
    if host_cutout_url:
        return _build_portrait(
            title, event_kind, date_text, canvas_size, t, host_cutout_url, host_name, host_role,
            action_line, contact_line, brand_logo_url, headline_font, brand_color or t["accent"],
            title_cap, social_platforms or [],
        )
    width, height = canvas_size
    brand = brand_color or t["accent"]
    arc = _mix(brand, "#FFFFFF", 0.18)  # big shapes: softened brand colour
    pill_fill, pill_text = _mix(brand, "#FFFFFF", 0.72), INK  # a soft tint of the brand, dark text
    accent = adjust_to_contrast(brand, SURFACE, 4.5)  # small accents: full-strength (readable) brand colour
    has_photo = bool(host_cutout_url or host_photo_url)
    use_scene = bool(scene_url) and not has_photo
    surface = (scene_surface or SURFACE) if use_scene else SURFACE
    col_x = PAD
    col_w = int(width * (0.46 if use_scene else 0.50)) - PAD
    # Generated artwork brings its own background colour (often a warm beige): the muted text and small
    # accents are darkened just enough to stay legible on it, instead of failing the check and losing the art.
    quiet = adjust_to_contrast(QUIET, surface, 7.0)
    accent = adjust_to_contrast(brand, surface, 4.5)

    back: List[Dict] = []  # behind the column, absolute positions
    if use_scene:
        # The text side takes the art's own background colour, so the two read as one image.
        surface = scene_surface or SURFACE
        # The art keeps to the right; the text side is the plain surface, with a soft fade between.
        art_x = int(width * 0.40)
        back.append({"type": "ai_generated_background", "url": scene_url, "x": art_x, "y": 0,
                     "width": width - art_x, "height": height, "fit": "cover", "focus": (0.72, 0.5)})
        back.append({"type": "shape", "shape": "hgradient", "x": art_x, "y": 0, "width": int(width * 0.16),
                     "height": height, "color": surface, "from_alpha": 255, "to_alpha": 0})

    if not use_scene:
        # Faint oversized word from the title along the bottom (ornament).
        back.append({"type": "text", "content": _ghost_word(title), "decorative": True,
                     "font_family": headline_font, "font_size": 190, "font_weight": 900, "color": "#F2EEEF",
                     "x": -10, "y": height - 200})
        # The arc on the right, with a thin ring echoing it.
        back.append({"type": "shape", "shape": "ellipse", "x": int(width * 0.47), "y": int(-height * 0.20),
                     "width": int(width * 0.86), "height": int(width * 0.86), "fill_color": arc})
        back.append({"type": "shape", "shape": "ellipse", "x": int(width * 0.43), "y": int(-height * 0.25),
                     "width": int(width * 0.95), "height": int(width * 0.95),
                     "border_color": _mix(brand, "#FFFFFF", 0.55), "border_width": 2})

    if has_photo:
        if host_cutout_url:
            back.append({"type": "composited_product", "url": host_cutout_url, "x": int(width * 0.50),
                         "y": int(height * 0.16), "width": int(width * 0.50), "height": int(height * 0.84),
                         "fit": "contain"})
        else:
            back.append({"type": "ai_generated_background", "url": host_photo_url, "x": int(width * 0.58),
                         "y": int(height * 0.22), "width": int(width * 0.36), "height": int(height * 0.56),
                         "fit": "cover", "focus": (0.5, 0.3)})
    elif not use_scene:
        # Date card on the arc — the business's own date, only re-arranged.
        card_w, card_x = 330, int(width * 0.60)
        parts = _date_parts(date_text)
        rows: List[Tuple[str, str, int, int, str]] = []
        if parts:
            if parts["weekday"]:
                rows.append((parts["weekday"].upper(), BODY_FAMILY, 30, 800, QUIET))
            rows.append((parts["day"], headline_font, 150 if len(parts["day"]) <= 2 else 100, 900, INK))
            month_size = 48
            while month_size > 42 and text_width(parts["month"].upper(), headline_font, month_size, 900) > card_w - 56:
                month_size -= 1
            rows.append((parts["month"].upper(), headline_font, month_size, 900, accent))
            if parts["time"]:
                rows.append((parts["time"], BODY_FAMILY, 34, 800, INK))
        else:
            for line in wrap_words(date_text.upper(), headline_font, 44, 900, card_w - 56)[:4]:
                rows.append((line, headline_font, 44, 900, INK))
        pitches = [int(r[2] * 1.14) for r in rows]
        card_h = sum(pitches) + 96
        card_y = int((height - card_h) * 0.42)
        back.append({"type": "shape", "shape": "rounded_rect", "x": card_x, "y": card_y, "width": card_w,
                     "height": card_h, "corner_radius": 30, "fill_color": "#FFFFFF",
                     "border_color": "#EFE4E9", "border_width": 2})
        back.extend(_calendar_mark(card_x + card_w // 2 - 18, card_y + 26, 36, accent))
        y = card_y + 78
        for (text, fam, size, weight, colour), pitch in zip(rows, pitches):
            back.append({"type": "text", "content": text, "font_family": fam, "font_size": size,
                         "font_weight": weight, "color": colour, "x": card_x + card_w // 2, "y": y,
                         "text_align": "ma", "secondary": size < 42})
            y += pitch

    # ── left column, laid out from y=0 then centred ──
    col: List[Dict] = []
    y = 0
    size = title_cap
    lines = _title_lines(title, headline_font, size, col_w)
    if len(lines) > 4:
        raise EventPromoOverflow("title too long for four lines")
    for ln in lines:
        col.append({"type": "text", "content": "".join(sp["text"] for sp in ln),
                    "spans": [{**sp, "color": INK} for sp in ln], "font_family": headline_font,
                    "font_size": size, "font_weight": 900, "color": INK, "x": col_x, "y": y})
        y += int(size * 0.93)
    y += 26

    lead = _number_lead(event_kind)
    if lead:
        num, words = lead
        num_size = 120
        num_w = int(text_width(num, headline_font, num_size, 900))
        col.append({"type": "text", "content": num, "font_family": headline_font, "font_size": num_size,
                    "font_weight": 900, "color": INK, "x": col_x, "y": y - 6})
        beside_w = col_w - num_w - 14
        beside: List[str] = []
        for i, wtxt in enumerate(words[:2]):
            beside += wrap_words(wtxt, BODY_FAMILY, 42, 800 if i == 0 else 700, beside_w)
        beside = beside[:3]
        for i, wtxt in enumerate(beside):
            col.append({"type": "text", "content": wtxt, "font_family": BODY_FAMILY, "font_size": 42,
                        "font_weight": 800 if i == 0 else 700, "color": INK, "x": col_x + num_w + 14,
                        "y": y + 12 + i * 50})
        y += max(num_size, 12 + 50 * len(beside)) + 6
        col.append({"type": "shape", "shape": "rect", "x": col_x, "y": y, "width": min(col_w, num_w + 340),
                    "height": 4, "fill_color": accent})
        y += 30
    else:
        col.append({"type": "shape", "shape": "ellipse", "x": col_x, "y": y + 17, "width": 14, "height": 14,
                    "fill_color": accent})
        for wl in wrap_words(event_kind, BODY_FAMILY, 42, 700, col_w - 30)[:2]:
            col.append({"type": "text", "content": wl, "font_family": BODY_FAMILY, "font_size": 42,
                        "font_weight": 700, "color": INK, "x": col_x + 28, "y": y})
            y += 52
        y += 20

    if has_photo or use_scene:
        # Date pill with a calendar mark; a long date wraps (date, then time) inside a taller pill.
        room = col_w - 92
        date_lines = [date_text]
        date_size = 34
        while date_size > 28 and text_width(date_text, BODY_FAMILY, date_size, 800) > room:
            date_size -= 1
        if text_width(date_text, BODY_FAMILY, date_size, 800) > room:
            head, _, tail = date_text.rpartition(",")
            date_lines = [head.strip(), tail.strip()] if head and tail.strip() else wrap_words(
                date_text, BODY_FAMILY, 30, 800, room)[:2]
            date_size = 30
            while date_size > 28 and max(text_width(l, BODY_FAMILY, date_size, 800) for l in date_lines) > room:
                date_size -= 1
        line_h = int(date_size * 1.22)
        pill_h = 74 if len(date_lines) == 1 else 40 + line_h * len(date_lines)
        pill_w = min(col_w, int(max(text_width(l, BODY_FAMILY, date_size, 800) for l in date_lines)) + 96)
        col.append({"type": "shape", "shape": "rounded_rect", "x": col_x, "y": y, "width": pill_w,
                    "height": pill_h, "corner_radius": min(37, pill_h // 2), "fill_color": pill_fill})
        col.extend(_calendar_mark(col_x + 22, y + (pill_h - 28) // 2, 28, adjust_to_contrast(brand, pill_fill, 3.0)))
        ty = y + (pill_h - line_h * len(date_lines)) // 2 + 2
        for i, line in enumerate(date_lines):
            col.append({"type": "text", "content": line, "font_family": BODY_FAMILY, "font_size": date_size,
                        "font_weight": 800 if i == 0 else 700, "color": pill_text, "x": col_x + 64,
                        "y": ty + i * line_h, "secondary": True})
        y += pill_h + 24

    if host_name:
        col.append({"type": "shape", "shape": "triangle", "x": col_x + 2, "y": y + 12, "width": 13,
                    "height": 16, "fill_color": accent})
        col.append({"type": "text", "content": host_name, "font_family": BODY_FAMILY, "font_size": 34,
                    "font_weight": 800, "color": INK, "x": col_x + 28, "y": y, "secondary": True})
        y += 44
        if host_role:
            col.append({"type": "text", "content": host_role, "font_family": BODY_FAMILY, "font_size": 28,
                        "font_weight": 600, "color": quiet, "x": col_x + 28, "y": y, "secondary": True})
            y += 38
        y += 20

    if action_line:
        btn_h = 70
        label = f"{action_line}  →"
        btn_w = min(col_w, int(text_width(label, BODY_FAMILY, 32, 800)) + 72)
        col.append({"type": "shape", "shape": "rounded_rect", "x": col_x + 2, "y": y + 8, "width": btn_w,
                    "height": btn_h, "corner_radius": btn_h // 2, "fill_color": "#14101422"})
        col.append({"type": "shape", "shape": "rounded_rect", "x": col_x, "y": y, "width": btn_w,
                    "height": btn_h, "corner_radius": btn_h // 2, "fill_color": INK})
        col.append({"type": "text", "content": label, "font_family": BODY_FAMILY, "font_size": 32,
                    "font_weight": 800, "color": "#FFFFFF", "x": col_x + btn_w // 2, "y": y + 16,
                    "text_align": "ma", "secondary": True})
        y += btn_h + 18

    if contact_line:
        if contact_line.strip().startswith("@"):
            col.append({"type": "shape", "shape": "ellipse", "x": col_x, "y": y + 6, "width": 26, "height": 26,
                        "border_color": quiet, "border_width": 2})
            col.append({"type": "text", "content": "@", "font_family": BODY_FAMILY, "font_size": 28,
                        "font_weight": 800, "color": quiet, "x": col_x + 13, "y": y + 19, "text_align": "mm",
                        "decorative": True})
            contact_line = contact_line.strip()[1:]
        else:
            col.extend(_globe_mark(col_x, y + 6, 26, quiet))
        col.append({"type": "text", "content": contact_line, "font_family": BODY_FAMILY, "font_size": 30,
                    "font_weight": 700, "color": quiet, "x": col_x + 38, "y": y, "secondary": True})
        y += 40

    top = PAD + (100 if brand_logo_url else 10)
    room = height - PAD - top
    if y > room:
        raise EventPromoOverflow("event details don't fit the column")
    offset = top + (room - y) // 2
    col = [_shift(layer, offset) for layer in col]

    layers = back + col
    if brand_logo_url:
        layers.append({"type": "brand_asset", "role": "brand_logo", "url": brand_logo_url, "x": PAD, "y": PAD - 16, "width": 220,
                       "height": 84})
    for i, layer in enumerate(layers, start=1):
        layer["z_index"] = i

    document = {"canvas": {"width": width, "height": height, "background_color": surface}, "layers": layers}
    assert_legible(document, {**t, "surface": surface, "accent": accent})
    return document


# ── the portrait version: the reference layout, built around the host's cut-out ──

PLATFORM_COLOURS = {
    "whatsapp": "#25D366", "instagram": "#E4405F", "facebook": "#1877F2", "tiktok": "#111111",
    "youtube": "#FF0000", "linkedin": "#0A66C2", "x": "#111111",
}


def _platform_in(text: str) -> Optional[str]:
    """The network an event runs on, when its description names one ("5-Day WhatsApp Challenge")."""
    low = text.lower()
    return next((p for p in ("whatsapp", "instagram", "facebook", "tiktok", "youtube", "linkedin")
                 if p in low), None)


def _fitted_title(title: str, family: str, cap: int, col_w: int) -> List[Tuple[int, List[Dict]]]:
    """The title as (size, spans) lines. Lines are wrapped at `cap`, then each is enlarged (up to
    1.3×cap) to fill the column, so the block reads as one solid stack like "MASTER YOUR / MONEY /
    MINDSET". Small linking words stay uppercase at about half size, sitting on the baseline."""
    lines = _title_lines(title, family, cap, col_w)
    out = []
    for ln in lines:
        w = spans_width(ln, family, cap, 900)
        size = max(cap, min(int(cap * 1.3), int(cap * col_w / max(1, w))))
        k = size / cap
        spans = [{**sp, "text": sp["text"].upper(), "font_size": max(28, int(sp["font_size"] * k)),
                  "font_weight": 900 if sp["font_weight"] == 900 else 800} for sp in ln]
        while spans_width(spans, family, size, 900) > col_w and size > cap:
            size -= 2
            k = size / cap
            spans = [{**sp, "font_size": max(28, int(sp2["font_size"] * k))} for sp, sp2 in zip(spans, ln)]
        out.append((size, spans))
    return out


def _date_two_lines(date_text: str) -> List[str]:
    """"19th – 23rd February, 7pm" -> ["19TH – 23RD", "FEBRUARY, 7PM"]: the day(s) big, the rest
    under it — the business's own words, only split and capitalised."""
    low = date_text.lower()
    month = next((m for m in MONTHS if m in low), None)
    if month:
        i = low.index(month)
        head, tail = date_text[:i].strip(" ,"), date_text[i:].strip()
        if head and tail:
            return [head.upper(), tail.upper()]
    return [date_text.upper()]


def _role_lines(host_role: Optional[str], max_w: int) -> Tuple[int, List[str]]:
    """The host's title, at the 28px legibility floor, in up to four lines (it used to stop at two and drop
    the rest without a sign). A title longer still ends with an ellipsis so it reads as shortened."""
    if not host_role:
        return 28, []
    lines = wrap_words(host_role, BODY_FAMILY, 28, 700, max_w)
    if len(lines) > 4:
        lines = lines[:4]
        lines[-1] = lines[-1].rstrip(".,;:") + "…"
    return 28, lines


def _build_portrait(title, event_kind, date_text, canvas_size, t, host_cutout_url, host_name, host_role,
                    action_line, contact_line, brand_logo_url, headline_font, brand, title_cap,
                    social_platforms) -> Dict:
    width, height = canvas_size
    s = width / 1080
    pad = int(57 * s)
    col_w = int(width * 0.545) - pad
    surface = "#FFFFFF"
    line_colour = brand  # ornament lines and markers: the brand colour itself, as in the reference

    back: List[Dict] = []
    # Two oversized faint words from the title, running off the bottom-left (ornament).
    key = [w for w in title.split() if w.lower().strip(".,!?:;") not in SMALL_WORDS] or title.split()
    ghost = [w.capitalize() for w in key[-2:]]
    gsize = int(205 * s)
    for i, word in enumerate(ghost):
        back.append({"type": "text", "content": word, "decorative": True, "font_family": headline_font,
                     "font_size": gsize, "font_weight": 900, "color": "#F3F1F2",
                     "x": int(-18 * s), "y": height - int(gsize * (len(ghost) - i) * 0.86) + int(20 * s)})
    # The arc, measured off the reference: a large brand-colour circle from the top-right,
    # with a soft halo, and its top-right corner cut back to white by a second, larger circle.
    cx, cy, r = int(979 * s), int(28 * s), int(572 * s)
    halo = int(r * 1.07)
    back.append({"type": "shape", "shape": "ellipse", "x": cx - halo, "y": cy - halo, "width": 2 * halo,
                 "height": 2 * halo, "fill_color": _mix(brand, "#FFFFFF", 0.82)})
    back.append({"type": "shape", "shape": "ellipse", "x": cx - r, "y": cy - r, "width": 2 * r,
                 "height": 2 * r, "fill_color": brand})
    wx, wy, wr = int(1477 * s), int(-1400 * s), int(1713 * s)
    back.append({"type": "shape", "shape": "ellipse", "x": wx - wr, "y": wy - wr, "width": 2 * wr,
                 "height": 2 * wr, "fill_color": surface})
    # The host: on the right, standing on the bottom edge, clear of the name beside them. The name and
    # title sit over the photo's empty top-left, as in the reference; when they run long enough to reach
    # the host's head (about 0.655 of the width), the photo moves right by just that much.
    photo_x, photo_right = int(width * 0.525), int(width * 0.525) + int(width * 0.495)
    if host_name:
        tx = int(width * 0.418) + int(30 * s)
        role_size, role_lines = _role_lines(host_role, int(width * 0.22))
        text_right = tx + max([text_width(host_name, BODY_FAMILY, int(34 * s), 800)]
                              + [text_width(rl, BODY_FAMILY, role_size, 700) for rl in role_lines])
        photo_x += max(0, int(text_right - width * 0.655))
    back.append({"type": "composited_product", "url": host_cutout_url, "trim": True, "fit": "contain",
                 "align_x": 1.0, "align_y": 1.0, "x": photo_x, "y": int(height * 0.15),
                 "width": photo_right - photo_x, "height": int(height * 0.87)})

    col: List[Dict] = []
    y = 0
    for size, spans in _fitted_title(title, headline_font, title_cap, col_w):
        col.append({"type": "text", "content": "".join(sp["text"] for sp in spans),
                    "spans": [{**sp, "color": INK} for sp in spans], "font_family": headline_font,
                    "font_size": size, "font_weight": 900, "color": INK, "x": pad, "y": y})
        y += int(size * 0.86)
    y += int(46 * s)

    host_y = y  # the host's name sits level with the event line, beside the photo
    host_x = int(width * 0.418)
    ev_w = (host_x - pad - int(24 * s)) if host_name else col_w
    lead = _number_lead(event_kind)
    platform = _platform_in(event_kind)
    words_size, pitch = int(46 * s), int(50 * s)
    if lead:
        num, words = lead
        num_size = int(176 * s)
        num_w = int(text_width(num, headline_font, num_size, 900))
        col.append({"type": "text", "content": num, "font_family": headline_font, "font_size": num_size,
                    "font_weight": 900, "color": INK, "x": pad - int(4 * s), "y": y - int(30 * s)})
        bx = pad + num_w + int(8 * s)
        beside: List[str] = []
        for wtxt in words[:2]:
            beside += wrap_words(wtxt, BODY_FAMILY, words_size, 800, ev_w - (bx - pad))
        beside = beside[:3]
        right = bx
        for i, wtxt in enumerate(beside):
            col.append({"type": "text", "content": wtxt, "font_family": BODY_FAMILY, "font_size": words_size,
                        "font_weight": 800, "color": INK, "x": bx, "y": y + i * pitch})
            right = max(right, bx + int(text_width(wtxt, BODY_FAMILY, words_size, 800)))
        block_h = max(int(num_size * 0.78), pitch * len(beside))
        if platform:
            # The network's own badge after the first word, as the WhatsApp badge in the reference.
            bs = int(56 * s)
            first_w = int(text_width(beside[0], BODY_FAMILY, words_size, 800))
            bxx, byy = bx + first_w + int(10 * s), y - int(14 * s)
            col.append({"type": "shape", "shape": "ellipse", "x": bxx, "y": byy, "width": bs, "height": bs,
                        "fill_color": PLATFORM_COLOURS[platform]})
            col.append({"type": "icon", "name": platform, "x": bxx + bs // 5, "y": byy + bs // 5,
                        "size": bs - 2 * (bs // 5), "color": "#FFFFFF"})
            right = max(right, bxx + bs)
        y += block_h + int(14 * s)
        col.append({"type": "shape", "shape": "rect", "x": pad, "y": y, "width": right - pad + int(8 * s),
                    "height": max(3, int(4 * s)), "fill_color": line_colour})
        y += int(52 * s)
    else:
        for wl in wrap_words(event_kind, BODY_FAMILY, words_size, 800, ev_w)[:3]:
            col.append({"type": "text", "content": wl, "font_family": BODY_FAMILY, "font_size": words_size,
                        "font_weight": 800, "color": INK, "x": pad, "y": y})
            y += pitch
        y += int(10 * s)
        col.append({"type": "shape", "shape": "rect", "x": pad, "y": y, "width": int(col_w * 0.6),
                    "height": max(3, int(4 * s)), "fill_color": line_colour})
        y += int(48 * s)

    # Date pill: the brand colour, the day(s) big over the month, a calendar mark, a soft shadow.
    pill_fill, pill_ink = block_with_text(brand, INK, 7.0)
    lines = _date_two_lines(date_text)
    big, small = max(42, int(44 * s)), int(30 * s)
    sizes = [big, small] if len(lines) == 2 else [big]
    room = col_w - int(100 * s)
    while max(text_width(l, BODY_FAMILY, z, 900) for l, z in zip(lines, sizes)) > room and sizes[0] > 42:
        sizes = [z - 1 for z in sizes]
        sizes = [max(42, sizes[0])] + [max(28, z) for z in sizes[1:]]
        if sizes[0] <= 42:
            break
    if max(text_width(l, BODY_FAMILY, z, 900) for l, z in zip(lines, sizes)) > room:
        raise EventPromoOverflow("date too long for the pill")
    text_h = int(sizes[0] * 1.05) + (int(sizes[1] * 1.1) if len(sizes) == 2 else 0)
    pill_h = text_h + int(30 * s)
    pill_w = int(max(text_width(l, BODY_FAMILY, z, 900) for l, z in zip(lines, sizes))) + int(110 * s)
    col.append({"type": "shape", "shape": "rounded_rect", "x": pad + int(6 * s), "y": y + int(10 * s),
                "width": pill_w - int(12 * s), "height": pill_h, "corner_radius": pill_h // 2,
                "fill_color": _mix(brand, "#FFFFFF", 0.55)})
    col.append({"type": "shape", "shape": "rounded_rect", "x": pad, "y": y, "width": pill_w, "height": pill_h,
                "corner_radius": pill_h // 2, "fill_color": pill_fill})
    col.extend(_calendar_mark(pad + int(28 * s), y + (pill_h - int(40 * s)) // 2, int(40 * s), pill_ink))
    ty = y + (pill_h - text_h) // 2 - int(2 * s)
    for i, (line, z) in enumerate(zip(lines, sizes)):
        col.append({"type": "text", "content": line, "font_family": BODY_FAMILY, "font_size": z,
                    "font_weight": 900 if i == 0 else 700, "color": pill_ink, "x": pad + int(82 * s),
                    "y": ty, "secondary": i > 0})
        ty += int(z * 1.05)
    y += pill_h + int(40 * s)

    # Socials row (icons, then the handle) with the action beside it, smaller.
    row_h = int(56 * s)
    x = pad
    if contact_line:
        handle = contact_line.strip()
        icons = [p for p in social_platforms if p in PLATFORM_COLOURS][:4]
        ic = int(34 * s)
        if icons and handle.startswith("@"):
            for p in icons:
                col.append({"type": "icon", "name": p, "x": x, "y": y + (row_h - ic) // 2, "size": ic,
                            "color": INK})
                x += ic + int(8 * s)
            handle = "/" + handle
        elif handle.startswith("@"):
            col.append({"type": "shape", "shape": "ellipse", "x": x, "y": y + (row_h - ic) // 2, "width": ic,
                        "height": ic, "border_color": INK, "border_width": 2})
            col.append({"type": "text", "content": "@", "font_family": BODY_FAMILY, "font_size": int(30 * s),
                        "font_weight": 800, "color": INK, "x": x + ic // 2, "y": y + row_h // 2,
                        "text_align": "mm", "decorative": True})
            x += ic + int(10 * s)
            handle = handle[1:]
        else:
            col.extend(_globe_mark(x, y + (row_h - ic) // 2, ic, INK))
            x += ic + int(10 * s)
        hs = int(32 * s)
        col.append({"type": "text", "content": handle, "font_family": BODY_FAMILY, "font_size": hs,
                    "font_weight": 700, "color": INK, "x": x, "y": y + (row_h - hs) // 2 - int(3 * s),
                    "secondary": True})
        x += int(text_width(handle, BODY_FAMILY, hs, 700)) + int(28 * s)
    if action_line:
        label = f"{action_line}  →"
        bfs = int(28 * s)
        btn_w = int(text_width(label, BODY_FAMILY, bfs, 800)) + int(48 * s)
        if x + btn_w > pad + col_w and x > pad:  # no room beside the handle: its own row
            y += row_h + int(14 * s)
            x = pad
        col.append({"type": "shape", "shape": "rounded_rect", "x": x, "y": y, "width": btn_w, "height": row_h,
                    "corner_radius": row_h // 2, "fill_color": INK})
        col.append({"type": "text", "content": label, "font_family": BODY_FAMILY, "font_size": bfs,
                    "font_weight": 800, "color": "#FFFFFF", "x": x + btn_w // 2, "y": y + row_h // 2,
                    "text_align": "mm", "secondary": True})
    if contact_line or action_line:
        y += row_h

    top = int(height * 0.205) if brand_logo_url else pad
    room_h = height - int(64 * s) - top
    if y > room_h:
        raise EventPromoOverflow("event details don't fit the column")
    offset = top + max(0, (room_h - y) // 3)
    col = [_shift(layer, offset) for layer in col]
    host_y += offset

    if host_name:
        hx = host_x
        hw = int(width * 0.22)
        col.append({"type": "shape", "shape": "triangle", "x": hx, "y": host_y + int(10 * s),
                    "width": int(14 * s), "height": int(18 * s), "fill_color": line_colour})
        col.append({"type": "text", "content": host_name, "font_family": BODY_FAMILY, "font_size": int(34 * s),
                    "font_weight": 800, "color": INK, "x": hx + int(30 * s), "y": host_y, "secondary": True})
        ry = host_y + int(44 * s)
        role_size, role_lines = _role_lines(host_role, hw)
        for rl in role_lines:
            col.append({"type": "text", "content": rl, "font_family": BODY_FAMILY, "font_size": role_size,
                        "font_weight": 700, "color": QUIET, "x": hx + int(30 * s), "y": ry, "secondary": True})
            ry += role_size + 6

    layers = back + col
    if brand_logo_url:
        layers.append({"type": "brand_asset", "role": "brand_logo", "url": brand_logo_url, "x": pad, "y": int(60 * s),
                       "width": int(300 * s), "height": int(110 * s)})
    for i, layer in enumerate(layers, start=1):
        layer["z_index"] = i
    document = {"canvas": {"width": width, "height": height, "background_color": surface}, "layers": layers}
    assert_legible(document, {**t, "surface": surface})
    return document

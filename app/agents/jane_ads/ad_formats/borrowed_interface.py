"""
Borrowed Interface — VSG-01 v3 §2.6 (SEED-087, companion record SEED-061).

"The ad should read as a message rather than an advertisement." Evoke the
*feel* of a familiar interface, not a pixel clone: message bubbles with
timestamps (`chat` variant), or a single note page (`note` variant). Three
or four turns maximum for chat — the final turn carries the offer or the
answer (caller-side guarantee: this module has no way to know which turn
is semantically "the answer", the same way Receipt can't know a figure is
"currently honoured" — it only lays out what it's given).

Hard checks (§2.6):

1. Android and WhatsApp, not iOS and iMessage — the audience is
   overwhelmingly on Android. Notes-app and email variants inherit the
   same rule. This module never draws iOS chrome (no blue iMessage
   bubbles, no San-Francisco-style rounded-square icons) — bubbles use
   token colours, not a platform's literal palette (see point 3).

2. No functional-looking controls — send buttons, reply fields, call
   icons, play buttons (SEED-090). Enforced by construction, not
   validation: this module has exactly two draw primitives in play —
   bubble + text — there is no code path here that could accidentally
   render a button or icon.

3. Evoke, do not counterfeit a platform's exact branding. WhatsApp's own
   bubble green (#DCF8C6 / #005C4B) is deliberately NOT hardcoded here —
   the outgoing bubble colour is a computed light tint of the brand's own
   `accent` token (see `_tint`), so the format reads as "a chat" without
   reproducing a specific app's literal colour value.

4. The exchange must not misrepresent price, delivery or availability —
   caller-side guarantee, same category as Receipt's bank-alert rule.

Also applies here, as everywhere (§1.6, a hard constraint, not specific to
this format): minimum body type is 42px at 1080 canvas width. A real chat
app renders timestamps much smaller than message text — that convention is
overridden here, deliberately, because §1.6 states no exception for
secondary/metadata text and this pipeline targets a mid-range Android
screen viewed outdoors after data-saver compression.
"""
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from .tokens import AdFormatDef, PLACEHOLDER_TOKENS
from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService

FORMAT = AdFormatDef(
    format_id="SEED-087",
    name="Borrowed Interface",
    asset_source="drawn",
    layers_used="L4",
    requires=[],
)

_FONT_BODY = 44   # >= §1.6's 42px floor, applied to every text element here,
                  # timestamps included — see module docstring
_FONT_TITLE = 72  # §1.6's headline floor, used only for the note variant's title


class TooManyTurns(ValueError):
    """§2.6: 'Three or four turns maximum.' A longer exchange stops reading
    as a borrowed interface and starts reading as a transcript — raised
    before layout rather than silently overflowing the canvas."""
    pass


_DUMMY_DRAW = ImageDraw.Draw(Image.new("RGB", (1, 1)))


def _load_measuring_font(font_size: int, font_weight: int) -> ImageFont.ImageFont:
    """Mirrors DocumentRendererService._load_font's exact path/fallback so a
    bubble is sized against the same font that will actually draw the text
    at render time — measuring against the wrong font is worse than not
    measuring at all."""
    try:
        path = (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if font_weight >= 700
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        )
        return ImageFont.truetype(path, font_size)
    except Exception:
        return ImageFont.load_default()


def _text_width(text: str, font_size: int, font_weight: int = 400) -> int:
    return int(_DUMMY_DRAW.textlength(text, font=_load_measuring_font(font_size, font_weight)))


def _wrap_text(text: str, max_width: int, font_size: int, font_weight: int = 400, measure=None) -> List[str]:
    """Greedy word-wrap against a measured width. A fixed-width bubble with
    unwrapped text silently overflows past its own edge (and off the canvas)
    the moment a message is longer than a couple of words — found by
    actually rendering a realistic message, not assumed safe from a short
    test string.

    `measure` defaults to the real font-file measurement (`_text_width`) but
    can be overridden — needed because the real measurement depends on a
    font file being present at DocumentRendererService's hardcoded path,
    which isn't true on every machine that runs this module's tests (Pillow
    silently falls back to a fixed-size bitmap font there, ignoring
    font_size entirely). Production always uses the real default; injection
    exists only so the wrap *algorithm* can be tested independently of
    which fonts happen to be installed."""
    measure = measure or (lambda s: _text_width(s, font_size, font_weight))
    words = text.split()
    if not words:
        return [text]
    lines = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if measure(candidate) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _tint(hex_color: str, toward: str, amount: float) -> str:
    """Blend `hex_color` toward `toward` by `amount` (0=hex_color, 1=toward).
    Used to derive the outgoing-bubble colour from the brand's own `accent`
    token rather than hardcoding a platform's literal brand colour."""
    a = tuple(int(hex_color.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    b = tuple(int(toward.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    mixed = tuple(round(a[c] + (b[c] - a[c]) * amount) for c in range(3))
    return "#%02X%02X%02X" % mixed


def build_document(
    turns: List[Tuple[str, str, str]],
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> Dict:
    """
    Chat variant. turns: [(speaker, message, timestamp), ...] where speaker
    is "them" (incoming, left-aligned, `field` bubble) or "us" (outgoing,
    right-aligned, tinted-`accent` bubble). Each message is rendered as a
    single line — this renderer does not wrap text, matching every other
    format module in this package.
    """
    if len(turns) > 4:
        raise TooManyTurns(
            f"got {len(turns)} turns, §2.6 allows three or four maximum"
        )
    for speaker, _msg, _ts in turns:
        if speaker not in ("them", "us"):
            raise ValueError(f"speaker must be 'them' or 'us', got {speaker!r}")

    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    us_bubble_color = _tint(t["accent"], "#FFFFFF", 0.85)

    layers = []
    z = 0
    margin = 72
    bubble_width = int(width * 0.68)
    h_pad, v_pad = 28, 24
    line_height = int(_FONT_BODY * 1.3)
    y = margin

    for speaker, message, timestamp in turns:
        is_us = speaker == "us"
        bubble_x = (width - margin - bubble_width) if is_us else margin
        fill = us_bubble_color if is_us else t["field"]
        border = None if is_us else t["edge"]

        lines = _wrap_text(message, bubble_width - 2 * h_pad, _FONT_BODY)
        bubble_height = max(96, v_pad * 2 + len(lines) * line_height - (line_height - _FONT_BODY))

        z += 1
        layers.append({
            "type": "shape", "z_index": z, "shape": "rounded_rect",
            "x": bubble_x, "y": y, "width": bubble_width, "height": bubble_height,
            "corner_radius": 20, "fill_color": fill,
            **({"border_color": border, "border_width": 2} if border else {}),
        })

        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": "\n".join(lines),
            "x": bubble_x + h_pad, "y": y + (bubble_height - (len(lines) * line_height - (line_height - _FONT_BODY))) // 2,
            "font_size": _FONT_BODY, "color": t["ink"],
        })

        z += 1
        ts_x = (bubble_x + bubble_width - 4) if is_us else (bubble_x + 4)
        layers.append({
            "type": "text", "z_index": z, "content": timestamp,
            "x": ts_x, "y": y + bubble_height + 8,
            "font_size": _FONT_BODY, "color": t["ink-quiet"],
            **({"text_align": "ra"} if is_us else {}),
        })

        y += bubble_height + 8 + _FONT_BODY + 32

    return {
        "canvas": {"width": width, "height": height, "background_color": t["surface"]},
        "layers": layers,
    }


def build_note_document(
    lines: List[str],
    title: Optional[str] = None,
    meta_line: Optional[str] = None,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> Dict:
    """
    Note-page variant (§2.6: "a note page"). A single card, `field` on
    `surface`, `edge` border — a title (headline-tier, if given), an
    optional meta line ("Edited 2m ago" — ink-quiet, still body-tier per
    §1.6), then body lines, one per string, no wrapping.
    """
    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    margin = 72
    card_x, card_y = margin, margin
    card_width = width - 2 * margin

    layers = []
    z = 0
    content_y = card_y + 56

    if title:
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": title,
            "x": card_x + 40, "y": content_y,
            "font_size": _FONT_TITLE, "font_weight": 700, "color": t["ink"],
        })
        content_y += _FONT_TITLE + 24

    if meta_line:
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": meta_line,
            "x": card_x + 40, "y": content_y,
            "font_size": _FONT_BODY, "color": t["ink-quiet"],
        })
        content_y += _FONT_BODY + 32

    for line in lines:
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": line,
            "x": card_x + 40, "y": content_y,
            "font_size": _FONT_BODY, "color": t["ink"],
        })
        content_y += _FONT_BODY + 24

    card_height = (content_y - card_y) + 40

    # Card background/border drawn last but z-indexed under everything above
    # (z_index 0, lowest) so it sits behind the text/title/meta already laid
    # out against fixed offsets from card_y.
    layers.append({
        "type": "shape", "z_index": 0, "shape": "rounded_rect",
        "x": card_x, "y": card_y, "width": card_width, "height": card_height,
        "corner_radius": 16, "fill_color": t["field"],
        "border_color": t["edge"], "border_width": 2,
    })

    return {
        "canvas": {"width": width, "height": height, "background_color": t["surface"]},
        "layers": layers,
    }


async def render(*args, **kwargs) -> bytes:
    """Chat variant build + render in one call."""
    document = build_document(*args, **kwargs)
    return await DocumentRendererService.render_to_png(document)


async def render_note(*args, **kwargs) -> bytes:
    """Note variant build + render in one call."""
    document = build_note_document(*args, **kwargs)
    return await DocumentRendererService.render_to_png(document)

"""
Shared text-measurement/wrap helpers for drawn (L4) ad format modules.

Extracted out of borrowed_interface.py once Review Card needed the exact
same capability — a fixed-width card holding wrapped body text isn't unique
to chat bubbles. Keeping one copy means the font-path-mirroring logic below
(`load_measuring_font`) only has to be right once, and every format module
that needs wrapped text stays in sync with whatever DocumentRendererService
actually does at render time.
"""
from typing import Callable, List, Optional

from PIL import Image, ImageDraw, ImageFont

_DUMMY_DRAW = ImageDraw.Draw(Image.new("RGB", (1, 1)))


def load_measuring_font(font_size: int, font_weight: int) -> ImageFont.ImageFont:
    """Mirrors DocumentRendererService._load_font's exact path/fallback so
    text is measured against the same font that will actually draw it at
    render time — measuring against the wrong font is worse than not
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


def text_width(text: str, font_size: int, font_weight: int = 400) -> int:
    return int(_DUMMY_DRAW.textlength(text, font=load_measuring_font(font_size, font_weight)))


def wrap_text(
    text: str,
    max_width: int,
    font_size: int,
    font_weight: int = 400,
    measure: Optional[Callable[[str], int]] = None,
) -> List[str]:
    """Greedy word-wrap against a measured width.

    `measure` defaults to the real font-file measurement (`text_width`) but
    can be overridden — needed because the real measurement depends on a
    font file being present at DocumentRendererService's hardcoded path,
    which isn't guaranteed on every machine that runs a format module's
    tests (Pillow silently falls back to a fixed-size bitmap font there,
    ignoring font_size entirely, rather than raising). Production always
    uses the real default; injection exists only so the wrap *algorithm*
    can be tested independently of which fonts happen to be installed.
    """
    measure = measure or (lambda s: text_width(s, font_size, font_weight))
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

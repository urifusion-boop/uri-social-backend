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

from PIL import ImageFont

from app.utils.fonts import load_font, text_width as fonts_text_width


def load_measuring_font(font_size: int, font_weight: int, font_family: Optional[str] = None) -> ImageFont.ImageFont:
    """The same font DocumentRendererService draws with (both go through app/utils/fonts.py),
    so text is measured against what will actually draw it — measuring against the wrong
    font is worse than not measuring at all."""
    return load_font(font_family, font_size, font_weight)


def text_width(text: str, font_size: int, font_weight: int = 400, font_family: Optional[str] = None) -> int:
    """Width as drawn, including any characters drawn in the fallback font (e.g. ₦)."""
    return int(fonts_text_width(text, font_family, font_size, font_weight))


def wrap_text(
    text: str,
    max_width: int,
    font_size: int,
    font_weight: int = 400,
    measure: Optional[Callable[[str], int]] = None,
    font_family: Optional[str] = None,
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
    measure = measure or (lambda s: text_width(s, font_size, font_weight, font_family))
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

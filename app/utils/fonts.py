"""
Fonts for rendered images (ads, documents): bundled font files, picked by family name and weight.

The files live in app/assets/fonts/<FamilyNoSpaces>/<FamilyNoSpaces>-<weight>.ttf, with each
family's licence beside them (all Google Fonts, OFL or Apache — free to bundle and use).

- A family that isn't bundled (or no family at all — the renderer's old "Arial" default)
  keeps the previous DejaVu Sans behaviour exactly, so nothing renders differently until a
  caller actually asks for a bundled family.
- A weight that isn't bundled resolves to the nearest one that is (ties go heavier: a headline
  asked for at 750 should come out bold, not regular).
- Many display fonts have no ₦ sign (Poppins, Bebas Neue, Playfair Display...). Text is drawn
  and measured in runs: any character the chosen font lacks is drawn in Inter at the same
  weight, so a price never shows up as an empty box.
"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import List, Optional, Tuple

from PIL import ImageDraw, ImageFont

FONTS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "assets", "fonts"))

DEJAVU = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
DEJAVU_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Drawn in place of any character the chosen font doesn't have. Inter covers ₦, € and £,
# smart quotes and dashes.
FALLBACK_FAMILY = "Inter"

_WEIGHT_FILE = re.compile(r"-(\d{3})\.ttf$")


def _slug(family: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", family or "")


@lru_cache(maxsize=None)
def bundled_weights(family: str) -> Tuple[int, ...]:
    """The weights bundled for a family, lightest first. Empty if the family isn't bundled."""
    folder = os.path.join(FONTS_DIR, _slug(family))
    if not os.path.isdir(folder):
        return ()
    weights = []
    for name in os.listdir(folder):
        m = _WEIGHT_FILE.search(name)
        if m:
            weights.append(int(m.group(1)))
    return tuple(sorted(weights))


def available_families() -> List[str]:
    """Folder names of every bundled family (spaces removed, e.g. 'BebasNeue')."""
    if not os.path.isdir(FONTS_DIR):
        return []
    return sorted(d for d in os.listdir(FONTS_DIR) if bundled_weights(d))


def _nearest(weights: Tuple[int, ...], wanted: int) -> int:
    return min(weights, key=lambda w: (abs(w - wanted), -w))


def font_path(family: Optional[str], weight: int = 400) -> str:
    """The file to draw `family` at `weight` with. Falls back to DejaVu (the renderer's
    long-standing default) when the family isn't bundled."""
    weights = bundled_weights(family or "")
    if weights:
        w = _nearest(weights, int(weight or 400))
        slug = _slug(family or "")
        return os.path.join(FONTS_DIR, slug, f"{slug}-{w}.ttf")
    return DEJAVU_BOLD if (weight or 400) >= 700 else DEJAVU


@lru_cache(maxsize=512)
def _truetype(path: str, size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def load_font(family: Optional[str], size: int, weight: int = 400) -> ImageFont.ImageFont:
    return _truetype(font_path(family, weight), int(size))


@lru_cache(maxsize=None)
def _charmap(path: str) -> Optional[frozenset]:
    """Code points the font file can draw. None if it can't be read — then nothing is
    swapped for that font, the same as before this module existed."""
    try:
        from fontTools.ttLib import TTFont

        with TTFont(path, lazy=True) as tt:
            return frozenset(tt.getBestCmap() or {})
    except Exception:
        return None


def text_runs(text: str, family: Optional[str], weight: int = 400) -> List[Tuple[str, str]]:
    """Split `text` into (run, font_path) pieces: the chosen font wherever it has the
    character, Inter where it doesn't. Whitespace stays with the run it sits in."""
    primary = font_path(family, weight)
    cmap = _charmap(primary)
    if not text or cmap is None:
        return [(text, primary)]
    fallback = font_path(FALLBACK_FAMILY, weight)
    if fallback == primary or not bundled_weights(FALLBACK_FAMILY):
        return [(text, primary)]
    runs: List[Tuple[str, str]] = []
    for ch in text:
        path = primary if (ch.isspace() or ord(ch) in cmap) else fallback
        if runs and runs[-1][1] == path:
            runs[-1] = (runs[-1][0] + ch, path)
        else:
            runs.append((ch, path))
    return runs


def text_width(text: str, family: Optional[str], size: int, weight: int = 400) -> float:
    """Advance width of `text` as draw_text will draw it, fallback runs included."""
    return sum(_truetype(path, int(size)).getlength(run) for run, path in text_runs(text, family, weight))


def draw_text(
    draw: ImageDraw.ImageDraw,
    xy: Tuple[float, float],
    text: str,
    family: Optional[str],
    size: int,
    weight: int = 400,
    fill=None,
    anchor: Optional[str] = None,
) -> None:
    """ImageDraw.text with the font picked by family/weight and per-character fallback.

    A single-font line is passed straight to Pillow, anchor and all — identical to drawing it
    directly. A mixed line is laid out run by run on the chosen font's baseline, honouring the
    horizontal part of the anchor (l/m/r) and the vertical ascender/baseline/descender part.
    """
    runs = text_runs(text, family, weight)
    if len(runs) == 1:
        draw.text(xy, text, font=_truetype(runs[0][1], int(size)), fill=fill, anchor=anchor)
        return

    anchor = anchor or "la"
    horizontal, vertical = anchor[0], anchor[1]
    primary = _truetype(font_path(family, weight), int(size))
    ascent, descent = primary.getmetrics()
    x, y = xy
    total = sum(_truetype(path, int(size)).getlength(run) for run, path in runs)
    if horizontal == "m":
        x -= total / 2
    elif horizontal == "r":
        x -= total
    if vertical in ("a", "t"):
        y += ascent
    elif vertical in ("d", "b"):
        y -= descent
    elif vertical == "m":
        y += (ascent - descent) / 2
    for run, path in runs:
        font = _truetype(path, int(size))
        draw.text((x, y), run, font=font, fill=fill, anchor="ls")
        x += font.getlength(run)

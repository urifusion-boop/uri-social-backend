"""
Type helpers for the poster-style formats (Event Promo, Offer Spotlight, Problems vs Solution):
which bundled font a headline uses, colours that stay readable, and headline lines with one
emphasised phrase in a second colour.
"""
from __future__ import annotations

import colorsys
from typing import Dict, List, Optional, Tuple

from app.utils.fonts import spans_width, text_width
from .brand_tokens import contrast_ratio

# The Brand Playbook's font styles (frontend src/data/fontLibrary.ts) -> the bundled family.
BRAND_FONT_FAMILIES = {
    "serif_elegance": "Playfair Display",
    "modern_sans": "Inter",
    "bold_condensed": "Bebas Neue",
    "script_flow": "Great Vibes",
    "geometric_bold": "Montserrat",
    "slab_serif": "Roboto Slab",
    "display_expressive": "Abril Fatface",
    "minimal_light": "Cormorant",
    "street_block": "Teko",
    "handwritten_casual": "Permanent Marker",
    "afro_display": "Saira Condensed",
    "tech_mono": "JetBrains Mono",
    "rounded_friendly": "Nunito",
    "art_deco": "Poiret One",
    "brush_raw": "Bungee Shade",
    "luxury_hairline": "Cormorant Garamond",
}

# Brand fonts that still read as a bold ad headline. Script, hairline and novelty faces are
# lovely in a logo but unreadable as a 3-line headline on a phone, so those brands get the
# format's own default instead.
HEADLINE_SAFE = {
    "Playfair Display", "Inter", "Bebas Neue", "Montserrat", "Roboto Slab", "Abril Fatface",
    "Teko", "Saira Condensed", "Nunito",
}


def headline_family(brand_context: Optional[dict], default: str) -> str:
    slug = ((brand_context or {}).get("primary_font") or "").strip()
    family = BRAND_FONT_FAMILIES.get(slug)
    return family if family in HEADLINE_SAFE else default


def adjust_to_contrast(hex_color: str, against: str, min_ratio: float) -> str:
    """Same hue, lightness moved (lighter on dark, darker on light) only as far as needed to
    reach min_ratio against `against` — keeps the brand's colour recognisable while readable."""
    if contrast_ratio(hex_color, against) >= min_ratio:
        return hex_color
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    bg = against.lstrip("#")
    bg_light = colorsys.rgb_to_hls(*(int(bg[i:i + 2], 16) / 255 for i in (0, 2, 4)))[1]
    step = 0.02 if bg_light < 0.5 else -0.02
    candidate = hex_color
    for _ in range(60):
        light = min(1.0, max(0.0, light + step))
        rr, gg, bb = colorsys.hls_to_rgb(hue, light, sat)
        candidate = "#{:02X}{:02X}{:02X}".format(round(rr * 255), round(gg * 255), round(bb * 255))
        if contrast_ratio(candidate, against) >= min_ratio:
            return candidate
    return "#FFFFFF" if bg_light < 0.5 else "#000000"


def wrap_words(text: str, family: str, size: int, weight: int, max_width: int,
               tracking: float = 0.0) -> List[str]:
    words = (text or "").split()
    if not words:
        return []
    lines, current = [], words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if text_width(candidate, family, size, weight, tracking) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def fit_headline(text: str, family: str, weight: int, max_width: int, max_lines: int,
                 largest: int, smallest: int, tracking: float = 0.0) -> Tuple[int, List[str]]:
    """The largest size (stepping down by 4) at which `text` wraps into max_lines and no
    single word is wider than the column. Raises ValueError if even `smallest` won't fit."""
    size = largest
    while size >= smallest:
        lines = wrap_words(text, family, size, weight, max_width, tracking)
        if len(lines) <= max_lines and all(text_width(l, family, size, weight, tracking) <= max_width for l in lines):
            return size, lines
        size -= 4
    raise ValueError(f"headline doesn't fit {max_lines} lines at {smallest}px: {text!r}")


def emphasis_spans(line: str, emphasis: Optional[str], base: str, accent: str,
                   highlight: bool = False) -> List[Dict]:
    """A headline line as spans, with `emphasis` (if it appears in this line, case-insensitive)
    in the accent colour — or, with highlight=True, in `base` on a solid accent block."""
    if not emphasis:
        return [{"text": line, "color": base}]
    i = line.lower().find(emphasis.lower())
    if i < 0:
        return [{"text": line, "color": base}]
    j = i + len(emphasis)
    spans = []
    if line[:i]:
        spans.append({"text": line[:i], "color": base})
    spans.append({"text": line[i:j], "color": base, "highlight": accent} if highlight
                 else {"text": line[i:j], "color": accent})
    if line[j:]:
        spans.append({"text": line[j:], "color": base})
    return spans


def lines_with_emphasis(lines: List[str], emphasis: Optional[str]) -> List[Optional[str]]:
    """Per line, the part of `emphasis` that falls on it — an emphasis phrase can wrap across
    two lines ("Healthy Living" -> "...Healthy" / "Living")."""
    if not emphasis:
        return [None] * len(lines)
    target = emphasis.lower().split()
    out: List[Optional[str]] = [None] * len(lines)
    flat = [(li, w) for li, line in enumerate(lines) for w in line.split()]
    words = [w.lower().strip(".,!?:;") for _, w in flat]
    for start in range(len(words) - len(target) + 1):
        if [w.strip(".,!?:;") for w in words[start:start + len(target)]] == [t.strip(".,!?:;") for t in target]:
            by_line: Dict[int, List[str]] = {}
            for li, w in flat[start:start + len(target)]:
                by_line.setdefault(li, []).append(w)
            for li, ws in by_line.items():
                out[li] = " ".join(ws)
            break
    return out


def spans_fit(spans: List[Dict], family: str, size: int, weight: int, max_width: int) -> bool:
    return spans_width(spans, family, size, weight) <= max_width


def _saturation(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return colorsys.rgb_to_hls(r, g, b)[2]


def brand_block_color(brand_colors: Optional[List[str]], tokens: Dict[str, str]) -> str:
    """The brand's own most saturated colour, for use as a BLOCK (pill, card, tag) — unlike the
    accent token, which must work as text on white and so skips pale colours like yellow."""
    valid = [c for c in (brand_colors or []) if isinstance(c, str) and len(c.strip()) == 7 and c.strip().startswith("#")]
    try:
        valid = sorted((c.strip().upper() for c in valid), key=_saturation, reverse=True)
    except ValueError:
        valid = []
    return valid[0] if valid else tokens["accent"]


def block_with_text(fill: str, ink: str = "#1A1A1A", min_ratio: float = 7.0) -> Tuple[str, str]:
    """(fill, text colour) for a block in `fill`: white or ink, whichever reads better; if
    neither reaches min_ratio the fill is deepened (same hue) until white does."""
    from .brand_tokens import darken_to_contrast
    white, dark = contrast_ratio("#FFFFFF", fill), contrast_ratio(ink, fill)
    if max(white, dark) >= min_ratio:
        return fill, ("#FFFFFF" if white >= dark else ink)
    return darken_to_contrast(fill, "#FFFFFF", min_ratio), "#FFFFFF"


def _hue_light(hex_color: str) -> Tuple[float, float]:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hue, light, _ = colorsys.rgb_to_hls(r, g, b)
    return hue, light


def tint(hex_color: str, amount: float) -> str:
    """`hex_color` mixed toward white by `amount` (0..1) — a soft fill in the same hue."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    mix = [round(c + (255 - c) * amount) for c in (r, g, b)]
    return "#{:02X}{:02X}{:02X}".format(*mix)


def brand_pair(brand_colors: Optional[List[str]], tokens: Dict[str, str]) -> Tuple[str, str]:
    """(main, second) brand colours for two-tone pieces — the green and yellow halves of the
    "Fresh Crops" pill. The second is the brand's next colour that clearly differs from the
    main one (another hue, or much lighter/darker); a one-colour brand gets a light tint of
    its own colour instead of a colour it doesn't own."""
    # The brand's own first colour leads (the order the Playbook lists them in), not the most
    # saturated one — a green-and-yellow farm brand's pill is green with a yellow half.
    valid = [c.strip().upper() for c in (brand_colors or [])
             if isinstance(c, str) and len(c.strip()) == 7 and c.strip().startswith("#")]
    main = valid[0] if valid else brand_block_color(brand_colors, tokens)
    try:
        mh, ml = _hue_light(main)
    except ValueError:
        main = tokens["accent"]
        mh, ml = _hue_light(main)
    for c in brand_colors or []:
        if not (isinstance(c, str) and len(c.strip()) == 7 and c.strip().startswith("#")):
            continue
        c = c.strip().upper()
        if c == main.upper():
            continue
        try:
            ch, cl = _hue_light(c)
        except ValueError:
            continue
        hue_gap = min(abs(ch - mh), 1 - abs(ch - mh))
        if (hue_gap > 0.08 or abs(cl - ml) > 0.3) and _saturation(c) > 0.25:
            return main, c
    return main, tint(main, 0.78)


def vivid_block(fill: str, ink: str = "#1A1A1A", min_ratio: float = 7.0) -> Tuple[str, str]:
    """(fill, text colour) like block_with_text, but when neither white nor ink reads on `fill`
    it picks the gentler of two fixes: deepen it for white text, or lighten it toward white for
    dark text. A bright orange stays a light orange with dark text instead of turning brown; a
    mid blue stays blue, a little deeper, with white text."""
    from .brand_tokens import darken_to_contrast
    white, dark = contrast_ratio("#FFFFFF", fill), contrast_ratio(ink, fill)
    if max(white, dark) >= min_ratio:
        return fill, ("#FFFFFF" if white >= dark else ink)
    deep = darken_to_contrast(fill, "#FFFFFF", min_ratio)
    light = None
    for step in range(1, 20):
        candidate = tint(fill, step * 0.05)
        if contrast_ratio(ink, candidate) >= min_ratio:
            light = candidate
            break
    # A colour white already nearly reads on (a mid blue, a brand green) is deepened for white
    # text; a light, warm one (orange, yellow, mint) is lifted for dark text instead of browning.
    if light and white < 3.0:
        return light, ink
    return deep, "#FFFFFF"

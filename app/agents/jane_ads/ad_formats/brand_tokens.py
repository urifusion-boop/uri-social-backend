"""
Real per-brand colour tokens — VSG-01 v3 §1.4 / §10.1.

§10.1 named reconciling colour tokens with "the 28-style Visual Style
Guides document" the largest outstanding dependency. That document turned
out to be app/agents/social_media_manager/services/style_library.py itself
(the "28" is just stale — the library has grown past that count since
VSG-01 v3 was drafted) — but it carries no tokenised colour-role system
under any name: every entry is prose meant to steer Layer 2 AI generation
("Bone white, charcoal, warm gray, muted neutrals"), not a role → hex
mapping. What *does* exist, real and per-brand, is `brand_colors` on
brand_profiles — the Playbook's own colour-swatch field. This module
resolves that into the 6-role vocabulary every ad format's build_document()
already accepts as `tokens=`.

Design: only `accent` is derived from the brand's own colours.
surface/field/ink/ink-quiet/edge stay fixed neutrals, identical to
PLACEHOLDER_TOKENS. Why: `brand_colors` is an unordered swatch list a user
picked in a colour wheel, with no contrast validation of its own, and §1.6
is a HARD constraint (7:1 minimum contrast, "no pale-on-pale" — this
pipeline targets ads viewed outdoors on compressed JPEGs). Deriving the
primary text/background roles from arbitrary brand hues could silently
fail that constraint per brand with nothing to catch it. `accent` is the
one role VSG-01's own vocabulary already treats as brand personality
("price, offer band, star row, single emphasis") — every format shipped so
far only ever paints brand colour through accent, never the other five.

Not yet wired into a live call path: the orchestrator that resolves a
brand profile and actually invokes a format's render() doesn't exist yet
(VSG-01 steps 7-9). The intended call site, once it does, is
`resolve_brand_tokens(brand_profile["brand_colors"])` passed as `tokens=`.
"""
import colorsys
from typing import Dict, List, Optional

from .tokens import PLACEHOLDER_TOKENS

_MIN_CONTRAST_RATIO = 4.5  # WCAG AA, normal text — accent renders as text at
                           # body/headline size in every format that uses it
                           # as text colour (Receipt's total, Review Card's
                           # stars); §1.6's stricter 7:1 targets `ink`
                           # specifically, the primary reading colour, not
                           # a single-emphasis accent.


def _hex_to_rgb(hex_color: str) -> tuple:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _srgb_channel_to_linear(c: int) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _relative_luminance(hex_color: str) -> float:
    r, g, b = (_srgb_channel_to_linear(c) for c in _hex_to_rgb(hex_color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(hex_a: str, hex_b: str) -> float:
    """WCAG relative-luminance contrast ratio, 1:1 (identical) to 21:1
    (black on white) — computed, not estimated."""
    la, lb = _relative_luminance(hex_a), _relative_luminance(hex_b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def _saturation(hex_color: str) -> float:
    r, g, b = (c / 255.0 for c in _hex_to_rgb(hex_color))
    _, _, s = colorsys.rgb_to_hls(r, g, b)
    return s


def resolve_brand_tokens(brand_colors: Optional[List[str]]) -> Dict[str, str]:
    """
    brand_colors: a brand's Playbook colour swatches (unordered hex
    strings), or None/[] for a brand that hasn't set any.

    Returns the full 6-role dict every format's build_document() accepts
    as `tokens=`. Only `accent` varies by brand; the rest are
    PLACEHOLDER_TOKENS' own fixed neutrals (see module docstring).
    """
    tokens = dict(PLACEHOLDER_TOKENS)
    candidates = sorted((brand_colors or []), key=_saturation, reverse=True)
    for candidate in candidates:
        if (
            contrast_ratio(candidate, tokens["surface"]) >= _MIN_CONTRAST_RATIO
            and contrast_ratio(candidate, tokens["field"]) >= _MIN_CONTRAST_RATIO
        ):
            tokens["accent"] = candidate
            break
    # else: no swatch clears the contrast bar (or brand_colors is empty) —
    # tokens["accent"] stays PLACEHOLDER_TOKENS' own default, already
    # verified (see tests) to clear it.
    return tokens


def darken_to_contrast(hex_color: str, against: str, min_ratio: float) -> str:
    """Same hue and saturation as hex_color, lightness reduced only as far
    as needed to reach min_ratio contrast against `against` — general
    helper shared by any format that wants to use a brand colour as a BOLD
    PANEL BACKGROUND (not just as small-emphasis text, which is all
    resolve_brand_tokens' own accent-selection validates). A deep shade of
    the brand's real colour still reads as "this brand's colour"; an
    unrelated neutral does not — used so a caller never has to fall back
    to a generic colour just because the raw accent alone can't carry
    legible text on its own in this different (background, not text) role."""
    hc = hex_color.lstrip("#")
    r, g, b = (int(hc[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    candidate = hex_color
    for _ in range(60):
        rr, gg, bb = colorsys.hls_to_rgb(h, l, s)
        candidate = "#{:02X}{:02X}{:02X}".format(round(rr * 255), round(gg * 255), round(bb * 255))
        if contrast_ratio(candidate, against) >= min_ratio:
            return candidate
        if l <= 0.0:
            break
        l = max(0.0, l - 0.02)
    return candidate


def bold_panel_colors(tokens: Dict[str, str], min_ratio: float = 7.0) -> tuple:
    """(panel_fill, text_color) for a bold panel/banner background —
    ALWAYS this brand's own accent colour family, never a generic neutral
    fallback (explicit product decision, see news_headline.py's own
    module docstring). Picks whichever of white/ink text contrasts better
    against the raw accent; if neither clears min_ratio, darkens the
    accent itself (same hue, less lightness) until one does."""
    accent = tokens["accent"]
    white_contrast = contrast_ratio("#FFFFFF", accent)
    ink_contrast = contrast_ratio(tokens["ink"], accent)
    if max(white_contrast, ink_contrast) >= min_ratio:
        return accent, ("#FFFFFF" if white_contrast >= ink_contrast else tokens["ink"])
    return darken_to_contrast(accent, "#FFFFFF", min_ratio), "#FFFFFF"

"""
News Headline — VSG-01 v3 §2.8 (SEED-077).

"There is a genuine announcement. Strongest on institutional and
education accounts, which have real news more often than retail does." A
photo/scene fills the whole canvas; a headline bar across the lower third
carries the news itself, with a short secondary line and a date stamp
beneath.

Asset source: `generate` (scene only, via _scene_prompt/render) or
`upload_as_is` (a real photo of the actual event/subject). build_document
takes an already-resolved photo_url either way, same pattern as Review
Card's product photo — this module doesn't need to know which path
produced it.

REVISED DECISION on the "breaking news" banner (supersedes §2.8's original
"the label is not the format" stance): the original spec's own hard check
#3 blocked a "BREAKING NEWS"-style banner outright, reasoning it "adds no
information, announces the artifice, and pushes the asset toward the
sensational-content line." That reasoning assumed the banner ITSELF was
the risk. Live market evidence says otherwise — real, large Nigerian
fintechs (Moniepoint among them) run exactly this convention successfully
on Meta today, generically styled (no specific broadcaster's branding),
never paired with an invented claim. The genuinely load-bearing rule was
never "no banner" — it's "no FABRICATED announcement" (hard check #1,
still fully enforced, unchanged). A truthful announcement wrapped in a
familiar, attention-grabbing convention is not deceptive; a false one
would be regardless of how it's framed. Decided explicitly by the
business owner after being shown this precedent, not defaulted into.

The banner itself is Layer 4 (build_document), not AI-generated — boilerplate
"BREAKING NEWS" text drawn the same reliable way the headline is, so it's
typo-proof by construction. The actual news content (headline/secondary_line/
date_stamp) still goes through the exact same SensationalLabelRejected
check as before — the banner is decorative framing, not a substitute for
the headline stating a real fact plainly.

Hard checks (§2.8), as revised:

1. "Real announcements only — fabricated news framing sits close to
   prohibitions on sensational content and deceptive practices." UNCHANGED
   — still the actual load-bearing rule. Caller-side guarantee, same
   category as Receipt's "every figure real and honoured" — this module
   has no way to verify an announcement's truth.

2. Do not imitate a SPECIFIC broadcaster's exact branding (a particular
   network's logo, exact colour signature, or wordmark). UNCHANGED. The
   "BREAKING NEWS" banner below uses a generic red/white convention shared
   across many real outlets and advertisers, not any one broadcaster's
   identity.

3. SUPERSEDED — a "breaking news"-style banner is now permitted (see
   above). SensationalLabelRejected still guards the headline/secondary_line
   text itself: those must still state real information plainly, never a
   vague hype label standing in for actual news.

`requires_isolation=True` per §6: SEED-077 is explicitly named among the
formats requiring the usage cap check ("cap usage across the book before
scaling," SEED-079).
"""
import re
from typing import Dict, Optional, Tuple

from ..visual_slots import resolve_nigerian_setting
from ..layer2_generation import REPRESENTATION_BLOCK, generate_scene
from .brand_tokens import contrast_ratio
from .legibility import assert_legible
from ._text_metrics import wrap_text
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS
from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService

FORMAT = AdFormatDef(
    format_id="SEED-077",
    name="News Headline",
    asset_source="generate",  # or upload_as_is — see module docstring
    layers_used="L2-L4",
    brand_mark="prohibited",  # VSG-01-PROMPTS v2 §6.8 — a brand mark returns this to
                               # an advertisement and forfeits the reportage read
    requires=[],
    requires_isolation=True,  # §6: SEED-077 named explicitly
)

_FONT_HEADLINE = 48  # measured against the real render font: 56px left a
                     # realistic one-line headline ("New campus now open in
                     # Yaba") wrapping to 2 lines, which alone consumed the
                     # whole bar zone's height budget before the secondary
                     # line or date stamp could fit at all
_FONT_SECONDARY = 44
_FONT_DATE = 42
_PADDING = 56
_LINE_HEIGHT_HEADLINE = int(_FONT_HEADLINE * 1.3)
_LINE_HEIGHT_SECONDARY = int(_FONT_SECONDARY * 1.3)

# §2.8: "The asset does not print the words 'Breaking News', and no
# equivalent banner tag." Best-effort, defense-in-depth, same framing as
# every regex guard in this library — not a substitute for retrieval-time
# curation.
#
# No trailing \b after the colon-terminated alternatives: a colon is a
# non-word character, so \b right after one only matches if the character
# following it is a word character — "Breaking: prices" has a space after
# the colon (non-word to non-word, no boundary), so \b silently failed to
# match every colon-terminated label followed by a space. Found by testing
# against realistic labels, not assumed correct from the pattern alone.
# The leading \b is enough on its own since every alternative starts with
# a word character.
_SENSATIONAL_LABEL = re.compile(
    r"\b(?:breaking\s*news\b|breaking:|just\s+in:|news\s+alert:|urgent:|"
    r"developing\s+story\b|exclusive:|you\s+won'?t\s+believe\b)",
    re.IGNORECASE,
)


class SensationalLabelRejected(ValueError):
    """§2.8: 'The label is not the format' — a banner-tag label adds no
    information and announces the artifice rather than carrying real
    news."""
    pass


class ContentOverflowsZone(ValueError):
    """The headline bar is a fixed lower-third zone, same lesson as every
    other L2 format in this library: copy that would spill past it is
    rejected rather than silently rendered over the boundary."""
    pass


def _check_bar_fits(bar_content_height: int, bar_zone_height: int) -> None:
    if bar_content_height > bar_zone_height:
        raise ContentOverflowsZone(
            f"headline bar content needs {bar_content_height}px, taller than the "
            f"bar zone ({bar_zone_height}px) — shorten the headline/secondary line"
        )


def _darken_to_contrast(hex_color: str, against: str, min_ratio: float) -> str:
    """Same hue and saturation as hex_color, lightness reduced only as far
    as needed to reach min_ratio contrast against `against` — explicit
    decision: the banner must always use the brand's own colour family,
    never substitute an unrelated neutral, even when the raw accent value
    alone can't carry legible text. A deep shade of the brand's real pink
    still reads as "this brand's colour"; a generic dark grey does not."""
    import colorsys

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


def _banner_colors(t: Dict[str, str]) -> Tuple[str, str]:
    """(bar_fill, text_color) for the banner's bold top bar. ALWAYS the
    brand's own accent colour family — explicit instruction: the banner
    must use the user's real brand colour, never fall back to an
    unrelated neutral. resolve_brand_tokens only validates accent's
    contrast as TEXT against light surface/field backgrounds (§1.6) —
    using it here as a BOLD BACKGROUND with text on top is a different
    pairing that isn't automatically guaranteed, so when the raw accent
    doesn't carry legible white or ink text on its own, it's darkened
    (same hue/saturation, only lightness reduced) until it does — still
    unmistakably the brand's colour, just a deeper shade of it."""
    accent = t["accent"]
    white_contrast = contrast_ratio("#FFFFFF", accent)
    ink_contrast = contrast_ratio(t["ink"], accent)
    if max(white_contrast, ink_contrast) >= 7.0:
        return accent, ("#FFFFFF" if white_contrast >= ink_contrast else t["ink"])
    return _darken_to_contrast(accent, "#FFFFFF", 7.0), "#FFFFFF"


def build_document(
    photo_url: str,
    headline: str,
    secondary_line: Optional[str] = None,
    date_stamp: Optional[str] = None,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
    show_breaking_news_banner: bool = False,
) -> Dict:
    """
    photo_url: a real photo — either a Layer 2 generated scene (via
    render()) or a real upload_as_is photo of the actual event/subject.
    headline: the news itself, e.g. "Admissions close 14 September" —
    never a "Breaking News"-style label (checked below).

    show_breaking_news_banner: see the module docstring's "REVISED
    DECISION" — an explicit, deliberate business choice, not a default.
    Drawn reliably in Layer 4 (never AI-generated, so it's typo-proof) —
    but its colour is THIS brand's own accent token (see _banner_colors),
    never a fixed hex, same as every other coloured element in this
    format. Defaults to False — every existing caller keeps rendering
    exactly as before unless it opts in.
    """
    if _SENSATIONAL_LABEL.search(headline) or (secondary_line and _SENSATIONAL_LABEL.search(secondary_line)):
        raise SensationalLabelRejected(
            "headline/secondary line reads as a sensational banner-tag label "
            "('breaking news' or equivalent) rather than the news itself (§2.8)"
        )

    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    max_text_width = width - 2 * _PADDING

    bar_zone_height = height - (height * 2) // 3
    bar_y = height - bar_zone_height

    headline_lines = wrap_text(headline, max_text_width, _FONT_HEADLINE, 700)
    secondary_lines = wrap_text(secondary_line, max_text_width, _FONT_SECONDARY) if secondary_line else []
    bar_content_height = (
        2 * _PADDING
        + len(headline_lines) * _LINE_HEIGHT_HEADLINE
        + (24 + len(secondary_lines) * _LINE_HEIGHT_SECONDARY if secondary_lines else 0)
        + (24 + _FONT_DATE if date_stamp else 0)
    )
    _check_bar_fits(bar_content_height, bar_zone_height)

    layers = []
    z = 0

    z += 1
    layers.append({
        "type": "ai_generated_background", "z_index": z,
        "url": photo_url, "x": 0, "y": 0, "width": width, "height": height,
    })

    if show_breaking_news_banner:
        # Two-tone banner sitting in the photo zone's upper-left, the same
        # generic CONVENTION real outlets/advertisers use — deliberately
        # not any one broadcaster's exact wordmark (§2.8 point 2) — but
        # every colour in it comes from THIS brand's own tokens
        # (_banner_colors/t["field"]/t["ink"]), never an invented hex.
        _banner_x, _banner_y = 40, 48
        _banner_w = int(width * 0.58)
        _banner_bar_h = 84
        _bar_fill, _bar_text = _banner_colors(t)
        z += 1
        layers.append({
            "type": "shape", "z_index": z, "shape": "rect",
            "x": _banner_x, "y": _banner_y, "width": _banner_w, "height": _banner_bar_h,
            "fill_color": _bar_fill,
        })
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": "BREAKING",
            "x": _banner_x + 24, "y": _banner_y + 14,
            "font_size": 52, "font_weight": 700, "color": _bar_text,
        })
        z += 1
        layers.append({
            "type": "shape", "z_index": z, "shape": "rect",
            "x": _banner_x, "y": _banner_y + _banner_bar_h, "width": _banner_w, "height": _banner_bar_h,
            "fill_color": t["field"],
        })
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": "NEWS",
            "x": _banner_x + 24, "y": _banner_y + _banner_bar_h + 14,
            "font_size": 52, "font_weight": 700, "color": t["ink"],
        })

    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "rect",
        "x": 0, "y": bar_y, "width": width, "height": bar_zone_height,
        "fill_color": t["field"],
    })

    # A slim accent-coloured rule at the very top of the bar — the one
    # deliberate colour hit this otherwise plain bar carries, same "single
    # accent edge" convention Us vs Them already uses. Purely decorative,
    # not a masthead/logo (§2.8 point 2 still holds: no network-branded
    # chrome), just enough to read as designed rather than a flat plate.
    _ACCENT_RULE_H = 6
    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "rect",
        "x": 0, "y": bar_y, "width": width, "height": _ACCENT_RULE_H,
        "fill_color": t["accent"],
    })

    content_y = bar_y + _PADDING
    z += 1
    layers.append({
        "type": "text", "z_index": z, "content": "\n".join(headline_lines),
        "x": _PADDING, "y": content_y, "font_size": _FONT_HEADLINE, "font_weight": 700, "color": t["ink"],
    })
    content_y += len(headline_lines) * _LINE_HEIGHT_HEADLINE + 24

    if secondary_lines:
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": "\n".join(secondary_lines),
            "x": _PADDING, "y": content_y, "font_size": _FONT_SECONDARY, "color": t["ink"],
        })
        content_y += len(secondary_lines) * _LINE_HEIGHT_SECONDARY + 24

    if date_stamp:
        # Accent-coloured, not muted ink-quiet — reads as a considered
        # highlight (like a date chip) rather than fine-print metadata,
        # since for many announcements this is the single most actionable
        # fact ("when does this happen") a reader takes away.
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": date_stamp,
            "x": _PADDING, "y": content_y, "font_size": _FONT_DATE, "font_weight": 700, "color": t["accent"],
        })

    document = {
        "canvas": {"width": width, "height": height, "background_color": t["surface"]},
        "layers": layers,
    }
    assert_legible(document, t)
    return document


def _scene_prompt(announcement_subject: str, nigerian_setting: str) -> str:
    # Editorial advertising photography, not documentary/photojournalism —
    # see the module docstring's "REVISED DECISION". The genuinely
    # load-bearing rule was always truthfulness (a real subject/event, never
    # fabricated), not a candid/raw photographic STYLE — a confident, well-
    # lit, professionally composed real photo is exactly as truthful as a
    # grainy candid one, and reads as a real advertisement rather than an
    # amateur snapshot.
    return (
        f"Professional editorial advertising photograph of {announcement_subject} "
        f"in {resolve_nigerian_setting(nigerian_setting)}, a real, confident subject "
        "with a genuine, warm, camera-aware expression, shot like a polished brand "
        "campaign image — flattering directional lighting that sculpts the subject "
        "beautifully, rich saturated colour grading, shot on a premium lens with "
        "shallow depth of field separating the subject from a richly detailed, "
        "specific background — real signage-free objects and textures that genuinely "
        "belong to this exact setting, never a generic or empty backdrop. Polished "
        f"and intentional, like a real published advertisement, not a candid snapshot. "
        f"{REPRESENTATION_BLOCK}. Fill the frame edge to edge with the subject "
        "and setting, including the left and right thirds — no large empty "
        "background areas. The only reserved space is a plain strip across "
        "the lower third of the frame for a headline bar."
    )


async def render(
    announcement_subject: str,
    nigerian_setting: str,
    headline: str,
    secondary_line: Optional[str] = None,
    date_stamp: Optional[str] = None,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> bytes:
    """Real Layer 2 generation for the scene, then Layer 4 template-fill —
    the `generate` path. A caller with a real upload_as_is photo instead
    should call build_document directly with their own photo_url."""
    width, height = canvas_size
    photo_url = await generate_scene(_scene_prompt(announcement_subject, nigerian_setting), size=f"{width}x{height}")

    document = build_document(photo_url, headline, secondary_line, date_stamp, canvas_size, tokens)
    return await DocumentRendererService.render_to_png(document)

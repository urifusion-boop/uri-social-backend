"""
News Headline — VSG-01 v3 §2.8 (SEED-077).

"There is a genuine announcement. Strongest on institutional and
education accounts, which have real news more often than retail does." A
photo/scene fills the whole canvas; a headline bar across the lower third
carries the news itself, with a short secondary line and a date stamp
beneath.

Asset source: `generate` (scene only, via §2.8's own verbatim Layer 2
prompt — see _scene_prompt/render) or `upload_as_is` (a real photo of the
actual event/subject). build_document takes an already-resolved
photo_url either way, same pattern as Review Card's product photo — this
module doesn't need to know which path produced it.

**"The label is not the format."** The asset does not print the words
"Breaking News" or any equivalent banner tag — the headline states what
actually happened ("Admissions close 14 September," "New branch now open
in Yaba"). "A label saying 'breaking news' adds no information, announces
the artifice, and pushes the asset toward the sensational-content line."
Enforced, not just documented: SensationalLabelRejected fires on the named
label pattern and its common variants.

Hard checks (§2.8):

1. "Real announcements only — fabricated news framing sits close to
   prohibitions on sensational content and deceptive practices."
   Caller-side guarantee, same category as Receipt's "every figure real
   and honoured" — this module has no way to verify an announcement's
   truth.

2. "Do not imitate a specific broadcaster or masthead. Keep bar geometry
   distinct from any specific news organisation's identity." Enforced by
   construction: this module has no logo/masthead-badge layer type or
   network-branded colour scheme at all — the bar is a plain `field`
   plate using the brand's own tokens, nothing else.

3. SensationalLabelRejected — "breaking news" and equivalent banner-tag
   language, checked on both the headline and the secondary line.

`requires_isolation=True` per §6: SEED-077 is explicitly named among the
formats requiring the usage cap check ("cap usage across the book before
scaling," SEED-079).
"""
import re
from typing import Dict, Optional, Tuple

from ..visual_slots import resolve_nigerian_setting
from ..layer2_generation import generate_scene
from .legibility import assert_legible
from ._text_metrics import wrap_text
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS
from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService

FORMAT = AdFormatDef(
    format_id="SEED-077",
    name="News Headline",
    asset_source="generate",  # or upload_as_is — see module docstring
    layers_used="L2-L4",
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


def build_document(
    photo_url: str,
    headline: str,
    secondary_line: Optional[str] = None,
    date_stamp: Optional[str] = None,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> Dict:
    """
    photo_url: a real photo — either a Layer 2 generated scene (via
    render()) or a real upload_as_is photo of the actual event/subject.
    headline: the news itself, e.g. "Admissions close 14 September" —
    never a "Breaking News"-style label (checked below).
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

    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "rect",
        "x": 0, "y": bar_y, "width": width, "height": bar_zone_height,
        "fill_color": t["field"],
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
        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": date_stamp,
            "x": _PADDING, "y": content_y, "font_size": _FONT_DATE, "color": t["ink-quiet"],
        })

    document = {
        "canvas": {"width": width, "height": height, "background_color": t["surface"]},
        "layers": layers,
    }
    assert_legible(document, t)
    return document


def _scene_prompt(announcement_subject: str, nigerian_setting: str) -> str:
    return (
        f"Photojournalistic image of {announcement_subject} in "
        f"{resolve_nigerian_setting(nigerian_setting)}, candid unposed moment, "
        "natural available light, slight motion, documentary reportage style, "
        "muted realistic colour, clear empty space across the lower third, "
        "shot on a 35mm lens, authentic and unstyled"
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

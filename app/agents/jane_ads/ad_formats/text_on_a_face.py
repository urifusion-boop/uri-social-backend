"""
Text on a Face — VSG-01 v3 §2.7 (SEED-082).

"An owner-operated or service business with a real person to feature." A
close-up (eyes in the upper third — a framing requirement on the source
photograph itself, caller-side, same category as Day 1 → Day 30's
"identical crop, framing and lighting"), one short line across the
lower-mid face on a solid `field` plate.

"The person must be real (§1.2). The mechanism is proximity and
accountability — a specific human standing behind the business. A
generated face defeats it entirely." Where the client has no usable
portrait, this format is unavailable — asset_source `upload_as_is` only,
never `generate`.

Hard checks (§2.7) — **the highest policy risk of the visual formats**,
enforced as guards, not just documented:

1. ViewerPresumption / DisallowedPersonalTopic — "The text states the
   seller's position or an observed situation, never a presumption about
   the viewer; 'are you struggling with…' is the pattern that gets
   rejected... Never touches health, body, finances or personal
   circumstance." Two independent regex guards (_content_guards.py):
   presumes_viewer_attribute catches the named second-person-presumption
   pattern on any topic; mentions_disallowed_personal_topic catches the
   named topics outright, with no second-person framing required to
   trigger it. Best-effort, defense-in-depth — same framing as every
   other regex guard in this library.

2. TextNotOneLine — "one short line." Not a soft guideline here: unlike
   every other format's wrap_text usage, multi-line output on this format
   is a composition failure, not an overflow to catch — a face is not a
   paragraph surface. Checked by wrapping at the plate's real width/font
   and rejecting anything that doesn't fit on a single line, rather than
   silently letting it wrap.

3. PermissionNotOnFile — "Owner or consenting real customer." Same
   structural enforcement as Testimonial + Offer's permission_on_file: a
   required parameter with no default, not an implicit yes.

4. "Text over skin tones is where legibility fails first — solid plate or
   hard outline only." Enforced by construction: this module has no code
   path that draws text without first drawing the `field` plate beneath
   it — there is no floating-text option to reach for by mistake.
"""
from typing import Dict, Tuple

from ._content_guards import mentions_disallowed_personal_topic, presumes_viewer_attribute
from ._text_metrics import wrap_text
from .legibility import assert_legible
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS
from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService

FORMAT = AdFormatDef(
    format_id="SEED-082",
    name="Text on a Face",
    asset_source="upload",  # upload_as_is only — never generate (§1.2)
    layers_used="L4",
    requires=["real_customer_photo"],  # Requirement.REAL_CUSTOMER_PHOTO — a generated
                                        # face paired with a first-person statement is a
                                        # misleading representation (§1.2)
)

_FONT_STATEMENT = 48  # bold, prominent, above the §1.6 floor — checked against the
                      # real render font: 64px left no realistic short statement able
                      # to fit in one line at all (a 35-character line alone measured
                      # 1252px against a ~968px plate), so this was tuned down using
                      # real measurements rather than picked by eye a second time
_PADDING = 56


class PermissionNotOnFile(ValueError):
    """§2.7: 'Owner or consenting real customer.' A required, no-default
    parameter rather than an implicit yes — see module docstring."""
    pass


class ViewerPresumption(ValueError):
    """§2.7: 'are you struggling with…' is the named rejected pattern —
    the text must state the seller's position or an observed situation,
    never a presumption about the viewer."""
    pass


class DisallowedPersonalTopic(ValueError):
    """§2.7: 'Never touches health, body, finances or personal
    circumstance.'"""
    pass


class TextNotOneLine(ValueError):
    """§2.7: 'One short line.' A face is not a paragraph surface — text
    that would wrap to more than one line at this plate's width/font is
    rejected rather than silently wrapped."""
    pass


def _check_one_line(lines) -> None:
    if len(lines) != 1:
        raise TextNotOneLine(
            f"statement wraps to {len(lines)} lines at this width/font — §2.7 requires "
            "one short line; shorten it"
        )


def build_document(
    photo_url: str,
    statement: str,
    permission_on_file: bool,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> Dict:
    """
    photo_url: a real, permission-cleared close-up portrait — never
    generated (§1.2). Framing (eyes in the upper third) is a caller-side
    photography guarantee this module can't verify from a URL alone.
    statement: the seller's own position or an observed situation, one
    short line — see the three hard-check guards in the module docstring.
    """
    if not permission_on_file:
        raise PermissionNotOnFile(
            "owner or consenting customer permission must be on file before this "
            "renders (§2.7) — pass permission_on_file=True only once confirmed"
        )
    if mentions_disallowed_personal_topic(statement):
        raise DisallowedPersonalTopic(
            f"statement {statement!r} touches health, body, finances or personal "
            "circumstance — not permitted regardless of framing (§2.7)"
        )
    if presumes_viewer_attribute(statement):
        raise ViewerPresumption(
            f"statement {statement!r} presumes something about the viewer — the text "
            "must state the seller's position or an observed situation instead (§2.7)"
        )

    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    plate_max_width = width - 2 * _PADDING

    lines = wrap_text(statement, plate_max_width, _FONT_STATEMENT, 700)
    _check_one_line(lines)

    plate_height = int(_FONT_STATEMENT * 1.8)
    plate_y = int(height * 0.52)

    layers = []
    z = 0

    z += 1
    layers.append({
        "type": "composited_product", "z_index": z,
        "url": photo_url, "x": 0, "y": 0, "width": width, "height": height,
    })

    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "rect",
        "x": 0, "y": plate_y, "width": width, "height": plate_height,
        "fill_color": t["field"],
    })

    z += 1
    layers.append({
        "type": "text", "z_index": z, "content": lines[0],
        "x": width // 2, "y": plate_y + (plate_height - _FONT_STATEMENT) // 2,
        "font_size": _FONT_STATEMENT, "font_weight": 700, "color": t["ink"],
        "text_align": "ma",
    })

    document = {
        "canvas": {"width": width, "height": height, "background_color": t["surface"]},
        "layers": layers,
    }
    assert_legible(document, t)
    return document


async def render(*args, **kwargs) -> bytes:
    document = build_document(*args, **kwargs)
    return await DocumentRendererService.render_to_png(document)

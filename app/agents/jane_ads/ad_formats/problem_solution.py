"""
Problem / Solution — VSG-01 v3 §2.3 (SEED-080).

"Default choice when nothing more specific fits. Lowest policy risk in the
library." Two zones, fixed order: problem (top), solution (bottom) — a
naira-cost problem stated concretely, a solution stated as outcome, not
feature. One focal point per zone, roughly 15 words total between them.

Asset source: `generate` — permitted here specifically because the image
illustrates a SITUATION, not the product (§1.2's product-truthfulness rule
still applies: if the solution zone needs to show the actual product, that
half is upload_as_is instead, not generated — out of scope for this
module, which only ever generates).

Each zone's own Layer 2 prompt reserves a real empty band (top 40% for the
problem zone, bottom 40% for the solution zone, per §2.3's own
{{top|bottom}} choice) for Layer 4 text — composited here with a solid
`field` scrim between the generated photo and the text (§1.6: "text over
photography requires a solid field plate, a gradient scrim, or a hard
outline. Never a subtle drop shadow"), never floating text on top of an
uncontrolled background.

Hard checks (§2.3):

1. "The pain named must be one the seller can actually resolve." Caller-
   side guarantee, same category as Receipt's "every figure real and
   honoured" — this module has no way to judge whether a stated problem is
   one a given business can actually fix.

2. "Text-led, so §1.6 applies harder than anywhere else." Enforced, not
   just noted: build_document calls legibility.assert_legible() on its own
   output before returning — the only format module in this library that
   self-checks rather than leaving it to an external caller, because §2.3
   itself singles this format out for a harder bar than the rest.
"""
from typing import Dict, Tuple

from ..visual_slots import resolve_nigerian_setting
from ..layer2_generation import generate_scene
from .legibility import assert_legible
from ._text_metrics import wrap_text
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS
from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService

FORMAT = AdFormatDef(
    format_id="SEED-080",
    name="Problem / Solution",
    asset_source="generate",
    layers_used="L2-L4",
    requires=[],  # `generate` needs no photo from the business at all
)

_FONT_COPY = 56  # text-led format — well above the §1.6 floor, not just at it
_ZONE_TEXT_PADDING = 56
_LINE_HEIGHT = int(_FONT_COPY * 1.3)


class TextOverflowsScrim(ValueError):
    """§2.3: 'Text-led, so §1.6 applies harder than anywhere else.'
    wrap_text only guarantees a line fits horizontally — it says nothing
    about whether the wrapped block's total height still fits inside the
    zone's solid scrim band. Text that overflows the scrim lands directly
    on the raw generated photo behind it, which legibility.py's own check
    would not catch (it samples a text layer's origin point, not the full
    vertical extent of a multi-line block) — caught here explicitly
    instead, before that gap could ship."""
    pass


def _check_fits_scrim(lines, zone_name: str, scrim_height: int) -> None:
    available = scrim_height - _ZONE_TEXT_PADDING
    needed = len(lines) * _LINE_HEIGHT
    if needed > available:
        raise TextOverflowsScrim(
            f"{zone_name} text wraps to {len(lines)} line(s) ({needed}px), taller than "
            f"its scrim band ({available}px) — shorten it (§2.3: roughly 15 words total)"
        )


def _problem_prompt(problem_situation: str, nigerian_setting: str) -> str:
    return (
        f"Documentary photograph illustrating {problem_situation} in "
        f"{resolve_nigerian_setting(nigerian_setting)}, single clear subject, "
        "uncluttered composition, muted desaturated palette, strong empty area "
        "across the top 40 percent, overcast or shaded daylight, realistic, "
        "unstyled, shot on a phone camera"
    )


def _solution_prompt(solution_situation: str, nigerian_setting: str) -> str:
    return (
        f"Documentary photograph illustrating {solution_situation} in "
        f"{resolve_nigerian_setting(nigerian_setting)}, single clear subject, "
        "uncluttered composition, bright natural daylight, warm palette, "
        "resolved and orderly, strong empty area across the bottom 40 percent, "
        "realistic, unstyled, shot on a phone camera"
    )


def build_document(
    problem_image_url: str,
    solution_image_url: str,
    problem_text: str,
    solution_text: str,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> Dict:
    """
    problem_image_url / solution_image_url: already-generated Layer 2
    scenes (produced by render() below via generate_scene) — this function
    only lays them out, matching every other format module's
    build_document/render split.
    problem_text / solution_text: the actual Layer 4 copy — a naira-cost
    problem, an outcome-stated solution (§2.3's caller-side guarantee, not
    enforced here — see module docstring's hard check 1).
    """
    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    zone_height = height // 2
    scrim_height = int(zone_height * 0.4)

    layers = []
    z = 0

    # Problem zone — top half, generated image, scrim + text at the TOP of
    # this zone (matching the {{top}} 40 percent empty band its own Layer 2
    # prompt requested).
    z += 1
    layers.append({
        "type": "ai_generated_background", "z_index": z,
        "url": problem_image_url, "x": 0, "y": 0, "width": width, "height": zone_height,
    })
    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "rect",
        "x": 0, "y": 0, "width": width, "height": scrim_height,
        "fill_color": t["field"],
    })
    problem_lines = wrap_text(problem_text, width - 2 * _ZONE_TEXT_PADDING, _FONT_COPY, 700)
    _check_fits_scrim(problem_lines, "problem", scrim_height)
    z += 1
    layers.append({
        "type": "text", "z_index": z, "content": "\n".join(problem_lines),
        "x": _ZONE_TEXT_PADDING, "y": _ZONE_TEXT_PADDING // 2,
        "font_size": _FONT_COPY, "font_weight": 700, "color": t["ink"],
    })

    # Solution zone — bottom half, generated image, scrim + text at the
    # BOTTOM of this zone (matching its {{bottom}} 40 percent empty band).
    zone2_y = zone_height
    z += 1
    layers.append({
        "type": "ai_generated_background", "z_index": z,
        "url": solution_image_url, "x": 0, "y": zone2_y, "width": width, "height": zone_height,
    })
    scrim2_y = zone2_y + zone_height - scrim_height
    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "rect",
        "x": 0, "y": scrim2_y, "width": width, "height": scrim_height,
        "fill_color": t["field"],
    })
    solution_lines = wrap_text(solution_text, width - 2 * _ZONE_TEXT_PADDING, _FONT_COPY, 700)
    _check_fits_scrim(solution_lines, "solution", scrim_height)
    z += 1
    layers.append({
        "type": "text", "z_index": z, "content": "\n".join(solution_lines),
        "x": _ZONE_TEXT_PADDING, "y": scrim2_y + _ZONE_TEXT_PADDING // 2,
        "font_size": _FONT_COPY, "font_weight": 700, "color": t["ink"],
    })

    document = {
        "canvas": {"width": width, "height": height, "background_color": t["surface"]},
        "layers": layers,
    }
    assert_legible(document, t)
    return document


async def render(
    problem_situation: str,
    solution_situation: str,
    problem_text: str,
    solution_text: str,
    nigerian_setting: str,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> bytes:
    """Full pipeline: two real Layer 2 generations (problem, solution),
    then Layer 4 template-fill — the first render() in this library that
    does real generation rather than just building + rendering an
    already-fully-supplied document."""
    width, height = canvas_size
    zone_size = f"{width}x{height // 2}"

    problem_url = await generate_scene(_problem_prompt(problem_situation, nigerian_setting), size=zone_size)
    solution_url = await generate_scene(_solution_prompt(solution_situation, nigerian_setting), size=zone_size)

    document = build_document(problem_url, solution_url, problem_text, solution_text, canvas_size, tokens)
    return await DocumentRendererService.render_to_png(document)

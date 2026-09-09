"""
Work In Progress — VSG-01-PROMPTS v2 §6.15.

Service-business coverage: "A solar installer, plumber, clinic or school has
no product to photograph." Work visibly underway — hands, tools, a
partially completed job — plus one short line naming what's being done and
where. Full-bleed photo with a `field` text plate near the base, the same
composition shape as Text on a Face.

Asset source: `upload_as_is` preferred (the client's own real work photo),
`generate` permitted only for an illustrative scene — §6.15 is explicit
this is the WEAKER path ("a generated installation is not their work"), so
this module's own render() only ever generates; a caller with a real photo
should pass it straight to build_document instead of calling render().

**Scope note, disclosed rather than silently assumed:** §6.15 frames a real
upload as preferred and generation as a fallback specifically for a
business with no photo of its own work. This module currently only wires
the generate path into VSG-01's retrieval (NO_PHOTO_FORMAT_IDS in
vsg01_orchestrator.py) — there is no `work_in_progress`-specific photo
attestation type among the two the platform currently collects
(`product_photo` / `real_customer_photo`), so a real uploaded work-in-
progress photo isn't yet retrieval-eligible here. build_document itself
takes any photo_url regardless — the gap is in what triggers this format
being offered, not in what this module can render.

Hard checks (§6.15): no implied completed-job claim on generated imagery —
enforced by keeping every prompt this module builds framed as "underway,"
never "finished." No safety-violating depiction (unprotected work at
height, exposed live electrical work) — a caller-side guarantee this
module cannot verify from a URL or a text statement alone, same category as
every other format's real-world-fact hard checks.
"""
from typing import Dict, Tuple

from ..visual_slots import resolve_nigerian_setting
from ._text_metrics import wrap_text
from .legibility import assert_legible
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS, logo_badge_layers

FORMAT = AdFormatDef(
    format_id="SEED-098",
    name="Work In Progress",
    asset_source="generate",  # or upload_as_is — see module docstring
    layers_used="L2-L4",
    brand_mark="required",  # VSG-01-PROMPTS v2 §6.15/§7
    requires=[],
)

_FONT_STATEMENT = 48
_PADDING = 56


def _scene_prompt(trade_activity: str, nigerian_setting: str) -> str:
    return (
        f"Documentary photograph of {trade_activity} underway in "
        f"{resolve_nigerian_setting(nigerian_setting)}, hands and tools visible, "
        "work partially complete, natural available light, candid and unposed, "
        "realistic and unstyled, shot on a phone camera, muted realistic colour"
    )


def build_document(
    scene_image_url: str,
    statement: str,
    brand_logo_url: str = None,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> Dict:
    """
    scene_image_url: a real photo of the client's own work in progress
    (preferred, upload_as_is) or a generated illustrative scene from
    _scene_prompt (this module's own render(), only when no real photo
    exists — §6.15's weaker, fallback path).
    statement: one short line naming what's being done and where — e.g.
    "Solar install underway — Lekki Phase 1". Wrapped to at most 2 lines;
    a caller-side guarantee to keep it genuinely short is assumed, matching
    every other format's text-length hard checks.
    """
    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    plate_max_width = width - 2 * _PADDING

    lines = wrap_text(statement, plate_max_width, _FONT_STATEMENT, 700)[:2]
    line_height = int(_FONT_STATEMENT * 1.3)
    plate_height = len(lines) * line_height + 2 * 28
    plate_y = height - plate_height

    layers = []
    z = 0

    # Full-bleed, edge to edge — "ai_generated_background" regardless of
    # whether this URL came from a real upload or _scene_prompt's generation;
    # this is a scene filling the frame, not a floating product cutout (the
    # shadow/rotation composited_product supports don't apply here).
    z += 1
    layers.append({
        "type": "ai_generated_background", "z_index": z,
        "url": scene_image_url, "x": 0, "y": 0, "width": width, "height": height,
    })

    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "rect",
        "x": 0, "y": plate_y, "width": width, "height": plate_height,
        "fill_color": t["field"],
    })

    z += 1
    layers.append({
        "type": "text", "z_index": z, "content": "\n".join(lines),
        "x": _PADDING, "y": plate_y + 28, "font_size": _FONT_STATEMENT, "font_weight": 700, "color": t["ink"],
    })

    badge_layers, z = logo_badge_layers(brand_logo_url, width, height, z)
    layers.extend(badge_layers)

    document = {
        "canvas": {"width": width, "height": height, "background_color": t["surface"]},
        "layers": layers,
    }
    assert_legible(document, t)
    return document


async def render(trade_activity: str, nigerian_setting: str, statement: str, **kwargs) -> bytes:
    """Generate an illustrative scene, then build + render — the fallback path
    for a business with no real work-in-progress photo of its own (§6.15).
    A caller WITH a real photo should call build_document directly instead."""
    from ..layer2_generation import generate_scene
    from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService

    scene_url = await generate_scene(_scene_prompt(trade_activity, nigerian_setting), size="1080x1080")
    document = build_document(scene_url, statement, **kwargs)
    return await DocumentRendererService.render_to_png(document)

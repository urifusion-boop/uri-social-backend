"""
Humour / Cartoon — VSG-01 v3 §2.12 (SEED-089).

"The brand can carry it and the joke is about a shared situation." Single
panel, one idea, punchline in the image rather than the caption — the
humour is a visual sight gag the illustration itself carries, not a
caption riding over an unrelated scene. This does not relax §1.1: Layer 2
still never produces readable text; this format simply doesn't need any
Layer 4 caption text layered on top the way every other format does,
since the joke is the picture.

Asset source: `generate` only (illustration, no product claim) — no
upload_as_is path exists for this format; there is no "real photo" of a
cartoon.

Hard checks (§2.12):

1. "The joke targets a situation, never a group. No ethnic, regional or
   religious stereotype — Nigeria has live fault lines on all three, and
   a templated joke propagating across hundreds of accounts turns one
   lapse into a book-wide incident." Left as a caller-side/retrieval-time
   judgement, same honest framing as Starter Pack's identical-shaped hard
   check: whether a joke "targets a situation" versus "targets a group"
   is a semantic judgement about framing, not something a keyword guard
   could do justice to — it would either miss real stereotyping or
   wrongly flag legitimate situational humour that happens to mention a
   place or culture.

2. HumanReviewRequired — "Needs human review before shipping on the ₦15k
   tier, where no operator sees the asset first." The one format in this
   entire library where §5's framing ("automated blocks, not review
   prompts... no human sees the asset before it ships") is explicitly
   overridden. Enforced structurally, same pattern as every permission-
   gated format in this library: human_reviewed has no default value, so
   a caller cannot silently skip the review this format specifically
   requires that no other one does.
"""
from typing import Dict, Optional, Tuple

from ..visual_slots import resolve_nigerian_setting
from ..layer2_generation import generate_scene
from .legibility import assert_legible
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS
from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService

FORMAT = AdFormatDef(
    format_id="SEED-089",
    name="Humour / Cartoon",
    asset_source="generate",
    layers_used="L2-L4",
    brand_mark="prohibited",  # VSG-01-PROMPTS v2 §6.12 — a logo kills a joke;
                               # attribution comes from Meta's Sponsored label
    requires=[],
)


class HumanReviewRequired(ValueError):
    """§2.12: 'Needs human review before shipping on the ₦15k tier, where
    no operator sees the asset first.' A required, no-default parameter
    rather than an implicit yes — see module docstring."""
    pass


def build_document(
    illustration_url: str,
    human_reviewed: bool,
    brand_logo_url: Optional[str] = None,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> Dict:
    """
    illustration_url: a real Layer 2 generated cartoon/illustration (via
    render()) carrying the joke itself — no caption text is composited
    here; "punchline in the image rather than the caption" (§2.12).
    human_reviewed: confirmation a human has reviewed this specific asset
    before it ships — required, see HumanReviewRequired above.
    """
    if not human_reviewed:
        raise HumanReviewRequired(
            "this format requires human review before shipping (§2.12) — "
            "pass human_reviewed=True only once that review has actually happened"
        )

    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size

    layers = []
    z = 0

    z += 1
    layers.append({
        "type": "ai_generated_background", "z_index": z,
        "url": illustration_url, "x": 0, "y": 0, "width": width, "height": height,
    })

    # Brand mark reserved at a corner (§1.5 defers actual position/size/
    # treatment to the Brand Overlay Spec — a simple placement here, not
    # a final one, same caveat as every other format's logo reservation).
    if brand_logo_url:
        z += 1
        layers.append({
            "type": "brand_asset", "z_index": z,
            "url": brand_logo_url, "x": width - 176, "y": height - 88, "width": 120, "height": 48,
        })

    document = {
        "canvas": {"width": width, "height": height, "background_color": t["surface"]},
        "layers": layers,
    }
    assert_legible(document, t)
    return document


def _illustration_prompt(situation: str, nigerian_setting: str) -> str:
    return (
        f"Single-panel cartoon illustration depicting {situation}, in "
        f"{resolve_nigerian_setting(nigerian_setting)}, humorous and lighthearted, "
        "clean simple linework, bright flat colours, expressive characters, "
        "one single clear visual punchline, comic illustration style"
    )


async def render(
    situation: str,
    nigerian_setting: str,
    human_reviewed: bool,
    brand_logo_url: Optional[str] = None,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> bytes:
    width, height = canvas_size
    illustration_url = await generate_scene(_illustration_prompt(situation, nigerian_setting), size=f"{width}x{height}")

    document = build_document(illustration_url, human_reviewed, brand_logo_url, canvas_size, tokens)
    return await DocumentRendererService.render_to_png(document)

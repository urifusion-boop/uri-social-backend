"""
The Censored Item — VSG-01 v3 §2.10 (SEED-083).

"There is a real pending reveal." Subject visible, one element obscured by
a hard-edged `ink` bar. Reveal date or mechanism stated.

**The obscured item must be the real product (§1.2).** "v2 permitted
generating it; a generated product behind a redaction bar is still a
generated product, and the reveal will not match." Where the product
cannot be photographed because it does not yet exist, this format is
unavailable — asset_source `upload_as_is` (plain) or `recomposite` (a
generated background behind the real, unmodified product — see
render_recomposite/_background_prompt for §2.10's own verbatim scene-
treatment prompt), never `generate` for the product itself.

"Redaction bar composited in Layer 4, never generated" — this module's
redaction bar is a plain solid `ink` rect (a "hard-edged bar"). §2.10 also
permits "heavy blur" as an alternative treatment; that isn't implemented
here — DocumentRendererService has no region-blur primitive (only solid
shape fills), and adding real Gaussian-blur-over-a-photo-region compositing
is out of scope for this format alone. Known gap, not silently unmet: the
hard-edged bar is the only redaction style this module currently supports.

Hard checks (§2.10):

1. "Prohibited: obscuring price." Mechanically enforced, best-effort:
   ObscuresPriceRejected fires if the caller's own description of what's
   hidden (`what_is_obscured`) mentions price/cost/currency language —
   this module has no way to inspect the actual photo pixels under the
   bar, so this is the only surface it can check.

2. "Prohibited: implying withheld adult or shocking content, or redacting
   where nothing is actually revealed." The second half is enforced
   structurally: `reveal_text` (the date or mechanism) has no default —
   a caller cannot silently omit stating what's actually being revealed.
   The first half ("implying withheld adult or shocking content") is a
   caller-side judgement this module cannot assess from text alone.

3. "The obscured item must be the real product." Caller-side guarantee at
   the asset-source level, same as every upload_as_is/recomposite format
   in this library — this module places whatever photo URL it's given
   and has no way to verify it's genuinely the real, unaltered product.

`requires_isolation=True` per §6: SEED-083 is named explicitly among the
formats requiring the usage cap check.
"""
import re
from typing import Dict, Optional, Tuple

from ..layer2_generation import generate_scene
from .legibility import assert_legible
from ._text_metrics import wrap_text
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS
from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService

FORMAT = AdFormatDef(
    format_id="SEED-083",
    name="The Censored Item",
    asset_source="upload",  # or `recomposite` — see module docstring; never `generate`
    layers_used="L4",
    brand_mark="required",  # VSG-01-PROMPTS v2 §6.10
    requires=["product_photo"],
    requires_isolation=True,  # §6 names SEED-083 explicitly
)

_FONT_REVEAL = 48
_PADDING = 56

# §2.10: "Prohibited: obscuring price." Best-effort, defense-in-depth —
# this can only inspect the caller's own description of what's hidden,
# not the actual pixels under the redaction bar.
# \w* on price/cost/amount/fee, not \b — "costs" and "pricing" must match
# too; a bare \b immediately after "cost" only matches the exact word, not
# any inflected form, found by testing "how much it costs" specifically.
_PRICE_WORD = re.compile(r"\b(?:pric\w*|cost\w*|₦|naira|amount\w*|fee\w*)\b", re.IGNORECASE)


class ObscuresPriceRejected(ValueError):
    """§2.10: 'Prohibited: obscuring price.'"""
    pass


class ContentOverflowsZone(ValueError):
    """The reveal-text band is a fixed zone, same lesson as every other L2/
    text-over-photo format in this library: copy that would spill past it
    is rejected rather than silently rendered over the boundary."""
    pass


def _check_reveal_fits(reveal_content_height: int, reveal_zone_height: int) -> None:
    if reveal_content_height > reveal_zone_height:
        raise ContentOverflowsZone(
            f"reveal_text needs {reveal_content_height}px, taller than the reveal "
            f"zone ({reveal_zone_height}px) — shorten it"
        )


def build_document(
    product_image_url: str,
    obscure_x: int,
    obscure_y: int,
    obscure_width: int,
    obscure_height: int,
    reveal_text: str,
    what_is_obscured: str,
    background_image_url: Optional[str] = None,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> Dict:
    """
    product_image_url: the real, unaltered product photo — never generated
    (§1.2). obscure_x/y/width/height: where the redaction bar sits over
    it, in the caller's own coordinates against canvas_size — this module
    has no way to locate "the interesting part" of an arbitrary photo
    itself.
    reveal_text: the reveal date or mechanism (§2.10 requires this be
    stated; no default, so it can't be silently omitted).
    what_is_obscured: a short caller-facing description of what's hidden
    (e.g. "the new lid design") — used only for the price guard below,
    not necessarily rendered.
    background_image_url: a generated scene behind the real product (the
    `recomposite` path — see render_recomposite) — omit for a plain
    upload_as_is photo against the brand's own surface token.
    """
    if _PRICE_WORD.search(what_is_obscured):
        raise ObscuresPriceRejected(
            f"what_is_obscured={what_is_obscured!r} reads as hiding a price — "
            "not permitted regardless of framing (§2.10)"
        )

    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size
    max_text_width = width - 2 * _PADDING

    photo_zone_height = int(height * 0.78)
    reveal_zone_height = height - photo_zone_height

    layers = []
    z = 0

    if background_image_url:
        z += 1
        layers.append({
            "type": "ai_generated_background", "z_index": z,
            "url": background_image_url, "x": 0, "y": 0, "width": width, "height": photo_zone_height,
        })

    z += 1
    layers.append({
        "type": "composited_product", "z_index": z,
        "url": product_image_url, "x": 0, "y": 0, "width": width, "height": photo_zone_height,
    })

    # The redaction bar itself — a plain solid `ink` rect, composited here
    # in Layer 4, never generated.
    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "rect",
        "x": obscure_x, "y": obscure_y, "width": obscure_width, "height": obscure_height,
        "fill_color": t["ink"],
    })

    reveal_lines = wrap_text(reveal_text, max_text_width, _FONT_REVEAL, 700)
    reveal_line_height = int(_FONT_REVEAL * 1.3)
    reveal_content_height = 2 * _PADDING + len(reveal_lines) * reveal_line_height
    _check_reveal_fits(reveal_content_height, reveal_zone_height)

    reveal_y = photo_zone_height
    z += 1
    layers.append({
        "type": "shape", "z_index": z, "shape": "rect",
        "x": 0, "y": reveal_y, "width": width, "height": reveal_zone_height,
        "fill_color": t["field"],
    })
    z += 1
    layers.append({
        "type": "text", "z_index": z, "content": "\n".join(reveal_lines),
        "x": _PADDING, "y": reveal_y + _PADDING, "font_size": _FONT_REVEAL,
        "font_weight": 700, "color": t["accent"],
    })

    document = {
        "canvas": {"width": width, "height": height, "background_color": t["surface"]},
        "layers": layers,
    }
    assert_legible(document, t)
    return document


def _background_prompt() -> str:
    """§2.10's own verbatim scene treatment for the recomposite path."""
    return (
        "Dramatic single-source side lighting, dark neutral background, "
        "high contrast, generous empty space around the subject, "
        "studio product photography"
    )


async def render(*args, **kwargs) -> bytes:
    document = build_document(*args, **kwargs)
    return await DocumentRendererService.render_to_png(document)


async def render_recomposite(
    product_image_url: str,
    obscure_x: int,
    obscure_y: int,
    obscure_width: int,
    obscure_height: int,
    reveal_text: str,
    what_is_obscured: str,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> bytes:
    """The `recomposite` path: a real Layer 2 generated background behind
    the real, unaltered product — the product itself is never regenerated
    (§1.2). Uses §2.10's own verbatim scene-treatment prompt."""
    width, height = canvas_size
    photo_zone_height = int(height * 0.78)
    background_url = await generate_scene(_background_prompt(), size=f"{width}x{photo_zone_height}")

    document = build_document(
        product_image_url, obscure_x, obscure_y, obscure_width, obscure_height,
        reveal_text, what_is_obscured, background_url, canvas_size, tokens,
    )
    return await DocumentRendererService.render_to_png(document)

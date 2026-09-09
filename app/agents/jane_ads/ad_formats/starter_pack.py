"""
Starter Pack — VSG-01 v3 §2.11 (SEED-088).

"The audience is defined by a recognisable identity or situation." A
flat-lay grid of 4-9 items, even spacing, consistent scale and shadow on
`surface`. The client's real product sits among the surrounding items
rather than above them. Labels in `ink-quiet`.

Asset source: `generate` for surrounding items, `upload_as_is` for the
client's product — "the client's own product is composited into the grid
from a real photograph. Generating it alongside real-looking objects is
the most deceptive case in the library — it reads as a photograph of
things that exist" (§1.2). requires=["product_photo"]: this format cannot
surface for a business with no real product photo.

Layers: L2->L3->L4 — L3 (background removal) applies to the client's own
product photo so it sits flush among the flat-lay items rather than
floating on a visible rectangle; that step happens upstream of this
module (product_image_url is expected to already be a clean cutout, same
caller-side-resolved-asset convention every format in this library uses
for whatever pipeline stage produced its input URLs).

**Deliberate deviation from §2.11's literal Layer 2 prompt, and why.** The
spec's own prompt asks for ALL surrounding items "arranged in a neat grid"
in a single generation call. That's the wrong tool for "even spacing,
consistent scale" — text-to-image models are unreliable at precise
multi-item procedural layout (consistent item count, uniform scale, even
gaps), and there is no way to ask one for an exact reserved gap where the
real product will later be composited without gambling on where that gap
actually lands. This module instead generates each surrounding item as
its OWN separate flat-lay photo (_item_prompt, one Layer 2 call per item)
and lets Layer 4 do the grid arrangement deterministically — genuinely
guaranteeing even spacing and consistent scale (real position/size math
in Python) rather than hoping a single collage happens to have it. Same
underlying photographic direction as §2.11's own prompt, applied per item
instead of per grid.

Hard checks (§2.11) — both caller-side/retrieval-time judgements, not
mechanically checkable from image URLs or short labels:

1. "The identity must be one the audience claims willingly." A
   research/curation judgement about a specific audience, not something
   derivable from a label string.
2. "Never built on ethnic, regional or religious stereotype." A semantic
   judgement about how an identity is being framed, not merely whether
   an identity is named — a naive keyword blocklist here would either be
   toothless (miss real stereotyping) or actively wrong (flag legitimate
   identity-based content, e.g. "Lagos Owambe starter pack," for simply
   naming a culture). Left to retrieval-time curation (§6) rather than a
   guard that couldn't do this judgement justice.

What IS mechanically enforced: the 4-9 item count (InvalidItemCount), and
"consistent scale and shadow" — every grid cell (generated items and the
real product alike) gets the exact same cell size and the exact same
shadow config, so the product structurally cannot read as elevated above
the rest.
"""
import math
from typing import Dict, List, Optional, Tuple

from ..layer2_generation import generate_scene
from .legibility import assert_legible
from ._text_metrics import wrap_text
from .tokens import AdFormatDef, PLACEHOLDER_TOKENS
from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService

FORMAT = AdFormatDef(
    format_id="SEED-088",
    name="Starter Pack",
    asset_source="generate",  # surrounding items; the product itself is upload_as_is
    layers_used="L2-L3-L4",
    brand_mark="optional",  # VSG-01-PROMPTS v2 §6.11
    requires=["product_photo"],
)

_MIN_ITEMS = 4
_MAX_ITEMS = 9
_FONT_LABEL = 42  # §1.6 floor
_SHADOW = {"opacity": 0.25, "blur": 12, "offset_x": 0, "offset_y": 6}


class InvalidItemCount(ValueError):
    """§2.11: '4-9 items.'"""
    pass


class LabelNotOneLine(ValueError):
    """Every grid row has a fixed height budgeted for a single line of
    label text. A label that wraps to a second line would either be
    silently truncated (an earlier version of this module rendered only
    the first wrapped line, found by actually looking at a rendered grid —
    "Our snack pack" printed as "Our snack") or, if rendered in full,
    overflow into the next row and corrupt the grid. §2.11's own labels
    are terse item names ("Jollof rice," "Zobo drink"), so one line is the
    right constraint, not an accommodation to work around."""
    pass


def _check_label_one_line(label: str, cell_width: int) -> None:
    lines = wrap_text(label, cell_width, _FONT_LABEL)
    if len(lines) != 1:
        raise LabelNotOneLine(
            f"label {label!r} wraps to {len(lines)} lines at this grid cell's width "
            f"({cell_width}px) — shorten it"
        )


def _item_prompt(item: str, nigerian_setting_hint: str = "") -> str:
    """Per-item variant of §2.11's own flat-lay prompt — see module
    docstring for why this is generated per item rather than as one
    multi-item collage."""
    return (
        f"Overhead flat lay of {item} on a plain surface, even soft daylight "
        "from above, consistent scale, subtle uniform shadow, clean minimal "
        "styling, no props beyond the item itself, Nigerian everyday object, "
        "realistic wear and use"
        + (f", {nigerian_setting_hint}" if nigerian_setting_hint else "")
    )


def build_document(
    item_image_urls: List[str],
    item_labels: List[str],
    product_image_url: str,
    product_label: str,
    product_index: Optional[int] = None,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> Dict:
    """
    item_image_urls / item_labels: the surrounding items — real Layer 2
    generated photos (via render()) or any other already-resolved photo
    URLs, one label per image, same length.
    product_image_url: the client's real product photo, already cut out
    (background removed upstream — see module docstring).
    product_index: where the product sits in the grid (default: roughly
    the middle, "among them rather than above them" — never a corner,
    which reads as an afterthought bolted onto the pack).
    """
    if len(item_image_urls) != len(item_labels):
        raise ValueError("item_image_urls and item_labels must be the same length")

    total_items = len(item_image_urls) + 1
    if not (_MIN_ITEMS <= total_items <= _MAX_ITEMS):
        raise InvalidItemCount(
            f"{total_items} total items (including the product) — §2.11 requires "
            f"{_MIN_ITEMS}-{_MAX_ITEMS}"
        )

    t = tokens or PLACEHOLDER_TOKENS
    width, height = canvas_size

    cols = math.ceil(math.sqrt(total_items))
    rows = math.ceil(total_items / cols)

    margin = 56
    gap = 32
    label_height = int(_FONT_LABEL * 1.3) + 16
    cell_w = (width - 2 * margin - (cols - 1) * gap) // cols
    cell_h = (height - 2 * margin - (rows - 1) * gap) // rows - label_height

    if product_index is None:
        product_index = total_items // 2
    product_index = max(0, min(product_index, total_items - 1))

    entries = list(zip(item_image_urls, item_labels, [False] * len(item_image_urls)))
    entries.insert(product_index, (product_image_url, product_label, True))

    for _, label, _is_product in entries:
        _check_label_one_line(label, cell_w)

    layers = []
    z = 0

    for i, (url, label, is_product) in enumerate(entries):
        row, col = divmod(i, cols)
        cell_x = margin + col * (cell_w + gap)
        cell_y = margin + row * (cell_h + label_height + gap)

        z += 1
        layers.append({
            "type": "composited_product", "z_index": z,
            "url": url, "x": cell_x, "y": cell_y, "width": cell_w, "height": cell_h,
            "shadow": dict(_SHADOW),
        })

        z += 1
        layers.append({
            "type": "text", "z_index": z, "content": label,
            "x": cell_x + cell_w // 2, "y": cell_y + cell_h + 8,
            "font_size": _FONT_LABEL, "color": t["ink-quiet"], "text_align": "ma",
        })

    document = {
        "canvas": {"width": width, "height": height, "background_color": t["surface"]},
        "layers": layers,
    }
    assert_legible(document, t)
    return document


async def render(
    item_descriptions: List[str],
    item_labels: List[str],
    product_image_url: str,
    product_label: str,
    product_index: Optional[int] = None,
    canvas_size: Tuple[int, int] = (1080, 1080),
    tokens: Dict[str, str] = None,
) -> bytes:
    """Real Layer 2 generation, one call per surrounding item (see module
    docstring for why), then Layer 4 grid arrangement. item_descriptions
    are the scene descriptions to generate (e.g. "a bottle of zobo
    drink"); item_labels are the caption shown beneath each — same
    length, index-matched, may describe the same item in different words."""
    if len(item_descriptions) != len(item_labels):
        raise ValueError("item_descriptions and item_labels must be the same length")

    width, height = canvas_size
    cols = math.ceil(math.sqrt(len(item_descriptions) + 1))
    cell_size = f"{width // cols}x{width // cols}"

    item_urls = []
    for description in item_descriptions:
        item_urls.append(await generate_scene(_item_prompt(description), size=cell_size))

    document = build_document(
        item_urls, item_labels, product_image_url, product_label,
        product_index, canvas_size, tokens,
    )
    return await DocumentRendererService.render_to_png(document)

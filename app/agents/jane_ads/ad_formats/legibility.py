"""
The compression test — VSG-01 v3 §1.6 — as an automated pre-render check.

§1.6's own procedure is manual/operational: "export at JPEG quality 40,
view at 30% scale. If the copy is not readable, the asset fails." That
isn't automatable as a hard pass/fail without OCR (not available in this
environment, and even a real OCR pass would answer a different question —
whether a machine can still segment glyphs at lower fidelity, not whether
a human eye finds the result readable). What IS automatable, precisely,
are the deterministic rules §1.6 states immediately above the compression
paragraph — the numeric/structural thresholds that exist *because* they
survive that exact scenario:

- Minimum body type 42px at 1080 canvas width, headline 72px+ (checked
  uniformly as a >=42px floor — this format library doesn't currently tag
  which text layers are "headline" vs "body," and 72 implies 42 anyway).
- Contrast ratio 7:1 minimum between text and whatever sits behind it.
  `accent` — used only for single emphasis, not extended reading — is
  checked against 4.5:1 instead, the same reasoning brand_tokens.py's own
  accent-selection already applies.
- No hairline strokes: <2px, the floor every format in this library had
  already independently converged on except one (found and fixed by this
  checker — see the commit this ships in).
- No weights below Medium. KNOWN GAP: DocumentRendererService._load_font
  only has two font files (Regular/400, Bold/700) — there is no actual
  Medium (500) weight available to render. This checks font_weight >= 400
  (the renderer's real floor) rather than failing every non-bold text
  layer in the entire library against a weight the renderer cannot
  produce. Giving DocumentRendererService a real Medium-weight font file
  is separate, later work if stricter enforcement is wanted.
- Text over photography requires a solid field plate or scrim — never
  placed directly on an ai_generated_background/composited_product/
  brand_asset layer with nothing solid interposed.

Checking a *document* (the {"canvas":..., "layers":[...]} shape every
format's build_document() returns) rather than a rendered PNG means every
one of these is checkable before a single pixel is drawn — matching
§1.6's "automated, inside the pipeline" as a pre-render gate, not a
post-render forensic pass.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional

from .brand_tokens import contrast_ratio
from .tokens import PLACEHOLDER_TOKENS

MIN_BODY_FONT_SIZE = 42
MIN_HAIRLINE_STROKE = 2
MIN_FONT_WEIGHT = 400  # see module docstring — the renderer's real floor, not true Medium (500)

_ROLE_MIN_CONTRAST = {
    "ink": 7.0,
    "ink-quiet": 7.0,  # still extended-reading text (attribution/disclaimers) —
                       # §1.6 carves out no exception for secondary text
    "accent": 4.5,     # single emphasis, not extended reading (WCAG AA) —
                       # matches brand_tokens.py's own accent-selection bar
}
_DEFAULT_MIN_CONTRAST = 7.0  # a colour that matches no known token role — fail
                             # closed to the strict bar rather than guess

_PHOTO_LAYER_TYPES = {"ai_generated_background", "composited_product", "brand_asset"}
_FILL_SHAPE_TYPES = {"rect", "rounded_rect"}
_BACKGROUND_CANDIDATE_TYPES = _PHOTO_LAYER_TYPES | {"shape"}


@dataclass
class LegibilityIssue:
    layer_index: int
    issue: str
    detail: str


class IllegibleDocument(ValueError):
    """One of §1.6's automated blocks — raised rather than shipping a
    document with an unreadable or non-compliant text layer."""
    pass


def _role_for_color(color: str, tokens: Dict[str, str]) -> Optional[str]:
    reverse = {v.upper(): k for k, v in tokens.items()}
    return reverse.get(color.upper())


def _covers_point(layer: Dict, x: float, y: float) -> bool:
    lx, ly = layer.get("x", 0), layer.get("y", 0)
    lw, lh = layer.get("width"), layer.get("height")
    if lw is None or lh is None:
        return False
    return lx <= x <= lx + lw and ly <= y <= ly + lh


def _effective_background(document: Dict, text_layer: Dict, all_layers: List[Dict]):
    """Returns (kind, value): ("solid", hex), ("photo", layer_type), or
    ("canvas", hex) — whichever layer is actually topmost beneath the text
    layer's own origin point, real z-order and bounding-box math against
    the same document a renderer would paint, not a guess."""
    x, y = text_layer.get("x", 0), text_layer.get("y", 0)
    z = text_layer.get("z_index", 0)
    candidates = []
    for layer in all_layers:
        if layer is text_layer or layer.get("z_index", 0) >= z:
            continue
        if layer.get("type") not in _BACKGROUND_CANDIDATE_TYPES:
            continue
        if layer.get("type") == "shape" and (
            layer.get("shape") not in _FILL_SHAPE_TYPES or not layer.get("fill_color")
        ):
            continue  # a line, or an unfilled shape — establishes no background
        if _covers_point(layer, x, y):
            candidates.append(layer)
    if not candidates:
        return ("canvas", document["canvas"]["background_color"])
    topmost = max(candidates, key=lambda l: l.get("z_index", 0))
    if topmost["type"] in _PHOTO_LAYER_TYPES:
        return ("photo", topmost["type"])
    return ("solid", topmost["fill_color"])


def check_legibility(document: Dict, tokens: Optional[Dict[str, str]] = None) -> List[LegibilityIssue]:
    t = tokens or PLACEHOLDER_TOKENS
    layers = document.get("layers", [])
    issues: List[LegibilityIssue] = []

    for idx, layer in enumerate(layers):
        layer_type = layer.get("type")

        if layer_type == "text":
            content = layer.get("content", "")
            if not content:
                continue  # matches _render_text's own early-return-on-empty-content

            font_size = layer.get("font_size", 48)
            if font_size < MIN_BODY_FONT_SIZE:
                issues.append(LegibilityIssue(
                    idx, "font_too_small",
                    f"font_size={font_size} is below the §1.6 floor of {MIN_BODY_FONT_SIZE}px",
                ))

            font_weight = layer.get("font_weight", 400)
            if font_weight < MIN_FONT_WEIGHT:
                issues.append(LegibilityIssue(
                    idx, "weight_too_light",
                    f"font_weight={font_weight} is below {MIN_FONT_WEIGHT} (the renderer's Regular floor)",
                ))

            kind, value = _effective_background(document, layer, layers)
            if kind == "photo":
                issues.append(LegibilityIssue(
                    idx, "text_over_photography_without_scrim",
                    f"text sits directly on a {value!r} layer with no solid field/scrim beneath it (§1.6)",
                ))
            else:
                color = layer.get("color", "#FFFFFF")
                role = _role_for_color(color, t)
                min_contrast = _ROLE_MIN_CONTRAST.get(role, _DEFAULT_MIN_CONTRAST)
                ratio = contrast_ratio(color, value)
                if ratio < min_contrast:
                    role_note = f" for role {role!r}" if role else " (unrecognised colour, strict bar applied)"
                    issues.append(LegibilityIssue(
                        idx, "contrast_too_low",
                        f"{color} on {value} is {ratio:.2f}:1, below the {min_contrast}:1 floor{role_note}",
                    ))

        elif layer_type == "shape":
            stroke = layer.get("stroke_width")
            if stroke is None:
                stroke = layer.get("border_width")
            if stroke is not None and stroke < MIN_HAIRLINE_STROKE:
                issues.append(LegibilityIssue(
                    idx, "hairline_stroke",
                    f"stroke/border width {stroke}px is below the {MIN_HAIRLINE_STROKE}px hairline floor",
                ))

    return issues


def assert_legible(document: Dict, tokens: Optional[Dict[str, str]] = None) -> None:
    """The automated block itself — raises IllegibleDocument rather than
    shipping a document with an unreadable or non-compliant text layer."""
    issues = check_legibility(document, tokens)
    if issues:
        detail = "; ".join(f"[layer {i.layer_index}] {i.issue}: {i.detail}" for i in issues)
        raise IllegibleDocument(f"{len(issues)} legibility issue(s): {detail}")

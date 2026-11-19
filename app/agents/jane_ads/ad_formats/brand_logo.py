"""
Where a poster's standalone logo goes: where the brand chose in its Brand Playbook.

A format marks its free-standing logo layer with role="brand_logo" at the spot its own layout
would use. place_brand_logo then moves that layer to the brand's own logo_position, scaled to its
logo_size, and lines the logo up with that side (left / centre / right) instead of centring it in
its box. The brand's choice is kept unless the poster's own text or elements already sit there; only
then is the next clear position used (same rule as organic posts' overlay_logo_smart: the owner's
preference wins whenever it isn't the problem). A logo that is part of a design element (the one
inside Portrait + Headline's call-to-action pill) carries no role and is never moved.
"""
from typing import Dict, List, Optional, Tuple

from app.utils.fonts import text_width

POSITIONS = ("top_left", "top_center", "top_right", "bottom_left", "bottom_center", "bottom_right")
SIZE_SCALE = {"small": 0.75, "medium": 1.0, "large": 1.3}
_ALIGN = {"left": 0.0, "center": 0.5, "right": 1.0}
_GAP = 14  # clear space kept around the logo

Box = Tuple[float, float, float, float]


def _text_box(layer: Dict) -> Optional[Box]:
    content = str(layer.get("content") or "")
    if not content.strip():
        return None
    size = float(layer.get("font_size") or 0)
    lines = content.split("\n")
    w = max(text_width(l, layer.get("font_family"), size, int(layer.get("font_weight") or 400),
                       float(layer.get("tracking") or 0.0)) for l in lines)
    h = size * 1.25 * len(lines)
    anchor = str(layer.get("text_align") or "la")
    x, y = float(layer.get("x") or 0), float(layer.get("y") or 0)
    x -= {"l": 0, "m": w / 2, "r": w}.get(anchor[0], 0)
    if len(anchor) > 1 and anchor[1] == "m":
        y -= h / 2
    return x, y, x + w, y + h


def _obstacles(layers: List[Dict], width: int, height: int) -> List[Box]:
    """What a logo must not cover: real text, elements and photos. Backgrounds, washes, ornament
    words and the big decorative shapes a poster is built on are not obstacles."""
    out: List[Box] = []
    for layer in layers:
        kind = layer.get("type")
        if layer.get("role") == "brand_logo" or layer.get("decorative"):
            continue
        if kind == "text":
            box = _text_box(layer)
            if box:
                out.append(box)
        elif kind in ("shape", "composited_product", "brand_asset"):
            if kind == "shape" and layer.get("shape") in ("vgradient", "hgradient"):
                continue
            w, h = float(layer.get("width") or 0), float(layer.get("height") or 0)
            if w <= 0 or h <= 0 or (kind == "shape" and w * h > 0.3 * width * height):
                continue
            x, y = float(layer.get("x") or 0), float(layer.get("y") or 0)
            out.append((x, y, x + w, y + h))
    return out


def _hits(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def place_brand_logo(document: Dict, brand_context: Optional[Dict]) -> Dict:
    layers = document.get("layers") or []
    logo = next((l for l in layers if l.get("role") == "brand_logo"), None)
    if not logo:
        return document
    width, height = (document.get("canvas") or {}).get("width") or 1080, (document.get("canvas") or {}).get("height") or 1080
    bc = brand_context or {}
    wanted = str(bc.get("logo_position") or "").strip().lower()
    scale = SIZE_SCALE.get(str(bc.get("logo_size") or "").strip().lower(), 1.0)
    bw, bh = float(logo["width"]) * scale, float(logo["height"]) * scale
    # The format's own margins (its distance from the nearest edges), mirrored to every side. A logo the
    # format centres has no side margin of its own: the posters' standard edge margin is used then.
    lx, ly, lw, lh = float(logo["x"]), float(logo["y"]), float(logo["width"]), float(logo["height"])
    mx = min(lx, width - lx - lw)
    my = min(ly, height - ly - lh)
    edge = width * 57 / 1080
    if mx > width * 0.2:
        mx = edge
    if my > height * 0.2:
        my = edge
    obstacles = _obstacles(layers, width, height)

    def spot(position: str) -> Tuple[float, float, str]:
        vertical, horizontal = position.split("_")
        x = {"left": mx, "center": (width - bw) / 2, "right": width - mx - bw}[horizontal]
        y = my if vertical == "top" else height - my - bh
        return x, y, horizontal

    order = ([wanted] if wanted in POSITIONS else []) + [p for p in POSITIONS if p != wanted]
    for position in order:
        x, y, horizontal = spot(position)
        if any(_hits((x - _GAP, y - _GAP, x + bw + _GAP, y + bh + _GAP), o) for o in obstacles):
            continue
        if wanted in POSITIONS and position != wanted:
            print(f"[VSG01] logo: {wanted} is taken by the layout, placed at {position}", flush=True)
        logo.update({"x": int(x), "y": int(y), "width": int(bw), "height": int(bh),
                     "align_x": _ALIGN[horizontal], "trim": True})
        return document
    # Nowhere clear at all: the format's own spot, as it designed it.
    logo["trim"] = True
    return document

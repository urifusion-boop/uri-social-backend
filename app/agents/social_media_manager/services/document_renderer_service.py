"""
Document Renderer Service - Server-Side PNG Generation

Renders layered documents to final PNG/JPG images using Pillow.
This ensures pixel-perfect output matching the Canvas Editor preview.

WYSIWYG Guarantee: Client preview and server render must be identical.
"""

from typing import Dict, Any, Optional
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps
import io
import os
import httpx
from datetime import datetime

from app.utils.fonts import draw_spans, draw_text, load_font, text_width as fonts_text_width


class DocumentRendererService:
    """Service for rendering layered documents to images"""

    @staticmethod
    async def render_to_png(
        document: Dict[str, Any],
        output_format: str = "png",
        quality: int = 95
    ) -> bytes:
        """
        Render layered document to PNG/JPG bytes

        Args:
            document: Layered document JSON
            output_format: "png" or "jpeg"
            quality: JPEG quality (1-100), ignored for PNG

        Returns:
            Image bytes ready for upload
        """
        canvas_config = document.get("canvas", {})
        width = canvas_config.get("width", 1080)
        height = canvas_config.get("height", 1080)
        bg_color = canvas_config.get("background_color", "#000000")

        # Create base canvas
        canvas = Image.new("RGBA", (width, height), DocumentRendererService._hex_to_rgba(bg_color))

        # Get layers sorted by z_index (bottom to top)
        layers = sorted(
            document.get("layers", []),
            key=lambda l: l.get("z_index", 0)
        )

        # Render each layer
        for layer in layers:
            if not layer.get("visible", True):
                continue  # Skip hidden layers

            layer_type = layer.get("type")

            try:
                if layer_type == "ai_generated_background":
                    await DocumentRendererService._render_background(canvas, layer, width, height)

                elif layer_type == "composited_product":
                    await DocumentRendererService._render_product(canvas, layer)

                elif layer_type == "text":
                    await DocumentRendererService._render_text(canvas, layer)

                elif layer_type == "brand_asset":
                    await DocumentRendererService._render_asset(canvas, layer)

                elif layer_type == "shape":
                    await DocumentRendererService._render_shape(canvas, layer)

                elif layer_type == "badge":
                    await DocumentRendererService._render_badge(canvas, layer)

                elif layer_type == "icon":
                    DocumentRendererService._render_icon(canvas, layer)

            except Exception as e:
                print(f"⚠️ Error rendering layer {layer.get('id')}: {e}")
                # Continue rendering other layers even if one fails

        # Convert to output format
        output = io.BytesIO()
        if output_format.lower() == "jpeg" or output_format.lower() == "jpg":
            # Convert RGBA to RGB for JPEG
            rgb_canvas = Image.new("RGB", canvas.size, (255, 255, 255))
            rgb_canvas.paste(canvas, mask=canvas.split()[3])  # Use alpha as mask
            rgb_canvas.save(output, format="JPEG", quality=quality, optimize=True)
        else:
            canvas.save(output, format="PNG", optimize=True)

        return output.getvalue()

    @staticmethod
    async def _render_background(
        canvas: Image.Image,
        layer: Dict[str, Any],
        canvas_width: int,
        canvas_height: int
    ):
        """Render background image layer.

        x/y/width/height default to the full canvas — every caller before
        VSG-01's Problem/Solution format either passed those explicitly as
        the full canvas size or omitted them entirely, so this preserves
        their behaviour exactly. Previously this always resized to
        (canvas_width, canvas_height) and pasted at (0, 0) regardless of
        what a layer specified, silently ignoring x/y/width/height —
        harmless when a document only ever has one full-canvas background,
        but a real bug for a format needing two ai_generated_background
        layers in different zones of the same canvas (confirmed live: the
        second layer's full-canvas paste completely overwrote the first
        zone's scrim and text). Brought in line with _render_product,
        which already respects per-layer x/y/width/height correctly."""
        image_url = layer.get("url")
        if not image_url:
            return

        # Download image
        bg_image = await DocumentRendererService._fetch_image(image_url)
        if not bg_image:
            return

        x = layer.get("x", 0)
        y = layer.get("y", 0)
        width = layer.get("width", canvas_width)
        height = layer.get("height", canvas_height)

        # Resize to the layer's own dimensions. fit="cover" crops to fill instead of
        # stretching, for a photo whose shape doesn't match its box; without it the
        # image is stretched exactly as before.
        if layer.get("fit") == "cover":
            bg_image = ImageOps.fit(bg_image, (width, height), Image.Resampling.LANCZOS,
                                    centering=tuple(layer.get("focus", (0.5, 0.5))))
        else:
            bg_image = bg_image.resize((width, height), Image.Resampling.LANCZOS)

        # recolor: {from, to, box: [x0, y0, x1, y1], tolerance} — every pixel inside the box
        # within `tolerance` of `from` becomes `to`, keeping its own light and shade. Repaints a
        # billboard's blank face in exactly the colour its words were checked against, right
        # up to (and behind) the railings and lamp arms, which keep their own colours.
        recolor = layer.get("recolor")
        if recolor:
            bg_image = DocumentRendererService._recolor(bg_image, recolor)

        # Paste at the layer's own position
        canvas.paste(bg_image, (x, y))

    @staticmethod
    def _recolor(image: Image.Image, spec: Dict[str, Any]) -> Image.Image:
        import numpy as np
        src = np.array(DocumentRendererService._hex_to_rgba(spec["from"])[:3], dtype=np.float32)
        dst = np.array(DocumentRendererService._hex_to_rgba(spec["to"])[:3], dtype=np.float32)
        x0, y0, x1, y1 = (int(v) for v in spec.get("box", (0, 0, image.width, image.height)))
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(image.width, x1), min(image.height, y1)
        if x1 <= x0 or y1 <= y0:
            return image
        arr = np.array(image.convert("RGBA"), dtype=np.float32)
        region = arr[y0:y1, x0:x1, :3]
        dist = np.sqrt(((region - src) ** 2).sum(axis=2))
        tol = float(spec.get("tolerance", 60))
        # Fully replaced near the colour, blending out toward the edge of the tolerance, so the
        # face's edge stays soft. Shading is kept: each pixel keeps its offset from `from`.
        weight = np.clip((tol - dist) / (tol * 0.35), 0.0, 1.0)[..., None]
        shifted = np.clip(region + (dst - src), 0, 255)
        arr[y0:y1, x0:x1, :3] = region * (1 - weight) + shifted * weight
        return Image.fromarray(arr.astype(np.uint8), "RGBA")

    @staticmethod
    async def _render_product(canvas: Image.Image, layer: Dict[str, Any]):
        """Render composited product layer with shadow"""
        product_url = layer.get("url")
        if not product_url:
            return

        # Download product image
        product_image = await DocumentRendererService._fetch_image(product_url)
        if not product_image:
            return

        # Get dimensions and position
        x = layer.get("x", 0)
        y = layer.get("y", 0)
        width = layer.get("width")
        height = layer.get("height")

        # Resize if dimensions specified. fit="contain" keeps the image's proportions
        # inside the box, standing on its bottom edge, centred (a product or a cut-out
        # person); without it the image is resized to the box exactly as before.
        if layer.get("trim"):
            # Crop away a cut-out's transparent margins so the person, not the empty
            # canvas around them, is what gets fitted into the box.
            product_image = product_image.convert("RGBA")
            bbox = product_image.split()[3].getbbox()
            if bbox:
                product_image = product_image.crop(bbox)
        if width and height and layer.get("fit") == "contain":
            product_image = product_image.convert("RGBA")
            product_image.thumbnail((width, height), Image.Resampling.LANCZOS)
            x += int((width - product_image.width) * layer.get("align_x", 0.5))
            y += int((height - product_image.height) * layer.get("align_y", 1.0))
        elif width and height:
            product_image = product_image.resize((width, height), Image.Resampling.LANCZOS)

        # Apply rotation if specified
        rotation = layer.get("rotation", 0)
        if rotation:
            product_image = product_image.rotate(-rotation, expand=True, resample=Image.Resampling.BICUBIC)

        # Apply shadow if specified
        shadow_config = layer.get("shadow")
        if shadow_config:
            product_image = DocumentRendererService._apply_shadow(product_image, shadow_config)

        # Paste onto canvas
        canvas.paste(product_image, (x, y), product_image if product_image.mode == 'RGBA' else None)

    ICONS_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "assets", "icons"))

    @staticmethod
    def _render_icon(canvas: Image.Image, layer: Dict[str, Any]):
        """A bundled glyph (app/assets/icons/<name>.png — WhatsApp, Instagram, …) drawn in
        one colour at (x, y), `size` pixels square."""
        name = os.path.basename(str(layer.get("name", "")))
        path = os.path.join(DocumentRendererService.ICONS_DIR, f"{name}.png")
        if not name or not os.path.exists(path):
            return
        size = int(layer.get("size", 32))
        mask = Image.open(path).convert("RGBA").split()[3].resize((size, size), Image.Resampling.LANCZOS)
        fill = Image.new("RGBA", (size, size), DocumentRendererService._hex_to_rgba(layer.get("color", "#000000")))
        canvas.paste(fill, (int(layer.get("x", 0)), int(layer.get("y", 0))), mask)

    @staticmethod
    async def _render_text(canvas: Image.Image, layer: Dict[str, Any]):
        """Render text layer"""
        spans = layer.get("spans")
        content = layer.get("content", "")
        if not content and not spans:
            return

        draw = ImageDraw.Draw(canvas)

        # Text properties
        font_family = layer.get("font_family", "Arial")
        font_size = layer.get("font_size", 48)
        font_weight = layer.get("font_weight", 400)
        color = layer.get("color", "#FFFFFF")
        x = layer.get("x", 100)
        y = layer.get("y", 100)


        # Draw text. `text_align` maps straight onto Pillow's own anchor
        # strings (e.g. "ra" = right-ascender) — added for right-aligned
        # numbers (Receipt's prices need to line up at a fixed right edge,
        # which means anchoring at x rather than measuring text width
        # ourselves in every format builder that needs it). Left/top-anchored
        # ("la", Pillow's own default) is unchanged for every existing caller
        # that doesn't pass this.
        # Font picked by family/weight from the bundled set (app/utils/fonts.py); a family
        # that isn't bundled keeps DejaVu exactly as before. Characters the font lacks
        # (often ₦) are drawn in Inter instead of as empty boxes.
        rgba_color = DocumentRendererService._hex_to_rgba(color)
        anchor = layer.get("text_align")
        if spans:
            # One line in differently coloured pieces, e.g. "Get " + "50%" (accent) + " off".
            pieces = []
            for sp in spans:
                piece = {k: v for k, v in sp.items() if k in ("text", "font_family", "font_weight", "font_size")}
                if sp.get("color"):
                    piece["color"] = DocumentRendererService._hex_to_rgba(sp["color"])
                if sp.get("highlight"):
                    piece["highlight"] = DocumentRendererService._hex_to_rgba(sp["highlight"])
                pieces.append(piece)
            draw_spans(draw, (x, y), pieces, font_family, font_size, font_weight, fill=rgba_color, anchor=anchor,
                       tracking=float(layer.get("tracking", 0) or 0))
            return
        # `tracking`: letter spacing as a fraction of the size (-0.03 = 3% tighter), as the
        # reference posters set their headlines; 0 draws exactly as before.
        draw_text(draw, (x, y), content, font_family, font_size, font_weight, fill=rgba_color, anchor=anchor,
                  tracking=float(layer.get("tracking", 0) or 0))

    @staticmethod
    async def _render_badge(canvas: Image.Image, layer: Dict[str, Any]):
        """A pill or tag with text, optionally tilted — the "50% Off All Services" ticket.

        {cx, cy (centre on the canvas), text, font_family, font_size, font_weight, text_color,
        fill_color, padding_x, padding_y, corner_radius, rotation (degrees, positive = anticlockwise),
        border_color, border_width}. Drawn on its own transparent tile, rotated, then laid on the
        canvas, so the text tilts with the tag."""
        text = layer.get("text", "")
        if not text:
            return
        if layer.get("style") == "ticket":
            tile = DocumentRendererService._ticket_tile(layer)
        else:
            family = layer.get("font_family")
            size = int(layer.get("font_size", 36))
            weight = layer.get("font_weight", 700)
            pad_x, pad_y = int(layer.get("padding_x", 28)), int(layer.get("padding_y", 16))
            font = load_font(family, size, weight)
            ascent, descent = font.getmetrics()
            tracking = float(layer.get("tracking", 0) or 0)
            # One line, or two split on "\n" (the "Anniversary / Discounted Offer" tag).
            lines = [l for l in text.split("\n") if l.strip()][:2] or [text]
            pitch = int(size * 1.02)
            w = int(max(fonts_text_width(l, family, size, weight, tracking) for l in lines)) + 2 * pad_x
            h = ascent + descent + pitch * (len(lines) - 1) + 2 * pad_y
            tile = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            tile_draw = ImageDraw.Draw(tile)
            fill = DocumentRendererService._hex_to_rgba(layer["fill_color"]) if layer.get("fill_color") else None
            outline = DocumentRendererService._hex_to_rgba(layer["border_color"]) if layer.get("border_color") else None
            tile_draw.rounded_rectangle([0, 0, w - 1, h - 1], radius=int(layer.get("corner_radius", h // 2)),
                                        fill=fill, outline=outline, width=int(layer.get("border_width", 0) or 0))
            ink = DocumentRendererService._hex_to_rgba(layer.get("text_color", "#FFFFFF"))
            if len(lines) == 1:
                draw_text(tile_draw, (w / 2, h / 2), text, family, size, weight, fill=ink, anchor="mm",
                          tracking=tracking)
            else:
                for i, line in enumerate(lines):
                    draw_text(tile_draw, (pad_x, pad_y + i * pitch), line, family, size, weight, fill=ink,
                              anchor="la", tracking=tracking)
        rotation = float(layer.get("rotation", 0) or 0)
        if rotation:
            tile = tile.rotate(rotation, expand=True, resample=Image.Resampling.BICUBIC)
        cx, cy = layer.get("cx", 0), layer.get("cy", 0)
        DocumentRendererService._composite_clipped(canvas, tile, int(cx - tile.width / 2), int(cy - tile.height / 2))

    @staticmethod
    def _ticket_tile(layer: Dict[str, Any]) -> Image.Image:
        """style="ticket": a paper ticket — scalloped ends, a stub carrying five small stars
        (star_color) behind a dashed tear line, then the text (one or two lines, split on
        "\\n"). Same keys as a badge otherwise."""
        k = DocumentRendererService._SMOOTH
        family = layer.get("font_family")
        size = int(layer.get("font_size", 36))
        weight = layer.get("font_weight", 700)
        tracking = float(layer.get("tracking", 0) or 0)
        lines = [l for l in str(layer.get("text", "")).split("\n") if l.strip()][:2]
        pad_x, pad_y = int(layer.get("padding_x", 26)), int(layer.get("padding_y", 16))
        font = load_font(family, size, weight)
        ascent, descent = font.getmetrics()
        pitch = int(size * 1.02)
        text_w = int(max(fonts_text_width(l, family, size, weight, tracking) for l in lines))
        text_h = pitch * (len(lines) - 1) + ascent + descent
        star_r = max(5, int(size * 0.16))
        stub_w = 5 * star_r * 2 + 2 * pad_x // 2
        notch = max(6, int(size * 0.2))
        w = notch + stub_w + pad_x + text_w + pad_x + notch
        h = text_h + 2 * pad_y
        fill = DocumentRendererService._hex_to_rgba(layer.get("fill_color", "#0B6E4F"))
        ink = DocumentRendererService._hex_to_rgba(layer.get("text_color", "#FFFFFF"))
        star = DocumentRendererService._hex_to_rgba(layer.get("star_color", "#FFD23F"))
        big = Image.new("RGBA", (w * k, h * k), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)
        # Scalloped ends: little half-circles bitten out of the left and right edges.
        d.rectangle([notch * k, 0, (w - notch) * k, h * k], fill=fill)
        bites = max(3, h // (notch * 2))
        step = h / bites
        for i in range(bites):
            cy = (i + 0.5) * step * k
            r = step * k / 2
            d.ellipse([notch * k - r, cy - r, notch * k + r, cy + r], fill=fill)
            d.ellipse([(w - notch) * k - r, cy - r, (w - notch) * k + r, cy + r], fill=fill)
        # Stars on the stub, then the dashed tear line.
        sx = (notch + pad_x // 2 + star_r) * k
        for i in range(5):
            DocumentRendererService._star(d, sx + i * star_r * 2 * k, h * k * 0.62, star_r * k, star)
        tear = (notch + stub_w) * k
        y = int(h * k * 0.12)
        while y < h * k * 0.88:
            d.line([(tear, y), (tear, min(y + 5 * k, h * k * 0.88))], fill=ink, width=2 * k)
            y += 10 * k
        tile = big.resize((w, h), Image.Resampling.LANCZOS)
        td = ImageDraw.Draw(tile)
        tx = notch + stub_w + pad_x
        ty = pad_y
        for i, line in enumerate(lines):
            draw_text(td, (tx, ty + i * pitch), line, family, size, weight, fill=ink, anchor="la", tracking=tracking)
        return tile

    @staticmethod
    def _star(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill):
        import math
        pts = []
        for i in range(10):
            a = -math.pi / 2 + i * math.pi / 5
            rr = r if i % 2 == 0 else r * 0.45
            pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
        d.polygon(pts, fill=fill)

    @staticmethod
    async def _render_asset(canvas: Image.Image, layer: Dict[str, Any]):
        """Render brand asset (logo, badge, etc.)"""
        asset_url = layer.get("url")
        if not asset_url:
            return

        # Download asset
        asset_image = await DocumentRendererService._fetch_image(asset_url)
        if not asset_image:
            return

        # Get position and size
        x = layer.get("x", 0)
        y = layer.get("y", 0)
        width = layer.get("width")
        height = layer.get("height")

        # Fit within (width, height) preserving the logo's own aspect ratio,
        # then center it in the box — every caller (tokens.logo_badge_layers,
        # review_card.py, humour_cartoon.py, receipt.py) sizes this box to
        # its OWN fixed aspect ratio (e.g. 140x56, 2.5:1), which is almost
        # never a real logo's actual shape. A plain resize((width, height))
        # here stretched every logo to that box's ratio regardless — live-
        # confirmed a real wordmark logo rendered visibly squeezed/distorted.
        # Fixed once at this single render choke point rather than in each
        # of the 4 callers, since none of them can know a specific
        # business's real logo shape in advance anyway.
        if width and height:
            orig_w, orig_h = asset_image.size
            if orig_w and orig_h:
                scale = min(width / orig_w, height / orig_h)
                fit_w, fit_h = max(1, round(orig_w * scale)), max(1, round(orig_h * scale))
                asset_image = asset_image.resize((fit_w, fit_h), Image.Resampling.LANCZOS)
                x += (width - fit_w) // 2
                y += (height - fit_h) // 2

        # tint: the logo drawn in one colour (white on a brand-colour pill). Only for a logo
        # with a real transparent background — tinting an opaque logo would paint a solid
        # block, so that one sits on a small white plate instead.
        if layer.get("tint"):
            asset_image = asset_image.convert("RGBA")
            alpha = asset_image.split()[3]
            clear = sum(alpha.histogram()[:16]) / max(1, asset_image.width * asset_image.height)
            if clear > 0.08:
                solid = Image.new("RGBA", asset_image.size, DocumentRendererService._hex_to_rgba(layer["tint"]))
                solid.putalpha(alpha)
                asset_image = solid
            else:
                plate_pad = max(6, asset_image.height // 8)
                plate = Image.new("RGBA", (asset_image.width + 2 * plate_pad, asset_image.height + 2 * plate_pad),
                                  (0, 0, 0, 0))
                ImageDraw.Draw(plate).rounded_rectangle([0, 0, plate.width - 1, plate.height - 1],
                                                        radius=plate_pad * 2, fill=(255, 255, 255, 255))
                plate.alpha_composite(asset_image, (plate_pad, plate_pad))
                scale = min(asset_image.width / plate.width, asset_image.height / plate.height)
                x += (asset_image.width - round(plate.width * scale)) // 2
                y += (asset_image.height - round(plate.height * scale)) // 2
                asset_image = plate.resize((max(1, round(plate.width * scale)), max(1, round(plate.height * scale))),
                                           Image.Resampling.LANCZOS)

        # Apply opacity if specified
        opacity = layer.get("opacity", 1.0)
        if opacity < 1.0:
            if asset_image.mode != 'RGBA':
                asset_image = asset_image.convert('RGBA')
            alpha = asset_image.split()[3]
            alpha = alpha.point(lambda p: int(p * opacity))
            asset_image.putalpha(alpha)

        # Paste onto canvas
        canvas.paste(asset_image, (x, y), asset_image if asset_image.mode == 'RGBA' else None)

    @staticmethod
    async def _render_shape(canvas: Image.Image, layer: Dict[str, Any]):
        """
        Render a plain/rounded rectangle or a (optionally dashed) line — the
        one primitive this renderer was missing, needed for VSG-01's L4-only
        formats: Receipt's leader lines and rule-above-total, Us vs Them's
        field blocks and row rules, Borrowed Interface's message bubbles.
        No image download, no external dependency — pure Pillow drawing, same
        as _render_text below.

        rect / rounded_rect: {x, y, width, height, corner_radius (rounded_rect
        only), fill_color, border_color, border_width}. fill_color and/or
        border_color may be omitted (an outline-only or fill-only shape).

        line: {x1, y1, x2, y2, color, stroke_width, dashed, dash_length,
        gap_length}. Deliberately a distinct coordinate scheme from rect
        (endpoints, not x/y/width/height) — a leader line runs between two
        specific points, not a box.
        """
        shape = layer.get("shape", "rect")
        draw = ImageDraw.Draw(canvas)

        if shape in ("rect", "rounded_rect"):
            x, y = layer.get("x", 0), layer.get("y", 0)
            width, height = layer.get("width", 0), layer.get("height", 0)
            if width <= 0 or height <= 0:
                return
            box = [x, y, x + width, y + height]
            fill = DocumentRendererService._hex_to_rgba(layer["fill_color"]) if layer.get("fill_color") else None
            outline = DocumentRendererService._hex_to_rgba(layer["border_color"]) if layer.get("border_color") else None
            border_width = layer.get("border_width", 2)
            if fill is None and outline is None:
                return  # nothing would actually render — matches _render_text's early-return-on-empty-content
            if shape == "rounded_rect":
                radius = layer.get("corner_radius", 8)
                draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=border_width)
            else:
                draw.rectangle(box, fill=fill, outline=outline, width=border_width)

        elif shape == "vgradient":
            # A colour fading in over the box from top to bottom (or the reverse), laid
            # over what's beneath it — the dark wash that lets white text sit on a photo.
            # {x, y, width, height, color, from_alpha (0-255, top), to_alpha (bottom)}
            x, y = int(layer.get("x", 0)), int(layer.get("y", 0))
            width, height = int(layer.get("width", 0)), int(layer.get("height", 0))
            if width <= 0 or height <= 0:
                return
            r, g, b, _ = DocumentRendererService._hex_to_rgba(layer.get("color", "#000000"))
            a0, a1 = int(layer.get("from_alpha", 0)), int(layer.get("to_alpha", 220))
            column = Image.new("L", (1, height))
            column.putdata([int(a0 + (a1 - a0) * (i / max(1, height - 1))) for i in range(height)])
            wash = Image.new("RGBA", (width, height), (r, g, b, 255))
            wash.putalpha(column.resize((width, height)))
            canvas.alpha_composite(wash, (x, y))

        elif shape == "triangle":
            # {x, y, width, height, fill_color} — pointing right, like a ▸ marker.
            x, y = layer.get("x", 0), layer.get("y", 0)
            width, height = layer.get("width", 0), layer.get("height", 0)
            if width <= 0 or height <= 0 or not layer.get("fill_color"):
                return
            draw.polygon([(x, y), (x + width, y + height / 2), (x, y + height)],
                         fill=DocumentRendererService._hex_to_rgba(layer["fill_color"]))

        elif shape == "hgradient":
            # {x, y, width, height, color, from_alpha (left), to_alpha (right)} — keeps the text side of
            # a full-bleed image calm and clean.
            x, y = int(layer.get("x", 0)), int(layer.get("y", 0))
            width, height = int(layer.get("width", 0)), int(layer.get("height", 0))
            if width <= 0 or height <= 0:
                return
            r, g, b, _ = DocumentRendererService._hex_to_rgba(layer.get("color", "#FFFFFF"))
            a0, a1 = int(layer.get("from_alpha", 255)), int(layer.get("to_alpha", 0))
            row = Image.new("L", (width, 1))
            row.putdata([int(a0 + (a1 - a0) * (i / max(1, width - 1))) for i in range(width)])
            wash = Image.new("RGBA", (width, height), (r, g, b, 255))
            wash.putalpha(row.resize((width, height)))
            canvas.alpha_composite(wash, (x, y))

        elif shape == "ellipse":
            # {x, y, width, height, fill_color, border_color, border_width} — e.g. the
            # hand-drawn-style ring around "solution".
            x, y = layer.get("x", 0), layer.get("y", 0)
            width, height = layer.get("width", 0), layer.get("height", 0)
            if width <= 0 or height <= 0:
                return
            fill = DocumentRendererService._hex_to_rgba(layer["fill_color"]) if layer.get("fill_color") else None
            outline = DocumentRendererService._hex_to_rgba(layer["border_color"]) if layer.get("border_color") else None
            draw.ellipse([x, y, x + width, y + height], fill=fill, outline=outline,
                         width=int(layer.get("border_width", 4)))

        elif shape == "line":
            x1, y1 = layer.get("x1", 0), layer.get("y1", 0)
            x2, y2 = layer.get("x2", 0), layer.get("y2", 0)
            color = DocumentRendererService._hex_to_rgba(layer.get("color", "#000000"))
            stroke_width = layer.get("stroke_width", 2)
            if not layer.get("dashed"):
                draw.line([(x1, y1), (x2, y2)], fill=color, width=stroke_width)
                return
            # Dashed: walk the segment in dash+gap steps. Only horizontal and
            # vertical lines are supported dashed (every use case here —
            # Receipt's leaders, rule dividers — is axis-aligned); a diagonal
            # dashed line falls back to solid rather than silently drawing
            # nothing.
            dash_length = layer.get("dash_length", 6)
            gap_length = layer.get("gap_length", 4)
            step = dash_length + gap_length
            if y1 == y2:
                total = abs(x2 - x1)
                direction = 1 if x2 >= x1 else -1
                pos = 0
                while pos < total:
                    seg_end = min(pos + dash_length, total)
                    draw.line(
                        [(x1 + direction * pos, y1), (x1 + direction * seg_end, y1)],
                        fill=color, width=stroke_width,
                    )
                    pos += step
            elif x1 == x2:
                total = abs(y2 - y1)
                direction = 1 if y2 >= y1 else -1
                pos = 0
                while pos < total:
                    seg_end = min(pos + dash_length, total)
                    draw.line(
                        [(x1, y1 + direction * pos), (x1, y1 + direction * seg_end)],
                        fill=color, width=stroke_width,
                    )
                    pos += step
            else:
                draw.line([(x1, y1), (x2, y2)], fill=color, width=stroke_width)

        elif shape in ("polygon", "path"):
            DocumentRendererService._render_smooth_shape(canvas, layer, shape)

    # Supersampling factor for polygons and paths: Pillow draws them without anti-aliasing,
    # so they're drawn this many times larger and scaled down — smooth edges, like the rest.
    _SMOOTH = 3

    @staticmethod
    def _catmull_rom(points: list, steps: int = 16) -> list:
        """A smooth curve through every point (the swoosh, the hand-drawn ring)."""
        if len(points) < 3:
            return points
        pts = [points[0]] + points + [points[-1]]
        out = []
        for i in range(1, len(pts) - 2):
            p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
            for s in range(steps):
                t = s / steps
                t2, t3 = t * t, t * t * t
                out.append(tuple(
                    0.5 * ((2 * p1[k]) + (-p0[k] + p2[k]) * t + (2 * p0[k] - 5 * p1[k] + 4 * p2[k] - p3[k]) * t2
                           + (-p0[k] + 3 * p1[k] - 3 * p2[k] + p3[k]) * t3)
                    for k in (0, 1)))
        out.append(tuple(points[-1]))
        return out

    @staticmethod
    def _render_smooth_shape(canvas: Image.Image, layer: Dict[str, Any], shape: str):
        """polygon: {points: [[x, y], …], fill_color, border_color, border_width} — an arrow-ended
        pill, a ticket. path: {points, color, stroke_width, smooth (curve through the points),
        closed} — a hand-drawn ring, a swoosh."""
        points = [(float(p[0]), float(p[1])) for p in (layer.get("points") or [])]
        if len(points) < 2:
            return
        if shape == "path" and layer.get("smooth"):
            points = DocumentRendererService._catmull_rom(points + ([points[0]] if layer.get("closed") else []))
        k = DocumentRendererService._SMOOTH
        pad = int(layer.get("stroke_width", layer.get("border_width", 0)) or 0) + 2
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        x0, y0 = int(min(xs)) - pad, int(min(ys)) - pad
        w, h = int(max(xs)) - x0 + pad + 1, int(max(ys)) - y0 + pad + 1
        if w <= 0 or h <= 0:
            return
        tile = Image.new("RGBA", (w * k, h * k), (0, 0, 0, 0))
        d = ImageDraw.Draw(tile)
        scaled = [((x - x0) * k, (y - y0) * k) for x, y in points]
        if shape == "polygon":
            fill = DocumentRendererService._hex_to_rgba(layer["fill_color"]) if layer.get("fill_color") else None
            outline = DocumentRendererService._hex_to_rgba(layer["border_color"]) if layer.get("border_color") else None
            if fill is None and outline is None:
                return
            d.polygon(scaled, fill=fill, outline=outline, width=int(layer.get("border_width", 0) or 0) * k)
        else:
            color = DocumentRendererService._hex_to_rgba(layer.get("color", "#000000"))
            sw = int(layer.get("stroke_width", 4)) * k
            d.line(scaled, fill=color, width=sw, joint="curve")
            r = sw / 2
            for cx, cy in (scaled[0], scaled[-1]):  # round ends
                d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
        tile = tile.resize((w, h), Image.Resampling.LANCZOS)
        DocumentRendererService._composite_clipped(canvas, tile, x0, y0)

    @staticmethod
    def _composite_clipped(canvas: Image.Image, tile: Image.Image, x: int, y: int):
        """alpha_composite for a tile that may run off any edge of the canvas (a swoosh, a
        shape cut by the frame) — only the part on the canvas is drawn."""
        cx, cy = max(0, -x), max(0, -y)
        if cx >= tile.width or cy >= tile.height:
            return
        canvas.alpha_composite(tile.crop((cx, cy, tile.width, tile.height)), (max(0, x), max(0, y)))

    @staticmethod
    async def _fetch_image(url: str) -> Optional[Image.Image]:
        """Download image from URL.

        Retries twice (1s, then 2s backoff) before giving up — found live
        building VSG-01's News Headline format: a Cloudinary URL returned
        by layer2_generation.generate_scene() immediately after upload
        failed to fetch on the very next render call, then succeeded on a
        manual retry seconds later (curl confirmed the same URL was a
        real, complete image — a CDN propagation race, not a broken
        upload or a bad URL). Every ai_generated_background/
        composited_product/brand_asset layer goes through this method, so
        a single transient failure previously meant a silently blank
        background rather than the generated scene the whole format is
        built around."""
        last_error: Optional[Exception] = None
        for attempt in range(3):
            if attempt > 0:
                import asyncio
                await asyncio.sleep(attempt)  # 1s before 2nd try, 2s before 3rd
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.get(url)
                    if response.status_code == 200:
                        return Image.open(io.BytesIO(response.content)).convert("RGBA")
                    last_error = f"HTTP {response.status_code}"
            except Exception as e:
                last_error = e
        print(f"⚠️ Error fetching image {url} after 3 attempts: {last_error}")
        return None

    @staticmethod
    def _load_font(font_family: str, font_size: int, font_weight: int) -> ImageFont.FreeTypeFont:
        """The bundled font for this family and weight (app/utils/fonts.py), or DejaVu Sans when
        the family isn't bundled — the long-standing default."""
        return load_font(font_family, font_size, font_weight)

    @staticmethod
    def _hex_to_rgba(hex_color: str, alpha: int = 255) -> tuple:
        """Convert hex color to RGBA tuple"""
        hex_color = hex_color.lstrip('#')

        # Handle hex with alpha (8 characters)
        if len(hex_color) == 8:
            r = int(hex_color[0:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)
            a = int(hex_color[6:8], 16)
            return (r, g, b, a)

        # Handle standard hex (6 characters)
        if len(hex_color) == 6:
            r = int(hex_color[0:2], 16)
            g = int(hex_color[2:4], 16)
            b = int(hex_color[4:6], 16)
            return (r, g, b, alpha)

        # Fallback
        return (255, 255, 255, alpha)

    @staticmethod
    def _apply_shadow(
        image: Image.Image,
        shadow_config: Dict[str, Any]
    ) -> Image.Image:
        """
        Apply drop shadow to image

        Args:
            image: Source image (RGBA)
            shadow_config: {color, opacity, blur, offset_x, offset_y}

        Returns:
            Image with shadow applied
        """
        if image.mode != 'RGBA':
            image = image.convert('RGBA')

        # Create shadow layer
        shadow = Image.new('RGBA', image.size, (0, 0, 0, 0))
        shadow_draw = ImageDraw.Draw(shadow)

        # Get shadow properties
        color = shadow_config.get("color", "#000000")
        opacity = shadow_config.get("opacity", 0.4)
        blur_radius = shadow_config.get("blur", 24)
        offset_x = shadow_config.get("offset_x", 0)
        offset_y = shadow_config.get("offset_y", 8)

        # Convert color and opacity
        r, g, b = DocumentRendererService._hex_to_rgba(color)[:3]
        alpha = int(255 * opacity)

        # Create shadow (simplified - just offset + blur)
        shadow_layer = Image.new('RGBA', image.size, (0, 0, 0, 0))
        shadow_layer.paste(image, (offset_x, offset_y))

        # Apply blur
        if blur_radius > 0:
            shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(blur_radius))

        # Colorize shadow
        shadow_array = shadow_layer.split()
        shadow_colored = Image.new('RGBA', image.size, (r, g, b, 0))
        shadow_colored.putalpha(shadow_array[3].point(lambda p: int(p * opacity)))

        # Composite: shadow + original image
        result = Image.new('RGBA', image.size, (0, 0, 0, 0))
        result.paste(shadow_colored, (0, 0), shadow_colored)
        result.paste(image, (0, 0), image)

        return result

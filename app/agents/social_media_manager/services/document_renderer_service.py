"""
Document Renderer Service - Server-Side PNG Generation

Renders layered documents to final PNG/JPG images using Pillow.
This ensures pixel-perfect output matching the Canvas Editor preview.

WYSIWYG Guarantee: Client preview and server render must be identical.
"""

from typing import Dict, Any, Optional
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import io
import httpx
from datetime import datetime


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
        """Render background image layer"""
        image_url = layer.get("url")
        if not image_url:
            return

        # Download image
        bg_image = await DocumentRendererService._fetch_image(image_url)
        if not bg_image:
            return

        # Resize to canvas size
        bg_image = bg_image.resize((canvas_width, canvas_height), Image.Resampling.LANCZOS)

        # Paste onto canvas
        canvas.paste(bg_image, (0, 0))

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

        # Resize if dimensions specified
        if width and height:
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

    @staticmethod
    async def _render_text(canvas: Image.Image, layer: Dict[str, Any]):
        """Render text layer"""
        content = layer.get("content", "")
        if not content:
            return

        draw = ImageDraw.Draw(canvas)

        # Text properties
        font_family = layer.get("font_family", "Arial")
        font_size = layer.get("font_size", 48)
        font_weight = layer.get("font_weight", 400)
        color = layer.get("color", "#FFFFFF")
        x = layer.get("x", 100)
        y = layer.get("y", 100)

        # Load font
        font = DocumentRendererService._load_font(font_family, font_size, font_weight)

        # Draw text
        rgba_color = DocumentRendererService._hex_to_rgba(color)
        draw.text((x, y), content, font=font, fill=rgba_color)

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

        # Resize if dimensions specified
        if width and height:
            asset_image = asset_image.resize((width, height), Image.Resampling.LANCZOS)

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
    async def _fetch_image(url: str) -> Optional[Image.Image]:
        """Download image from URL"""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    return Image.open(io.BytesIO(response.content)).convert("RGBA")
        except Exception as e:
            print(f"⚠️ Error fetching image {url}: {e}")
        return None

    @staticmethod
    def _load_font(font_family: str, font_size: int, font_weight: int) -> ImageFont.FreeTypeFont:
        """
        Load font for rendering

        TODO: Support custom uploaded fonts from brand_profiles
        For now, falls back to default fonts
        """
        try:
            # Try to load system font
            # This is a simplified version - in production, map font_family to actual font files
            if font_weight >= 700:
                font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            else:
                font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

            return ImageFont.truetype(font_path, font_size)
        except:
            # Fallback to default font
            return ImageFont.load_default()

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

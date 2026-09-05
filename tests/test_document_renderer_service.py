"""
DocumentRendererService._render_background — regression coverage for a
real bug found while building VSG-01's Problem/Solution ad format: this
layer type ignored a layer's own x/y/width/height and always resized the
image to the full canvas and pasted at (0, 0). Harmless for every prior
caller (each either passed the full canvas explicitly or omitted the
fields, both of which this preserves exactly), but a real bug the moment a
document places two ai_generated_background layers in different zones of
the same canvas — the second one's full-canvas paste silently overwrote
the first zone's scrim and text entirely (confirmed with a real render
before the fix, then confirmed resolved with a real render after it — see
the commit this test ships in).

Mocks only DocumentRendererService._fetch_image (the network boundary) so
these tests are fast and deterministic; the actual compositing logic
(resize + paste at a position) runs for real via Pillow.
"""
from unittest.mock import patch, AsyncMock

from PIL import Image

from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService


def _run(coro):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro)


def _solid(color, size=(100, 100)):
    return Image.new("RGB", size, color)


class TestRenderBackgroundRespectsLayerPosition:
    def test_explicit_dimensions_are_placed_not_stretched_to_canvas(self):
        """A background layer smaller than the canvas, at a non-zero
        position, must render at exactly that position/size."""
        document = {
            "canvas": {"width": 200, "height": 200, "background_color": "#000000"},
            "layers": [
                {"type": "shape", "shape": "rect", "z_index": 0, "x": 0, "y": 0,
                 "width": 200, "height": 200, "fill_color": "#000000"},
                {"type": "ai_generated_background", "z_index": 1, "url": "https://example.com/red.png",
                 "x": 50, "y": 50, "width": 50, "height": 50},
            ],
        }
        with patch.object(DocumentRendererService, "_fetch_image", AsyncMock(return_value=_solid((255, 0, 0)))):
            png = _run(DocumentRendererService.render_to_png(document))
        img = Image.open(__import__("io").BytesIO(png)).convert("RGB")
        assert img.getpixel((75, 75)) == (255, 0, 0)   # inside the placed region
        assert img.getpixel((10, 10)) == (0, 0, 0)     # outside it — still the canvas colour

    def test_two_backgrounds_in_different_zones_do_not_overwrite_each_other(self):
        """Direct regression test for the Problem/Solution bug: a second
        ai_generated_background layer in a different zone must not erase
        the first zone's content."""
        document = {
            "canvas": {"width": 200, "height": 200, "background_color": "#FFFFFF"},
            "layers": [
                {"type": "ai_generated_background", "z_index": 1, "url": "https://example.com/top.png",
                 "x": 0, "y": 0, "width": 200, "height": 100},
                {"type": "shape", "shape": "rect", "z_index": 2, "x": 0, "y": 0,
                 "width": 200, "height": 30, "fill_color": "#00FF00"},
                {"type": "ai_generated_background", "z_index": 3, "url": "https://example.com/bottom.png",
                 "x": 0, "y": 100, "width": 200, "height": 100},
            ],
        }
        images = {"https://example.com/top.png": _solid((255, 0, 0)),
                  "https://example.com/bottom.png": _solid((0, 0, 255))}

        async def fake_fetch(url):
            return images[url]

        with patch.object(DocumentRendererService, "_fetch_image", AsyncMock(side_effect=fake_fetch)):
            png = _run(DocumentRendererService.render_to_png(document))
        img = Image.open(__import__("io").BytesIO(png)).convert("RGB")
        assert img.getpixel((10, 15)) == (0, 255, 0)    # the top zone's scrim, still intact
        assert img.getpixel((10, 60)) == (255, 0, 0)    # top zone's own photo, not overwritten
        assert img.getpixel((10, 150)) == (0, 0, 255)   # bottom zone's own photo

    def test_omitted_dimensions_default_to_full_canvas(self):
        """Backward compatibility: a layer with no x/y/width/height at all
        (layered_document_service.py's own usage pattern) must still fill
        the whole canvas, exactly as before this fix."""
        document = {
            "canvas": {"width": 100, "height": 100, "background_color": "#000000"},
            "layers": [
                {"type": "ai_generated_background", "z_index": 1, "url": "https://example.com/full.png"},
            ],
        }
        with patch.object(DocumentRendererService, "_fetch_image", AsyncMock(return_value=_solid((10, 20, 30)))):
            png = _run(DocumentRendererService.render_to_png(document))
        img = Image.open(__import__("io").BytesIO(png)).convert("RGB")
        assert img.getpixel((0, 0)) == (10, 20, 30)
        assert img.getpixel((99, 99)) == (10, 20, 30)

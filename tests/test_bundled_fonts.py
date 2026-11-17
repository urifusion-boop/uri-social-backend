"""Bundled fonts for rendered images (app/utils/fonts.py)."""
import asyncio
import os

from PIL import Image, ImageDraw

from app.utils import fonts
from app.agents.social_media_manager.services.document_renderer_service import DocumentRendererService

PLAYBOOK_FONTS = [
    "Playfair Display", "Inter", "Bebas Neue", "Great Vibes", "Montserrat", "Roboto Slab",
    "Abril Fatface", "Cormorant", "Teko", "Permanent Marker", "Saira Condensed", "JetBrains Mono",
    "Nunito", "Poiret One", "Bungee Shade", "Cormorant Garamond",
]
DISPLAY_FONTS = ["Anton", "Bebas Neue", "Archivo Black", "Permanent Marker", "Poppins", "Montserrat", "Inter"]


def test_every_playbook_and_display_font_is_bundled():
    for family in set(PLAYBOOK_FONTS + DISPLAY_FONTS):
        assert fonts.bundled_weights(family), family


def test_every_bundled_family_ships_its_licence():
    for slug in fonts.available_families():
        assert os.path.isfile(os.path.join(fonts.FONTS_DIR, slug, "LICENSE.txt")), slug


def test_family_and_weight_pick_the_matching_file():
    assert fonts.font_path("Poppins", 800).endswith("Poppins/Poppins-800.ttf")
    assert fonts.font_path("Bebas Neue", 400).endswith("BebasNeue/BebasNeue-400.ttf")


def test_missing_weight_goes_to_the_nearest_and_ties_go_heavier():
    assert fonts.font_path("Anton", 900).endswith("Anton-400.ttf")  # only one weight
    assert fonts.font_path("Nunito", 750).endswith("Nunito-800.ttf")  # 700 and 800 equally near
    assert fonts.font_path("Playfair Display", 300).endswith("PlayfairDisplay-400.ttf")


def test_unbundled_family_keeps_the_old_dejavu_default():
    assert fonts.font_path("Arial", 400) == fonts.DEJAVU
    assert fonts.font_path("Arial", 700) == fonts.DEJAVU_BOLD
    assert fonts.font_path(None, 400) == fonts.DEJAVU


def test_naira_falls_back_to_inter_where_the_font_has_none():
    runs = fonts.text_runs("Only ₦5,000 today", "Poppins", 700)
    assert [r for r, _ in runs] == ["Only ", "₦", "5,000 today"]
    assert runs[1][1] == fonts.font_path("Inter", 700)
    assert runs[0][1] == runs[2][1] == fonts.font_path("Poppins", 700)


def test_font_that_has_naira_draws_in_one_run():
    assert fonts.text_runs("₦5,000", "Inter", 700) == [("₦5,000", fonts.font_path("Inter", 700))]


def test_width_counts_the_fallback_run():
    whole = fonts.text_width("₦5,000", "Poppins", 60, 700)
    parts = fonts.text_width("₦", "Inter", 60, 700) + fonts.text_width("5,000", "Poppins", 60, 700)
    assert abs(whole - parts) < 0.01


def _ink(img):
    return img.convert("L").point(lambda v: 255 if v < 128 else 0).getbbox()


def test_mixed_run_line_honours_right_and_centre_anchors():
    for anchor, x in (("ra", 900), ("ma", 500), ("la", 100)):
        img = Image.new("RGB", (1000, 200), "white")
        fonts.draw_text(ImageDraw.Draw(img), (x, 40), "₦5,000", "Poppins", 80, 700, fill="black", anchor=anchor)
        left, _, right, _ = _ink(img)
        if anchor == "ra":
            assert abs(right - 900) <= 8
        elif anchor == "la":
            assert abs(left - 100) <= 8
        else:
            assert abs((left + right) / 2 - 500) <= 8


def test_renderer_draws_a_bundled_font():
    canvas = Image.new("RGBA", (800, 300), "white")
    layer = {"type": "text", "content": "Fresh Crops ₦2,500", "font_family": "Poppins",
             "font_weight": 900, "font_size": 64, "color": "#000000", "x": 20, "y": 40}
    asyncio.run(DocumentRendererService._render_text(canvas, layer))
    assert _ink(canvas) is not None


def test_renderer_font_and_measuring_font_are_the_same_file():
    from app.agents.jane_ads.ad_formats._text_metrics import load_measuring_font

    assert load_measuring_font(48, 700, "Montserrat").path == DocumentRendererService._load_font("Montserrat", 48, 700).path

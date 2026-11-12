"""
Live-diagnosed real bug: recomposite ad generation with a real perfume product
photo failed outright with "Prompt contains 'null' - aborting image generation".
GPT-4o-mini vision sometimes returns the literal string "null" instead of a real
JSON null for an optional field (its own prompt says "or null if not visible") —
custom_visual_guide_service.py already knows to filter this quirk, but
build_preservation_block didn't, so the literal text flowed straight into the
image-generation prompt ("Logo: null", "Liquid colour: null.") and tripped
image_content_service's own "prompt contains 'null'" validation guard.
"""
from app.agents.social_media_manager.services.product_analysis_service import ProductAnalysisService


def test_literal_null_string_from_vision_never_reaches_the_prompt():
    spec = {
        "product_type": "perfume bottle",
        "overall_shape": "tall cylinder",
        "height_width_ratio": "3:1",
        "cap_closure": {"type": "cap", "colour": "silver", "material": "metal"},
        "body": {"material": "glass", "colour": "clear", "finish": "glossy"},
        "liquid_visible": True,
        "liquid_colour": "null",  # the exact GPT-vision quirk
        "label": {
            "present": True,
            "position": "front",
            "background_colour": "white",
            "text_lines": ["BRAND NAME"],
            "text_colour": "black",
            "font_style": "serif",
            "logo_description": "null",  # same quirk
        },
        "additional_details": "null",
        "dominant_colours_hex": ["#ffffff", "null"],
    }
    block = ProductAnalysisService.build_preservation_block(spec)
    assert "null" not in block.lower()


def test_real_json_none_still_handled_gracefully():
    spec = {
        "product_type": "perfume bottle",
        "liquid_visible": False,
        "liquid_colour": None,
        "label": {"present": False, "logo_description": None, "text_lines": []},
    }
    block = ProductAnalysisService.build_preservation_block(spec)
    assert "null" not in block.lower()
    assert "perfume bottle" in block

"""
Template Library Configuration
Maps Visual Style Guides → Templates
"""

from typing import Dict, List
from ..models.visual_engine_models import VisualEngineTemplateV2


# ============================================================================
# TEMPLATE LIBRARY - Mapped from Visual Style Guides
# ============================================================================

TEMPLATE_LIBRARY: List[Dict] = [
    # Sale/Promo Templates
    {
        "template_id": "sale_poster_bold_v1",
        "name": "Bold Sale Poster",
        "style_family": "bold_modern",
        "format": "1:1",
        "post_intent": "sale",
        "image_path": "both",
        "orshot_template_id": None,  # To be created in Orshot
        "slots": {
            "product_image": "image",
            "headline": "text",
            "subhead": "text",
            "promo": "text",
            "logo": "image",
            "brand_primary": "color",
            "brand_font": "font"
        }
    },
    {
        "template_id": "sale_poster_bold_story_v1",
        "name": "Bold Sale Story",
        "style_family": "bold_modern",
        "format": "9:16",
        "post_intent": "sale",
        "image_path": "both",
        "orshot_template_id": None,
        "slots": {
            "product_image": "image",
            "headline": "text",
            "subhead": "text",
            "promo": "text",
            "logo": "image",
            "brand_primary": "color",
            "brand_font": "font"
        }
    },

    # Product Feature Templates
    {
        "template_id": "product_showcase_minimal_v1",
        "name": "Minimal Product Showcase",
        "style_family": "minimal_clean",
        "format": "1:1",
        "post_intent": "product",
        "image_path": "path_b",  # Best for real product photos
        "orshot_template_id": None,
        "slots": {
            "product_image": "image",
            "headline": "text",
            "feature_list": "text",
            "cta": "text",
            "logo": "image",
            "brand_primary": "color",
            "brand_font": "font"
        }
    },

    # Announcement Templates
    {
        "template_id": "announcement_centered_v1",
        "name": "Centered Announcement",
        "style_family": "modern_professional",
        "format": "1:1",
        "post_intent": "announcement",
        "image_path": "path_a",  # Generated backgrounds work well
        "orshot_template_id": "14698",  # Studio template (POST /v1/studio/render) — real fields differ from our vocabulary, see field_mapping
        "slots": {
            "background_image": "image",
            "headline": "text",
            "body": "text",
            "cta": "text",
            "logo": "image",
            "brand_primary": "color",
            "brand_font": "font"
        },
        # This Studio template's actual field names (eyebrow/title_top/title_main/
        # date/venue/host, an event-invitation layout) don't match our abstract
        # vocabulary at all — proves the system doesn't require them to. Maps our
        # key -> this template's real key; static_fields fills whatever's left.
        "field_mapping": {
            "promo": "eyebrow",
            "subhead": "title_top",
            "headline": "title_main",
            "cta": "host",
        },
        "static_fields": {
            "date": "",
            "venue": "",
        },
    },

    # Educational/Tips Templates
    {
        "template_id": "tips_card_modern_v1",
        "name": "Modern Tips Card",
        "style_family": "educational",
        "format": "1:1",
        "post_intent": "educational",
        "image_path": "both",
        "orshot_template_id": None,
        "slots": {
            "icon_or_image": "image",
            "tip_number": "text",
            "tip_headline": "text",
            "tip_body": "text",
            "logo": "image",
            "brand_primary": "color",
            "brand_font": "font"
        }
    },

    # Testimonial Templates
    {
        "template_id": "testimonial_quote_elegant_v1",
        "name": "Elegant Quote Testimonial",
        "style_family": "testimonial_social_proof",
        "format": "1:1",
        "post_intent": "testimonial",
        "image_path": "path_b",  # Customer photos
        "orshot_template_id": None,
        "slots": {
            "customer_image": "image",
            "quote": "text",
            "customer_name": "text",
            "rating": "text",
            "logo": "image",
            "brand_primary": "color",
            "brand_font": "font"
        }
    }
]


# ============================================================================
# TEMPLATE SELECTION LOGIC
# ============================================================================

def select_template(
    style_family: str,
    post_intent: str,
    format: str = "1:1",
    image_path: str = "both"
) -> str:
    """
    Deterministic template selection based on PRD Section 15

    Priority order:
    1. Client's assigned style (style_family)
    2. Post type/intent
    3. Format required
    4. Image source (path_a or path_b)
    """
    # Filter templates by criteria
    candidates = [
        t for t in TEMPLATE_LIBRARY
        if t["style_family"] == style_family
        and t["post_intent"] == post_intent
        and t["format"] == format
        and (t["image_path"] == image_path or t["image_path"] == "both")
    ]

    if candidates:
        return candidates[0]["template_id"]

    # Fallback 1: Relax image_path requirement
    candidates = [
        t for t in TEMPLATE_LIBRARY
        if t["style_family"] == style_family
        and t["post_intent"] == post_intent
        and t["format"] == format
    ]

    if candidates:
        return candidates[0]["template_id"]

    # Fallback 2: Use default style with matching intent
    candidates = [
        t for t in TEMPLATE_LIBRARY
        if t["post_intent"] == post_intent
        and t["format"] == format
    ]

    if candidates:
        return candidates[0]["template_id"]

    # Final fallback: First template in library
    return TEMPLATE_LIBRARY[0]["template_id"]


def get_template_config(template_id: str) -> Dict:
    """Get template configuration by ID"""
    for template in TEMPLATE_LIBRARY:
        if template["template_id"] == template_id:
            return template
    return TEMPLATE_LIBRARY[0]  # Fallback


# ============================================================================
# STYLE FAMILY MAPPINGS
# ============================================================================

STYLE_FAMILIES = {
    "bold_modern": "Bold, high-contrast, attention-grabbing",
    "minimal_clean": "Clean, spacious, professional",
    "modern_professional": "Balanced, corporate-friendly",
    "educational": "Clear hierarchy, readable, informative",
    "testimonial_social_proof": "Trust-building, customer-focused",
    "playful_colorful": "Vibrant, energetic, youth-oriented",
    "elegant_luxury": "Sophisticated, premium, refined"
}


POST_INTENTS = {
    "sale": "Promotional offers, discounts, limited-time deals",
    "product": "Product features, benefits, specifications",
    "announcement": "News, updates, launches",
    "educational": "Tips, how-tos, guides",
    "testimonial": "Customer reviews, social proof",
    "carousel": "Multi-slide storytelling"
}

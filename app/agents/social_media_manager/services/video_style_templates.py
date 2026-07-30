# app/agents/social_media_manager/services/video_style_templates.py
#
# Template = style config. Controls HOW the video looks, not WHERE edits happen.
# AI still decides cut points, b-roll, caption text. Templates set the execution style.

from typing import Dict, Any

VIDEO_STYLE_TEMPLATES: Dict[str, Dict[str, Any]] = {
    "product_showcase": {
        "id": "product_showcase",
        "name": "Product Showcase",
        "feel": "Clean, bright, aspirational",
        # Underlying video type for GPT analysis style guidance
        "video_type": "product",
        # Pacing — silence gap (seconds) that triggers a cut
        "silence_threshold": 0.5,   # medium, lets product breathe
        # Captions
        "caption_font": "Montserrat",
        "caption_color": "#ffffff",
        # Transitions
        "transition_style": "swipe",  # mostly hard cuts, soft dissolves
        # Music
        "music_volume": 0.06,         # low under voice
        "music_mood": "upbeat",
    },
    "fast_founder": {
        "id": "fast_founder",
        "name": "Fast Founder",
        "feel": "Punchy, direct, high-energy",
        "video_type": "founder",
        "silence_threshold": 0.35,  # tight — trims most pauses
        "caption_font": "Anton",
        "caption_color": "#ffffff",
        "transition_style": "flash",  # jump-cut IS the style
        "music_volume": 0.08,
        "music_mood": "upbeat",
    },
    "customer_testimonial": {
        "id": "customer_testimonial",
        "name": "Customer Testimonial",
        "feel": "Warm, authentic, trustworthy",
        "video_type": "founder",
        "silence_threshold": 0.8,   # keeps natural human pauses
        "caption_font": "Montserrat",
        "caption_color": "#ffffff",
        "transition_style": "swipe",  # soft dissolves
        "music_volume": 0.05,         # very soft under testimony
        "music_mood": "acoustic",
    },
    "tiktok_energy": {
        "id": "tiktok_energy",
        "name": "TikTok Energy",
        "feel": "Fast, loud, hyper-engaging",
        "video_type": "tiktok",
        "silence_threshold": 0.25,  # zero dead air
        "caption_font": "Bangers",
        "caption_color": "#FFD700",   # high-contrast yellow
        "transition_style": "flash",  # whip / snappy
        "music_volume": 0.12,         # prominent
        "music_mood": "electronic",
    },
}

DEFAULT_TEMPLATE_ID = "fast_founder"


def get_template(template_id: str) -> Dict[str, Any]:
    return VIDEO_STYLE_TEMPLATES.get(template_id, VIDEO_STYLE_TEMPLATES[DEFAULT_TEMPLATE_ID])

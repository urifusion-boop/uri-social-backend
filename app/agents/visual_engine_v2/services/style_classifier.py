"""
Style-family classifier — Visual Engine V2 only.

The backend style library (147 slugs in social_media_manager/style_library.py,
V1) predates V2's 7 template families and was never tagged with them —
retrofitting all 147 by hand would be arbitrary busywork for the many entries
that don't map cleanly to one family. Instead this scores each of the brand's
already-selected styles' free-text description/prompt_fragment against keyword
sets for the 7 families and picks the best match, so V2 reuses the brand's
*existing* style choices rather than asking them to pick a style twice.

Read-only: imports STYLES from V1's style_library.py, never modifies it.
"""
from typing import Dict, List, Optional
from collections import Counter

from app.agents.social_media_manager.services.style_library import STYLES

_FAMILY_KEYWORDS: Dict[str, List[str]] = {
    "bold_modern": [
        "bold", "vivid", "neon", "electric", "dramatic", "high contrast", "edgy",
        "street", "punchy", "graffiti", "loud", "cyberpunk",
    ],
    "minimal_clean": [
        "minimal", "clean", "negative space", "whitespace", "restraint",
        "pure white", "spacious", "simple", "airy", "breathing room",
    ],
    "modern_professional": [
        "professional", "corporate", "balanced", "business", "polished",
        "sleek", "structured", "trustworthy",
    ],
    "educational": [
        "clear hierarchy", "readable", "informative", "tutorial", "step",
        "instructional", "explainer", "infographic", "how-to",
    ],
    "testimonial_social_proof": [
        "testimonial", "review", "trust", "customer", "quote", "social proof",
        "authentic", "candid", "real people",
    ],
    "playful_colorful": [
        "playful", "vibrant", "colorful", "colourful", "fun", "youth",
        "energetic", "bright", "whimsical", "pop",
    ],
    "elegant_luxury": [
        "elegant", "luxury", "luxe", "premium", "sophisticated", "refined",
        "serif", "opulent", "high-end", "editorial",
    ],
}

_INDUSTRY_DEFAULTS: Dict[str, str] = {
    "fashion_ecommerce": "elegant_luxury",
    "beauty_wellness": "elegant_luxury",
    "food_beverage": "playful_colorful",
    "fitness_gym": "bold_modern",
    "real_estate": "modern_professional",
    "events_entertainment": "bold_modern",
    "tech_saas": "modern_professional",
    "general_other": "modern_professional",
}

DEFAULT_STYLE_FAMILY = "modern_professional"


def classify_style_family(style_selections: Optional[List[str]], industry: Optional[str] = None) -> str:
    """
    Best-effort mapping from a brand's chosen backend styles (style_selections,
    slugs into V1's STYLES) to one of V2's 7 template families. Falls back to
    an industry default, then a global default, if there's no keyword signal.
    """
    scores: Counter = Counter()

    for slug in (style_selections or []):
        style = STYLES.get(slug)
        if not style:
            continue
        text = " ".join([
            style.get("name", ""),
            style.get("description", ""),
            style.get("prompt_fragment", ""),
        ]).lower()
        for family, keywords in _FAMILY_KEYWORDS.items():
            hits = sum(1 for kw in keywords if kw in text)
            if hits:
                scores[family] += hits

    if scores:
        return scores.most_common(1)[0][0]

    if industry and industry in _INDUSTRY_DEFAULTS:
        return _INDUSTRY_DEFAULTS[industry]

    return DEFAULT_STYLE_FAMILY

"""
The 12 video styles, and reading one back out of a structured brief.

The script writer (video_storyboard_service.VIDEO_STYLE_DIRECTIVES) holds the full directive for each style. This module keeps
their names and one-line gists, light enough for the prompt expander and the script writer to share without importing the heavy
storyboard service. A test keeps the slugs in step with VIDEO_STYLE_DIRECTIVES.
"""
import re
from typing import Optional

VIDEO_STYLES: dict[str, tuple[str, str]] = {
    "clean_commercial": ("Clean Commercial", "polished brand commercial: smooth dolly moves, hero product shots, clean cuts"),
    "luxury_slow_burn": ("Luxury Slow Burn", "premium and cinematic: macro close-ups, very slow push-ins, long unhurried scenes"),
    "viral_fast_cut": ("Viral Fast Cut", "high-energy and social-native: handheld, snap zooms, very fast cuts, an instant hook"),
    "ingredient_reveal": ("Ingredient Reveal", "a sensory reveal of raw ingredients in macro texture, building to the hero product"),
    "street_style": ("Street Style", "authentic urban documentary: handheld, natural rhythm, real-life moments"),
    "unboxing_drama": ("Unboxing Drama", "a theatrical build-up and reveal of a product and its packaging, hands in close-up"),
    "before_after": ("Before / After", "a problem then its transformation: matched angles, muted first half, bright second half"),
    "mood_film": ("Mood Film", "atmosphere and emotion over product information: wide, slow, golden-hour feel"),
    "product_explosion": ("Product Explosion", "dynamic, physics-defying product visuals: orbits, particles, freeze-frames"),
    "testimonial_style": ("Testimonial Style", "an authentic person-to-camera testimonial or interview, conversational pacing"),
    "menu_showcase": ("Menu Showcase", "food that looks irresistible: overhead and glamour angles, lingering on texture"),
    "countdown_hype": ("Countdown Hype", "urgency that escalates scene by scene: each scene tighter and faster than the last"),
}


_BY_NAME = {name.lower(): slug for slug, (name, _gist) in VIDEO_STYLES.items()}
_STYLE_LINE = re.compile(r"(?im)^[ \t]*STYLE[ \t]*:[ \t]*(.+?)[ \t]*$")


def style_from_brief(brief: Optional[str]) -> Optional[str]:
    """The style slug named on a "STYLE: <name>." line of a structured brief (see prompt_expander.compose_video), or None.
    Takes the style's name or its slug, in any case, with or without the full stop. Only a line that STARTS with STYLE: counts,
    so the word "style" inside the owner's own sentences is never mistaken for it."""
    for match in _STYLE_LINE.finditer(brief or ""):
        raw = match.group(1).strip().rstrip(".").strip().lower()
        if raw in VIDEO_STYLES:
            return raw
        if raw in _BY_NAME:
            return _BY_NAME[raw]
    return None

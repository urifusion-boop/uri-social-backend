"""
The caption that goes with a finished video.

It used to be written from an empty script (the route read a `scene_description` field that Describe It
scenes don't have, and a `brand_context` the browser never sends), so every video got the same generic
"Blink and you'll miss it!" caption. It is now written from what the video is actually about (the creative
direction and each scene's on-screen text and spoken line) and from the brand's own Playbook, and finished with
the Playbook's word rules and call to action, exactly as other captions are.
"""
import re
from typing import Any, Dict, List, Optional

from . import brand_brief

PLATFORM_GUIDES = {
    "instagram": "Instagram — max 2200 chars, conversational, 3–5 relevant hashtags at the end, 1–2 emojis in the body.",
    "facebook": "Facebook — max 500 chars, warm and conversational, 0–2 hashtags, no emoji overload.",
    "tiktok": "TikTok — punchy, max 150 chars, 3–5 trending hashtags, high-energy tone.",
    "linkedin": "LinkedIn — professional tone, 1–3 paragraphs, 3–5 industry hashtags, no excessive emojis.",
}


def script_summary(storyboard: Dict[str, Any]) -> str:
    """What the video says and shows, in the order it plays."""
    lines: List[str] = []
    direction = str((storyboard or {}).get("creative_direction") or "").strip()
    if direction:
        lines.append(f"Creative direction: {direction}")
    for i, scene in enumerate((storyboard or {}).get("scenes") or []):
        if not isinstance(scene, dict):
            continue
        n = scene.get("scene_number", i + 1)
        shown = str(scene.get("scene_description") or scene.get("description") or scene.get("video_prompt") or "").strip()
        parts = [f"shows: {shown[:200]}"] if shown else []
        if str(scene.get("text_overlay") or "").strip():
            parts.append(f'on-screen text: "{scene["text_overlay"].strip()}"')
        if str(scene.get("dialogue") or "").strip():
            parts.append(f'spoken: "{scene["dialogue"].strip()}"')
        if parts:
            lines.append(f"Scene {n} — " + "; ".join(parts))
    return "\n".join(lines)


def build_prompt(storyboard: Dict[str, Any], platform: str, bc: Optional[Dict[str, Any]]) -> str:
    platform = platform if platform in PLATFORM_GUIDES else "instagram"
    bc = brand_brief.for_platform(bc, platform)
    brief = brand_brief.build(bc, sections=("rules", "voice", "audience", "offer", "pulse"), platform=platform,
                              captions=True, kind="video")
    name = (bc or {}).get("brand_name") or "the brand"
    return f"""You are a social media copywriter. Write a single ready-to-post caption for a video.

PLATFORM: {platform.upper()}
GUIDE: {PLATFORM_GUIDES[platform]}

THE VIDEO ({int((storyboard or {}).get("total_duration_seconds") or 0)}s, for {name}):
{script_summary(storyboard) or "(no script available)"}

{brief}

Rules:
- Write about THIS video: its message and what happens in it, in the brand's own voice. Never a generic line
  that could sit under any video.
- Write ONLY the caption text — no labels, no explanation, no markdown wrapper.
- Make it feel native to {platform}, not copy-paste generic.
- Hashtags go at the very end, on a new line.
- Do not include a trailing newline."""


_HASHTAG_LINE = re.compile(r"^\s*(#\S+\s*)+$")


def finish(caption: str, bc: Optional[Dict[str, Any]]) -> str:
    """What the Playbook can guarantee without a model: no banned word or emoji it forbids, no more hashtags
    than it allows, and the brand's call to action closing the text, above the hashtags."""
    text = (caption or "").strip()
    if not text:
        return text
    lines = text.split("\n")
    tags: List[str] = []
    if lines and _HASHTAG_LINE.match(lines[-1]):
        tags = lines.pop().split()
    body = "\n".join(lines).rstrip()
    body = brand_brief.enforce(body, bc, append_cta=True)
    tags = brand_brief.limit_hashtags(tags, bc)
    return body + ("\n\n" + " ".join(tags) if tags else "")

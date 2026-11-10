"""
The brand's real logo, stamped onto a generated video.

A picture or video model cannot reproduce a logo (it garbles it, the way it garbled text), so the logo is never
drawn into the scenes. The owner's logo file from the Brand Playbook is laid over the finished video by ffmpeg when
the clips are joined: the real file, sharp, in the corner the owner chose, steady for the whole video.
"""
from typing import Any, Dict, Optional, Tuple

# Share of the video's width, by the Brand Playbook's logo size. A little larger than on a still post (8/12/16%)
# because a video is watched small on a phone.
LOGO_WIDTH_FRACTION = {"small": 0.12, "medium": 0.16, "large": 0.2}
DEFAULT_SIZE = "small"
DEFAULT_POSITION = "bottom_right"

# Reels and TikTok put their own buttons and caption over the bottom and right edge, so the logo sits clear of them:
# well above the very bottom, and the caption (which ends 20% up) stays clear of it.
_SIDE_MARGIN_FRACTION = 0.05
_TOP_MARGIN_FRACTION = 0.08
_BOTTOM_MARGIN_FRACTION = 0.09


def logo_from_brand_context(brand_context: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """The playbook's logo (url, position, size) if the owner has one, else None."""
    bc = brand_context or {}
    url = str(bc.get("logo_url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return None
    size = str(bc.get("logo_size") or DEFAULT_SIZE).lower()
    position = str(bc.get("logo_position") or DEFAULT_POSITION).lower().replace("-", "_")
    return {"url": url, "position": position, "size": size if size in LOGO_WIDTH_FRACTION else DEFAULT_SIZE}


def logo_filter(position: str, size: str, video_width: int, video_height: int) -> str:
    """ffmpeg -filter_complex: input 0 is the video, input 1 the logo; scales the logo and overlays it."""
    width = max(48, int(video_width * LOGO_WIDTH_FRACTION.get(size, LOGO_WIDTH_FRACTION[DEFAULT_SIZE])))
    side = int(video_width * _SIDE_MARGIN_FRACTION)
    top = int(video_height * _TOP_MARGIN_FRACTION)
    bottom = int(video_height * _BOTTOM_MARGIN_FRACTION)
    xy = {
        "top_left": (f"{side}", f"{top}"),
        "top_center": ("(W-w)/2", f"{top}"),
        "top_right": (f"W-w-{side}", f"{top}"),
        "bottom_left": (f"{side}", f"H-h-{bottom}"),
        "bottom_center": ("(W-w)/2", f"H-h-{bottom}"),
        "bottom_right": (f"W-w-{side}", f"H-h-{bottom}"),
        "center": ("(W-w)/2", "(H-h)/2"),
    }.get(position, (f"W-w-{side}", f"H-h-{bottom}"))
    return f"[1:v]scale={width}:-2,format=rgba[logo];[0:v][logo]overlay={xy[0]}:{xy[1]}:format=auto[vout]"

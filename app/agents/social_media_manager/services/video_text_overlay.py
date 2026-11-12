"""
Sharp, readable on-screen text for generated videos.

The text used to be drawn into each storyboard picture by the image model ("On-screen text: ..."), and the
clip model then animated that picture, so letters wobbled and smeared (and image models misspell short
words anyway). Now the pictures carry no added text, and the words are drawn here by ffmpeg when the clips
are joined, after the clip has been scaled to its final 1080x1920: real font, real spelling, no blur.
"""
import re
from typing import List, Optional, Tuple

# The same font file the hook-text burn-in in the router already relies on in the container.
FONT_FILE = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

_SIDE_SAFE = 110          # px kept clear each side (platform buttons sit on the right edge)
_START_SECONDS = 0.25     # text appears just after the cut
_END_PAD_SECONDS = 0.2
_BOTTOM_SAFE_FRACTION = 0.2   # text block ends this far above the bottom: clear of captions/Reels/TikTok UI
_FONT_SIZE = 66
_MIN_FONT_SIZE = 42
_MAX_LINES = 4

# Emoji and other characters outside the font's range come out as empty boxes, so they are dropped.
_UNSUPPORTED = re.compile("[\U00010000-\U0010FFFF☀-➿️‍]")


def clean_overlay_text(text: Optional[str]) -> str:
    return re.sub(r"\s+", " ", _UNSUPPORTED.sub("", str(text or ""))).strip()


def wrap_overlay_text(text: str, video_width: int = 1080, fontsize: int = _FONT_SIZE) -> Tuple[str, int]:
    """The text broken into lines that fit inside the video's width, at the largest size that gives at most
    _MAX_LINES lines (never below _MIN_FONT_SIZE). Returns (text with newlines, font size)."""
    words = clean_overlay_text(text).split()
    if not words:
        return "", fontsize
    usable = max(video_width - 2 * _SIDE_SAFE, 240)
    size = fontsize
    lines: List[str] = []
    while True:
        # DejaVu Sans Bold averages about 0.66 x the font size per character; this is deliberately generous so
        # a line wraps early rather than running into the edge.
        max_chars = max(int(usable / (size * 0.66)), 4)
        lines, line = [], ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if len(candidate) <= max_chars or not line:
                line = candidate
            else:
                lines.append(line)
                line = word
        if line:
            lines.append(line)
        if len(lines) <= _MAX_LINES or size <= _MIN_FONT_SIZE:
            break
        size = max(int(size * 0.88), _MIN_FONT_SIZE)
    return "\n".join(lines[:_MAX_LINES]), size


def drawtext_filter(textfile: str, fontsize: int, clip_seconds: Optional[float] = None) -> str:
    """The ffmpeg drawtext filter for one clip: white bold text with a dark outline on a soft dark box, centred
    in the lower part of the frame. `textfile` holds the (already wrapped) text, so nothing needs escaping;
    expansion=none stops a % in the copy being read as a drawtext command."""
    enable = ""
    if clip_seconds and clip_seconds > _START_SECONDS + _END_PAD_SECONDS + 0.5:
        enable = f":enable='between(t,{_START_SECONDS},{clip_seconds - _END_PAD_SECONDS:.2f})'"
    return (
        f"drawtext=fontfile={FONT_FILE}:textfile={textfile}:expansion=none:fontsize={fontsize}:"
        "fontcolor=white:borderw=4:bordercolor=black@0.9:line_spacing=10:"
        f"x=(w-text_w)/2:y=h-text_h-h*{_BOTTOM_SAFE_FRACTION}:"
        "box=1:boxcolor=black@0.45:boxborderw=26"
        + enable
    )


# Spliced into the script writers' system prompts by .replace(), like the people rules.
SCREEN_TEXT_PLACEHOLDER = "{{SCREEN_TEXT_RULES}}"

# Text that is part of an object in the scene (a dashboard on a laptop, a phone screen, a sign, a menu) is drawn by the
# picture model and animated by the clip model, and it is only readable when it is large in the frame: the same
# dashboard is sharp in a push-in close-up and soft in a wide shot. So the writers frame such a scene for big text.
SCREEN_TEXT_RULES = (
    "- TEXT INSIDE THE SCENE (a dashboard, phone or laptop screen, sign, menu, label): AI video only draws text that is "
    "LARGE in the frame; small text blurs. Only include it if the story needs it, and then: (a) about three short words "
    "or figures at most, written out exactly in that scene's video_prompt; (b) frame the scene so that text fills a large "
    "share of the picture: a close-up, or a slow push-in on the screen, never a wide shot with small print; (c) in any "
    "wide or medium shot show screens, signs and menus as soft shapes, colours and simple charts with no readable words. "
    "Never ask for paragraphs, tables, tiny labels or a screen full of figures."
)

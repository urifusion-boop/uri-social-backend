"""Checks a Meta plan can really run, made while the client is still reviewing it.

Meta accepts an ad and then quietly refuses to deliver it — a WhatsApp ad with no
WhatsApp linked to the Page, a video in a shape Reels and Stories can't use — so the
client believes it is running when it isn't. These return plain-language warnings the
plan card shows before anything launches. They never block a launch, and a check that
can't be made returns nothing rather than a false alarm.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Optional

from app.core.config import settings
from app.agents.jane_ads.ads_connection import page_has_whatsapp_linked

_RATIO_OK = ((9 / 16, 0.03), (4 / 5, 0.03), (1.0, 0.03))
_MAX_STORY_SECONDS = 60
_FORMATS_OK = {"mp4", "mov", "gif"}


def whatsapp_link_steps(number: str) -> str:
    shown = f"+{number}" if number and not number.startswith("+") else (number or "your WhatsApp number")
    return (
        "1. Open your Facebook Page and switch into it.\n"
        "2. Go to Settings → Linked accounts → WhatsApp → Connect.\n"
        f"3. Enter {shown} and the code WhatsApp sends to that phone."
    )


async def whatsapp_warning(plan) -> list[str]:
    """Meta only shows a WhatsApp number to a token holding whatsapp_business_management,
    so an absent field means "can't see", never "not linked". The only certain answer is
    the dry-run at launch; here the client is told what must be true and how to check."""
    if (plan.destination_type or "") != "whatsapp":
        return []
    linked = await page_has_whatsapp_linked(plan.page_id, settings.META_ADS_ACCESS_TOKEN)
    if linked:
        return []
    if linked is False:
        return [
            "Meta says no WhatsApp number is linked to your Facebook Page, so an ad that opens a "
            "WhatsApp chat can look set up but never deliver. To link it:\n"
            + whatsapp_link_steps(plan.whatsapp_number)
        ]
    return [
        "This ad opens a WhatsApp chat, which only works if your WhatsApp number is linked to your "
        "Facebook Page — we can't see that link from here. If you've already linked it, ignore this. "
        "To check or link it:\n" + whatsapp_link_steps(plan.whatsapp_number)
    ]


async def video_facts(url: str) -> Optional[dict]:
    """width, height, duration (s), format of a video at any URL; None if unknown.

    ffprobe reads the container headers directly over HTTP(S) — no admin API
    or hosting-provider-specific lookup needed, so this works the same for an
    S3 URL as it used to for a Cloudinary one."""
    if not url:
        return None
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        data = json.loads(stdout.decode())
    except Exception:
        return None

    width = height = 0
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            width, height = s.get("width", 0), s.get("height", 0)
            break
    if not width or not height:
        return None
    duration_raw = data.get("format", {}).get("duration")
    fmt = (data.get("format", {}).get("format_name") or "").split(",")[0]
    ext = (re.sub(r"\?.*$", "", url).rsplit(".", 1)[-1] if "." in url else fmt).lower()
    return {"width": width, "height": height,
            "duration": float(duration_raw) if duration_raw else None, "format": ext}


def video_problems(facts: dict) -> list[str]:
    problems: list[str] = []
    ratio = facts["width"] / facts["height"]
    if not any(abs(ratio - target) <= tol for target, tol in _RATIO_OK):
        problems.append(
            f"it is {facts['width']}×{facts['height']}, a shape Meta can't use on Reels and Stories")
    if facts.get("duration") and facts["duration"] > _MAX_STORY_SECONDS:
        problems.append(f"it runs {facts['duration']:.0f} seconds, over the {_MAX_STORY_SECONDS} Meta allows on Reels and Stories")
    if facts.get("format") and facts["format"] not in _FORMATS_OK:
        problems.append(f"Meta doesn't accept .{facts['format']} files")
    return problems


async def video_warning(plan) -> list[str]:
    creative = plan.creative
    if not (creative and creative.is_video):
        return []
    facts = await video_facts(creative.image_url)
    problems = video_problems(facts) if facts else []
    if not problems:
        return []
    return [
        "Meta may reject or only partly run this video: " + "; ".join(problems) + ". "
        "Crop or trim it to vertical 9:16 (1080×1920), under 60 seconds, as an MP4 or MOV, "
        "then upload it again — otherwise the ad can look set up while it isn't running."
    ]


def tiktok_location_warning(geo) -> str:
    """TikTok's location list for Nigeria stops at the country — no state or city
    beneath it (checked live against /tool/region/, 2026-10-30) — so a TikTok ad here
    always runs nationwide, whatever area the plan names."""
    from .geo import split_areas

    areas = [p.name for p in geo.pins] or split_areas(geo.city)
    named = " and ".join(areas) if len(areas) <= 2 else ", ".join(areas[:-1]) + f" and {areas[-1]}"
    return (f"TikTok can only target all of Nigeria — it has no city or state targeting here, "
            f"so this ad will show nationwide, not only in {named}. If location matters, "
            "choose Instagram & Facebook instead.")


async def meta_warnings(plan) -> list[str]:
    results = await asyncio.gather(whatsapp_warning(plan), video_warning(plan), return_exceptions=True)
    return [w for r in results if isinstance(r, list) for w in r]

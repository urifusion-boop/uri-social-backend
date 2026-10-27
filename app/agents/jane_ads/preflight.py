"""Checks a Meta plan can really run, made while the client is still reviewing it.

Meta accepts an ad and then quietly refuses to deliver it — a WhatsApp ad with no
WhatsApp linked to the Page, a video in a shape Reels and Stories can't use — so the
client believes it is running when it isn't. These return plain-language warnings the
plan card shows before anything launches. They never block a launch, and a check that
can't be made returns nothing rather than a false alarm.
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional

from app.core.config import settings
from app.agents.jane_ads.ads_connection import page_has_whatsapp_linked

_RATIO_OK = ((9 / 16, 0.03), (4 / 5, 0.03), (1.0, 0.03))
_MAX_STORY_SECONDS = 60
_FORMATS_OK = {"mp4", "mov", "gif"}
_TRANSFORM_OR_VERSION = re.compile(r"^(v\d+|[a-z]{1,3}_[^/]*)$")


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


def _public_id(url: str) -> str:
    _, sep, tail = (url or "").partition("/video/upload/")
    if not sep:
        return ""
    parts = tail.split("?")[0].split("/")
    while len(parts) > 1 and _TRANSFORM_OR_VERSION.match(parts[0]):
        parts.pop(0)
    return re.sub(r"\.\w+$", "", "/".join(parts))


async def video_facts(url: str) -> Optional[dict]:
    """width, height, duration (s), format of a Cloudinary-hosted video; None if unknown."""
    public_id = _public_id(url)
    if not public_id:
        return None

    def fetch() -> dict:
        import cloudinary.api
        return cloudinary.api.resource(public_id, resource_type="video")

    try:
        info = await asyncio.to_thread(fetch)
    except Exception:
        return None
    if not info.get("width") or not info.get("height"):
        return None
    return {"width": info["width"], "height": info["height"],
            "duration": info.get("duration"), "format": (info.get("format") or "").lower()}


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


async def meta_warnings(plan) -> list[str]:
    results = await asyncio.gather(whatsapp_warning(plan), video_warning(plan), return_exceptions=True)
    return [w for r in results if isinstance(r, list) for w in r]

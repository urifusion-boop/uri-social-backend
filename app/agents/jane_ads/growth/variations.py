"""
Variations of a winning ad, added to the ad set it already runs in (PRD: scale what
works). The new ad copies the winner's routing from Meta, so it goes wherever the
winner goes, and changes only the words and the picture.
"""
from __future__ import annotations

from . import actions as act

MAX_ADS = 6


def _story_for(story: dict, headline: str, message: str, picture: str) -> dict:
    """The winner's story with new words and picture. A video winner becomes a photo ad
    sending people to the same place, because a variation is a generated image."""
    out = {k: v for k, v in story.items() if k not in ("link_data", "video_data")}
    link_data = story.get("link_data")
    if link_data:
        kept = {k: v for k, v in link_data.items() if k not in ("image_hash", "description")}
        out["link_data"] = {**kept, "message": message, "name": headline, "picture": picture}
        return out
    video = story.get("video_data") or {}
    cta = video.get("call_to_action") or {}
    link = (cta.get("value") or {}).get("link")
    if not link:
        raise ValueError("This ad's destination can't be copied to a photo ad.")
    out["link_data"] = {"message": message, "name": headline, "picture": picture,
                        "link": link, "call_to_action": cta}
    return out


async def read_source(adapter, ad_id: str) -> dict:
    """The winner as Meta holds it, plus the words it runs with."""
    fields = "adset_id,status,creative{object_story_spec,asset_feed_spec}"
    ad = await act._read(adapter, ad_id, fields)
    creative = ad.get("creative") or {}
    story = creative.get("object_story_spec") or {}
    body = story.get("link_data") or story.get("video_data") or {}
    return {"adset_id": ad.get("adset_id"), "status": ad.get("status") or "PAUSED", "story": story,
            "asset_feed_spec": creative.get("asset_feed_spec"),
            "headline": body.get("name") or body.get("title") or "", "text": body.get("message") or ""}


async def add_to_ad_set(adapter, source: dict, headline: str, text: str, picture: str, name: str) -> str:
    """Create the new ad beside the winner, in the same state. Returns its ad id."""
    account = f"act_{adapter._ad_account_id}"
    body = {"name": f"{name}-creative", "object_story_spec": _story_for(source["story"], headline, text, picture)}
    if source.get("asset_feed_spec"):
        body["asset_feed_spec"] = source["asset_feed_spec"]
    creative = await act._post(adapter, f"{account}/adcreatives", body)
    ad = await act._post(adapter, f"{account}/ads", {
        "name": name, "adset_id": source["adset_id"], "creative": {"creative_id": creative["id"]},
        "status": source["status"]})
    return ad["id"]

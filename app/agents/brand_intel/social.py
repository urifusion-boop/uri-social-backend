"""
"No website? Use your Instagram or Facebook page" (plan, WS1).

Reads the business's OWN profile through Meta's official Graph API, using the connection it
already made to Uri (no scraping): the Instagram bio, name, website and recent captions, or the
Facebook Page's about text, category, website, location and recent posts. The result has the
same shape as a website read, so the same extraction, review and apply steps follow.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from app.core.config import settings

from .safe_fetch import FetchError

SOURCES = ("instagram", "facebook")
_POSTS = 25


def _graph() -> str:
    return f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"


async def find_connection(db, user_id: str, brand_id: str, source: str) -> Optional[dict]:
    """This brand's own connected account of that kind, or one not tied to any brand — never another
    brand's (same rule publishing uses)."""
    if source == "instagram":
        base: dict[str, Any] = {"user_id": user_id, "platform": "instagram", "connection_status": "active",
                                "ig_user_id": {"$exists": True, "$ne": None},
                                "page_access_token": {"$exists": True, "$ne": None}}
    else:
        base = {"user_id": user_id, "platform": "facebook", "connection_status": "active",
                "page_id": {"$exists": True, "$ne": None}, "page_access_token": {"$exists": True, "$ne": None}}
    if brand_id:
        found = await db["social_connections"].find_one({**base, "brand_id": brand_id})
        if found:
            return found
        return await db["social_connections"].find_one({**base, "$or": [{"brand_id": {"$exists": False}},
                                                                          {"brand_id": None}]})
    return await db["social_connections"].find_one(base)


async def _get(client: httpx.AsyncClient, path: str, token: str, fields: str, **extra) -> dict:
    resp = await client.get(f"{_graph()}/{path}", params={"fields": fields, "access_token": token, **extra})
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400 or "error" in data:
        msg = ((data or {}).get("error") or {}).get("message", "")
        if "expired" in msg.lower() or "session" in msg.lower() or resp.status_code in (401, 403):
            raise FetchError("Your connection to that account has expired. Reconnect it in Connected Accounts, "
                             "then try again.", "reconnect")
        raise FetchError("We couldn't read that account just now. Try again shortly.", "graph_error")
    return data


async def read_profile(conn: dict, source: str) -> dict:
    """The profile as a 'site': {url, text, pages, page_list, socials, colors, logo_candidates, jsonld}."""
    token = conn["page_access_token"]
    async with httpx.AsyncClient(timeout=20) as client:
        if source == "instagram":
            ig = conn["ig_user_id"]
            p = await _get(client, ig, token, "username,name,biography,website,profile_picture_url")
            media = await _get(client, f"{ig}/media", token, "caption", limit=_POSTS)
            username = p.get("username") or conn.get("username") or ""
            url = f"https://instagram.com/{username}" if username else "https://instagram.com/"
            captions = [m.get("caption", "") for m in media.get("data") or [] if m.get("caption")]
            text = "\n".join(filter(None, [
                f"=== INSTAGRAM PROFILE: {url}", f"NAME: {p.get('name', '')}", f"BIO: {p.get('biography', '')}",
                f"WEBSITE: {p.get('website', '')}" if p.get("website") else "",
                "=== RECENT POSTS", *[f"- {c[:600]}" for c in captions],
            ]))
            socials = {"instagram": url}
            website = p.get("website") or ""
            picture = p.get("profile_picture_url") or ""
            name = p.get("name") or username
        else:
            page_id = conn["page_id"]
            p = await _get(client, page_id, token,
                           "name,about,description,category,website,single_line_address,phone,link,picture.type(large)")
            posts = await _get(client, f"{page_id}/posts", token, "message", limit=_POSTS)
            url = p.get("link") or f"https://facebook.com/{page_id}"
            messages = [m.get("message", "") for m in posts.get("data") or [] if m.get("message")]
            text = "\n".join(filter(None, [
                f"=== FACEBOOK PAGE: {url}", f"NAME: {p.get('name', '')}", f"CATEGORY: {p.get('category', '')}",
                f"ABOUT: {p.get('about', '')}", f"DESCRIPTION: {p.get('description', '')}",
                f"WEBSITE: {p.get('website', '')}" if p.get("website") else "",
                f"ADDRESS: {p.get('single_line_address', '')}" if p.get("single_line_address") else "",
                "=== RECENT POSTS", *[f"- {m[:600]}" for m in messages],
            ]))
            socials = {"facebook": url}
            website = (p.get("website") or "").split()[0] if p.get("website") else ""
            picture = (((p.get("picture") or {}).get("data") or {}).get("url")) or ""
            name = p.get("name") or ""
    if len(text) < 80:
        raise FetchError("That account doesn't say enough about the business yet to fill your Playbook.", "thin")
    return {"url": url, "requested": url, "text": text, "pages": [url], "page_list": [{"kind": source, "url": url,
                                                                                         "title": name}],
            "socials": socials, "colors": [], "logo_candidates": [picture] if picture else [],
            "jsonld": [{"name": name}] if name else [], "lang": "", "website_found": website}

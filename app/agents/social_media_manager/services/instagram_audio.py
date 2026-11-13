"""
Instagram Audio API — pick a licensed music track and attach it to a Reel.

Meta only supports this for Reels (a feed image, carousel or Story has no audio option in the API), and only on
the Facebook Login flavour of the Instagram API, which is how our Instagram connections are made.
Docs: https://developers.facebook.com/docs/instagram-platform/content-publishing/audio-api/

    search:  GET  /ig_audio?audio_type=music&user_id={ig-user-id}[&search_query=…]   (no query = trending)
    attach:  POST /{ig-user-id}/media  media_type=REELS&video_url=…
                  &audio_configuration={"audio_id":"…","audio_volume":1-100,"video_volume":1-100}

The chosen track is stored on the draft as `instagram_audio` (see `clean_choice`), so an immediate publish and a
scheduled one both pick it up.
"""
import json
import re
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings

# Meta's examples for this API use a newer Graph version than the rest of our Instagram calls (FACEBOOK_API_VERSION),
# so it gets its own setting. Only the track search and the Reel container that carries a track use it.
AUDIO_BASE = f"https://graph.facebook.com/{settings.INSTAGRAM_AUDIO_API_VERSION}"

_AUDIO_ID = re.compile(r"^\d{1,40}$")


class AudioSearchError(RuntimeError):
    """Instagram refused or failed the music search. The message is safe to show the customer."""


def clean_choice(audio: Any) -> Optional[Dict[str, Any]]:
    """A stored track choice, validated and normalised — or None if there is no usable choice.

    {audio_id, title, artist, audio_volume (1-100), video_volume (1-100)}. Volumes default to Meta's own default
    (full) and are clamped to its 1-100 range."""
    if not isinstance(audio, dict):
        return None
    audio_id = str(audio.get("audio_id") or "").strip()
    if not _AUDIO_ID.match(audio_id):
        return None

    def volume(key: str) -> int:
        try:
            return max(1, min(100, int(audio.get(key, 100))))
        except (TypeError, ValueError):
            return 100

    return {
        "audio_id": audio_id,
        "title": str(audio.get("title") or "")[:200],
        "artist": str(audio.get("artist") or "")[:200],
        "audio_volume": volume("audio_volume"),
        "video_volume": volume("video_volume"),
    }


def audio_configuration(audio: Any) -> Optional[str]:
    """The `audio_configuration` form value for a Reel container (a JSON object), or None for no music."""
    choice = clean_choice(audio)
    if not choice:
        return None
    return json.dumps({
        "audio_id": choice["audio_id"],
        "audio_volume": choice["audio_volume"],
        "video_volume": choice["video_volume"],
    })


def _track(raw: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "audio_id": str(raw.get("audio_id") or ""),
        "title": raw.get("title") or "",
        "artist": raw.get("display_artist") or "",
        "duration_seconds": round((raw.get("duration_in_ms") or 0) / 1000),
        "cover_url": raw.get("cover_artwork_thumbnail_uri") or raw.get("cover_artwork_thumbnail_url"),
        # A temporary link (about 1.5 days) to a preview of the track.
        "preview_url": raw.get("download_url") or raw.get("on_platform_audio_preview_link"),
    }


async def search_music(ig_user_id: str, access_token: str, query: Optional[str] = None) -> List[Dict[str, Any]]:
    """Licensed music for Reels, trending when `query` is empty. Raises AudioSearchError with a customer-safe message."""
    params = {"audio_type": "music", "user_id": ig_user_id, "access_token": access_token}
    if query and query.strip():
        params["search_query"] = query.strip()
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{AUDIO_BASE}/ig_audio", params=params)
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise AudioSearchError("Couldn't reach Instagram to load music. Please try again.") from e

    error = data.get("error") if isinstance(data, dict) else None
    if error or resp.status_code >= 400:
        detail = (error or {}).get("message") or f"HTTP {resp.status_code}"
        raise AudioSearchError(f"Instagram couldn't load music right now: {detail}")
    return [t for t in (_track(r) for r in (data.get("audio") or [])) if t["audio_id"]]

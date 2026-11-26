"""
Google video ads: a video for YouTube & Discover, Display and Everywhere.

Google Ads runs only videos that are on YouTube. A video the owner uploads to Uri goes to YouTube
through Google Ads itself (YouTubeVideoUploadService, Google Ads API v23.2+): it lands, unlisted, on
a Google-managed channel tied to the brand's own Google Ads account. Nobody connects a YouTube
channel, and uploads count per ad account, so one customer's uploads never hold up another's.

A new video takes a few minutes to process, longer than a request may run. So the upload starts as
soon as the plan is built (prepare), runs in the background, and the launch only uses it once Google
says it's PROCESSED (ensure_ready); until then the launch answers "getting it ready" and the page
tries again. A video already on YouTube (a pasted link) needs none of this.

Before any of that, the owner sees what Google will make of the video (check_upload, check_youtube):
its length against each placement's rules, its shape (upright videos also run in Shorts), sound,
and, for placements that still need a picture, stills taken from it.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

UPLOADS = "google_video_uploads"
# Google's own minimums (Demand Gen video specs; Performance Max video assets).
MIN_SECONDS = 5
IN_STREAM_SECONDS = 10
SHORTS_MAX_SECONDS = 60
# How long a background upload may take before the page is told it failed.
PROCESS_TIMEOUT = timedelta(minutes=25)
POLL_SECONDS = 10
READY_STATES = {"PROCESSED"}
FAILED_STATES = {"FAILED", "REJECTED", "UNAVAILABLE"}

PREPARING = ("Getting your video ready on YouTube. This usually takes a few minutes; "
             "Jane will launch it as soon as it's ready.")

_YT = re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:[^#]*&)?v=|shorts/|embed/|live/|v/))([A-Za-z0-9_-]{11})")
_BARE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class VideoNotReady(Exception):
    """The video is still being processed by YouTube; the launch should be tried again shortly."""


class VideoFailed(Exception):
    """YouTube couldn't take the video; the message says why, in the owner's words."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_indexes(db) -> None:
    await db[UPLOADS].create_index([("customer_id", 1), ("video_url", 1)])
    await db[UPLOADS].create_index("upload_id", unique=True)


# ── what Google will make of a video ────────────────────────────────────────────

def youtube_id(link: str) -> str:
    link = (link or "").strip()
    m = _YT.search(link)
    if m:
        return m.group(1)
    return link if _BARE.match(link) else ""


def thumbnail_for(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else ""


def facts_for(duration: float, width: int, height: int, has_audio: Optional[bool], placement: str) -> dict:
    """What this video means on Google, as lines the owner can read: ok, warn (it runs, with a
    limit) or block (it can't run here). `ok` is False when anything blocks."""
    facts: list[dict[str, str]] = []
    secs = int(round(duration or 0))
    if duration and duration < MIN_SECONDS:
        facts.append({"tone": "block", "text": f"{secs} seconds. Google needs at least {MIN_SECONDS}."})
    elif duration and placement == "pmax" and duration < IN_STREAM_SECONDS:
        facts.append({"tone": "block", "text": f"{secs} seconds. Everywhere needs at least {IN_STREAM_SECONDS}."})
    elif duration and duration < IN_STREAM_SECONDS:
        facts.append({"tone": "warn", "text": f"{secs} seconds. It runs in feeds and Shorts, not before other "
                                              f"YouTube videos (that needs {IN_STREAM_SECONDS})."})
    elif duration:
        facts.append({"tone": "ok", "text": f"{secs} seconds"})
    upright = bool(width and height and height > width * 1.15)
    sideways = bool(width and height and width > height * 1.15)
    if upright and duration and duration > SHORTS_MAX_SECONDS:
        facts.append({"tone": "warn", "text": "Upright, but over a minute, so it won't run in YouTube Shorts."})
    elif upright:
        facts.append({"tone": "ok", "text": "Upright, so it also runs in YouTube Shorts"})
    elif sideways:
        facts.append({"tone": "warn", "text": "Sideways, so it won't run in Shorts. Fine everywhere else."})
    elif width and height:
        facts.append({"tone": "ok", "text": "Square"})
    if has_audio is True:
        facts.append({"tone": "ok", "text": "Has sound"})
    elif has_audio is False:
        facts.append({"tone": "warn", "text": "No sound. It still runs, but YouTube ads do better with sound."})
    return {"ok": not any(f["tone"] == "block" for f in facts), "facts": facts, "upright": upright,
            "duration": duration or 0, "width": width or 0, "height": height or 0}


async def _run(*args: str, timeout: float = 45) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, b""
    return proc.returncode or 0, out


async def probe(video_url: str) -> dict:
    """Length, shape and sound of a hosted video (ffprobe reads only what it needs)."""
    code, out = await _run("ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                           "-show_format", video_url)
    if code != 0 or not out:
        raise ValueError("Jane couldn't read that video. Try uploading it again, as an MP4 or MOV.")
    data = json.loads(out)
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError("That file has no video in it. Upload an MP4 or MOV video.")
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    rotation = 0
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            rotation = int(abs(float(side["rotation"])))
    rotation = rotation or int(abs(float((video.get("tags") or {}).get("rotate") or 0)))
    if rotation % 180 == 90:
        width, height = height, width
    duration = float((data.get("format") or {}).get("duration") or video.get("duration") or 0)
    return {"duration": duration, "width": width, "height": height,
            "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


async def stills(video_url: str, duration: float, count: int = 3) -> list[str]:
    """`count` stills from across the video, hosted, for placements that still need a picture."""
    from .creative import _upload_bytes_to_cloudinary
    marks = [0.12, 0.45, 0.78][:count] if duration else [0.0]
    out: list[str] = []
    for mark in marks:
        code, jpg = await _run("ffmpeg", "-v", "error", "-ss", f"{max(0.0, duration * mark):.2f}", "-i", video_url,
                               "-frames:v", "1", "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
                               timeout=30)
        if code == 0 and jpg:
            url = await _upload_bytes_to_cloudinary(jpg, f"video-still-{uuid.uuid4().hex[:12]}",
                                                    resource_type="image", ext="jpg", content_type="image/jpeg")
            if url:
                out.append(url)
    return out


async def check_upload(video_url: str, placement: str) -> dict:
    meta = await probe(video_url)
    out = facts_for(meta["duration"], meta["width"], meta["height"], meta["has_audio"], placement)
    out["stills"] = await stills(video_url, meta["duration"]) if placement in ("display", "pmax") else []
    return out


async def check_youtube(link: str, placement: str) -> dict:
    """A video already on YouTube: found, viewable by Google (public or unlisted), and its length
    when YouTube shows it. A private or missing video can't run, said plainly."""
    vid = youtube_id(link)
    if not vid:
        raise ValueError("That doesn't look like a YouTube link. Copy it from the Share button on YouTube.")
    watch = f"https://www.youtube.com/watch?v={vid}"
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get("https://www.youtube.com/oembed", params={"url": watch, "format": "json"})
        if r.status_code in (401, 403):
            raise ValueError("That video is private. In YouTube Studio, set it to Unlisted or Public, "
                             "then check again.")
        if r.status_code == 404:
            raise ValueError("YouTube can't find that video. Check the link, or that it isn't private or deleted.")
        if r.status_code != 200:
            raise ValueError("YouTube didn't answer just now. Try again in a moment.")
        info = r.json()
        duration = 0.0
        try:
            page = await client.get(watch, headers={"Accept-Language": "en"})
            m = re.search(r'"lengthSeconds":"(\d+)"', page.text)
            duration = float(m.group(1)) if m else 0.0
        except Exception:
            pass
    shorts = "/shorts/" in link
    out = facts_for(duration, 9 if shorts else 0, 16 if shorts else 0, None, placement)
    if not shorts and not any("Upright" in f["text"] or "Sideways" in f["text"] for f in out["facts"]):
        out["facts"].append({"tone": "ok", "text": "Google decides whether it also runs in Shorts"})
    out["facts"].insert(0, {"tone": "ok", "text": "Found on YouTube"})
    out.update({"video_id": vid, "title": info.get("title") or "", "channel": info.get("author_name") or "",
                "thumbnail_url": thumbnail_for(vid), "stills": [thumbnail_for(vid)]})
    return out


# ── putting an uploaded video on YouTube, through Google Ads ────────────────────

async def prepare(db, adapter, video_url: str, title: str = "") -> dict:
    """Start (or reuse) the upload of this video to the brand's Google Ads account. Returns at
    once; the upload and YouTube's processing carry on in the background."""
    customer_id = adapter.customer_id
    doc = await db[UPLOADS].find_one({"customer_id": customer_id, "video_url": video_url,
                                      "status": {"$ne": "failed"}}, {"_id": 0})
    if doc and not _stale(doc):
        return doc
    doc = {"upload_id": f"gvu_{uuid.uuid4().hex[:16]}", "customer_id": customer_id, "video_url": video_url,
           "status": "uploading", "resource_name": "", "youtube_video_id": "", "error": "",
           "title": (title or "Ad video")[:100], "created_at": _now(), "updated_at": _now()}
    await db[UPLOADS].insert_one(dict(doc))
    asyncio.create_task(_upload_and_wait(db, adapter, doc))
    return doc


def _stale(doc: dict) -> bool:
    """An upload whose worker went away (a deploy) never finishes on its own: start again."""
    if doc.get("status") != "uploading":
        return False
    updated = doc.get("updated_at")
    if isinstance(updated, datetime) and updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return not isinstance(updated, datetime) or _now() - updated > timedelta(minutes=10)


async def _set(db, upload_id: str, **change: Any) -> None:
    await db[UPLOADS].update_one({"upload_id": upload_id}, {"$set": {**change, "updated_at": _now()}})


async def _upload_and_wait(db, adapter, doc: dict) -> None:
    uid = doc["upload_id"]
    try:
        resource = await adapter.upload_youtube_video(doc["video_url"], doc["title"])
    except Exception as e:
        print(f"[google_video] upload {uid} failed: {e!r}", flush=True)
        await _set(db, uid, status="failed", error="YouTube couldn't take that video. Try another file, "
                                                     "or paste a YouTube link instead.")
        return
    await _set(db, uid, status="processing", resource_name=resource)
    deadline = _now() + PROCESS_TIMEOUT
    while _now() < deadline:
        await asyncio.sleep(POLL_SECONDS)
        try:
            state, video_id = await adapter.youtube_upload_state(resource)
        except Exception as e:
            print(f"[google_video] status check {uid} failed: {e!r}", flush=True)
            continue
        if state in READY_STATES and video_id:
            await _set(db, uid, status="ready", youtube_video_id=video_id)
            return
        if state in FAILED_STATES:
            await _set(db, uid, status="failed", error=_failed_words(state))
            return
    # Still processing at the deadline: left as "processing"; the launch asks Google again.


def _failed_words(state: str) -> str:
    if state == "REJECTED":
        return "YouTube rejected that video. It may break YouTube's rules; try another video."
    return "YouTube couldn't process that video. Try another file, or paste a YouTube link instead."


async def ensure_ready(db, adapter, video_url: str, title: str = "") -> str:
    """The YouTube video id for this uploaded video, once YouTube has processed it. Raises
    VideoNotReady while it's still on its way (starting the upload if nothing started it),
    VideoFailed if YouTube refused it."""
    doc = await db[UPLOADS].find_one({"customer_id": adapter.customer_id, "video_url": video_url},
                                     {"_id": 0}, sort=[("created_at", -1)])
    if not doc or _stale(doc):
        await prepare(db, adapter, video_url, title)
        raise VideoNotReady(PREPARING)
    if doc["status"] == "ready" and doc.get("youtube_video_id"):
        return doc["youtube_video_id"]
    if doc["status"] == "failed":
        raise VideoFailed(doc.get("error") or _failed_words("FAILED"))
    if doc["status"] == "processing" and doc.get("resource_name"):
        # Ask Google directly too: the worker that was polling may have gone (a deploy).
        try:
            state, video_id = await adapter.youtube_upload_state(doc["resource_name"])
        except Exception:
            state, video_id = "", ""
        if state in READY_STATES and video_id:
            await _set(db, doc["upload_id"], status="ready", youtube_video_id=video_id)
            return video_id
        if state in FAILED_STATES:
            await _set(db, doc["upload_id"], status="failed", error=_failed_words(state))
            raise VideoFailed(_failed_words(state))
    raise VideoNotReady(PREPARING)

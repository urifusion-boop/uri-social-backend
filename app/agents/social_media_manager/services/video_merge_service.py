import asyncio
import json
import os
import subprocess
import tempfile
from typing import Dict, List, Optional

import httpx

from app.utils.s3_upload import upload_bytes
from . import video_logo_overlay as logo_overlay
from . import video_text_overlay as overlay

# Canonical shape every clip gets normalized to before concatenation. The
# outcome-routed fal.ai models in this pipeline do NOT return uniform clips:
# the talking-avatar model is fixed at 480p (video_generation_service.py)
# while most others run 720p, and several models (Kling 2.5 Standard, the two
# MiniMax H3 models, Wan 3.0) have no audio toggle at all — meaning some clips
# have no audio track while others always do. The old concat used `-c copy`
# (pure stream copy, no re-encode), which requires every input to already
# share codec/resolution/fps/audio layout. Live-confirmed 2026-10-05: merging
# a 480p avatar clip (with audio) and a 720p Veo fallback clip (with audio)
# produced visible playback glitches — exactly the failure mode `-c copy`
# concat is known for on mismatched inputs. 1080x1920/30fps is a standard,
# widely-supported target for 9:16 vertical social video.
_TARGET_WIDTH = 1080
_TARGET_HEIGHT = 1920
_TARGET_FPS = 30


_DOWNLOAD_ATTEMPTS = 3

# Faster than ffmpeg's default (medium) at a visibly equal result for this content. The merge runs inside the web
# request, so every second it saves is a second off the window where a deploy, a worker restart or a proxy timeout
# can cut the request off. A high quality (low crf) keeps small text crisp.
_ENCODE_PRESET = "veryfast"
_NORMALIZE_CRF = "18"


class VideoMergeError(Exception):
    """A merge that failed for a reason worth telling the person (a clip that could not be fetched, etc.)."""


class VideoMergeService:

    @staticmethod
    async def _fetch(client: "httpx.AsyncClient", url: str, what: str) -> bytes:
        """The bytes at `url`, tried a few times: a dropped connection or a slow CDN is normal, an error page is not a video."""
        last: Optional[Exception] = None
        for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
            try:
                response = await client.get(url)
                response.raise_for_status()
                if not response.content:
                    raise ValueError("empty response")
                return response.content
            except Exception as e:
                last = e
                print(f"[VideoMerge] {what}: download attempt {attempt}/{_DOWNLOAD_ATTEMPTS} failed: {e!r}", flush=True)
                if attempt < _DOWNLOAD_ATTEMPTS:
                    await asyncio.sleep(attempt)
        raise VideoMergeError(f"{what} could not be downloaded ({type(last).__name__})")

    @staticmethod
    async def merge_clips(clip_urls: List[str], overlay_texts: Optional[List[Optional[str]]] = None,
                          logo: Optional[Dict[str, str]] = None) -> str:
        """Download clips, normalize to one common format, concatenate, upload. Returns Cloudinary URL.
        overlay_texts, if given, is one on-screen caption (or None) per clip, drawn sharply onto that clip.
        logo, if given ({"url", "position", "size"} from the Brand Playbook), is laid over the whole video."""
        async with httpx.AsyncClient(timeout=120) as client:
            clip_bytes_list = await asyncio.gather(
                *[VideoMergeService._fetch(client, url, f"clip {i + 1}") for i, url in enumerate(clip_urls)])
            logo_bytes = None
            if logo:
                try:
                    logo_bytes = await VideoMergeService._fetch(client, logo["url"], "the logo")
                except Exception as e:
                    # Never lose the video over its logo.
                    print(f"[VideoMerge] logo could not be downloaded, merging without it: {e}", flush=True)

        loop = asyncio.get_running_loop()
        merged_bytes = await loop.run_in_executor(
            None,
            lambda: VideoMergeService._normalize_and_concat(
                clip_bytes_list, overlay_texts, logo_bytes, (logo or {}).get("position", logo_overlay.DEFAULT_POSITION),
                (logo or {}).get("size", logo_overlay.DEFAULT_SIZE)),
        )

        return await upload_bytes(
            merged_bytes,
            folder="uri-social/merged-videos",
            resource_type="video",
        )

    @staticmethod
    def _has_audio_stream(path: str) -> bool:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type",
             "-of", "json", path],
            capture_output=True, text=True,
        )
        try:
            streams = json.loads(result.stdout or "{}").get("streams", [])
        except json.JSONDecodeError:
            streams = []
        return len(streams) > 0

    @staticmethod
    def _duration_seconds(path: str) -> Optional[float]:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True, text=True,
        )
        try:
            return float((result.stdout or "").strip())
        except ValueError:
            return None

    @staticmethod
    def _stamp_logo(video_path: str, logo_bytes: bytes, position: str, size: str, tmp: str) -> Optional[str]:
        """The video with the logo over it, or None if ffmpeg could not do it (e.g. the logo is not an image)."""
        logo_path = os.path.join(tmp, "logo.img")
        with open(logo_path, "wb") as f:
            f.write(logo_bytes)
        out = os.path.join(tmp, "merged_logo.mp4")
        cmd = [
            "ffmpeg", "-y", "-i", video_path, "-i", logo_path,
            "-filter_complex", logo_overlay.logo_filter(position, size, _TARGET_WIDTH, _TARGET_HEIGHT),
            "-map", "[vout]", "-map", "0:a?",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", _ENCODE_PRESET, "-crf", "18",
            "-c:a", "copy", "-movflags", "+faststart", out,
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            return out
        except subprocess.CalledProcessError as e:
            print(f"[VideoMerge] logo overlay failed, merging without it: "
                  f"{(e.stderr or b'').decode(errors='replace')[-300:]}", flush=True)
            return None

    @staticmethod
    def _normalize_and_concat(clips: List[bytes], overlay_texts: Optional[List[Optional[str]]] = None,
                              logo_bytes: Optional[bytes] = None, logo_position: str = logo_overlay.DEFAULT_POSITION,
                              logo_size: str = logo_overlay.DEFAULT_SIZE) -> bytes:
        with tempfile.TemporaryDirectory() as tmp:
            raw_paths = []
            for i, data in enumerate(clips):
                p = os.path.join(tmp, f"raw_{i}.mp4")
                with open(p, "wb") as f:
                    f.write(data)
                raw_paths.append(p)

            vf = (
                f"scale={_TARGET_WIDTH}:{_TARGET_HEIGHT}:force_original_aspect_ratio=decrease,"
                f"pad={_TARGET_WIDTH}:{_TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
                f"setsar=1"
            )

            normalized_paths = []
            for i, raw_path in enumerate(raw_paths):
                norm_path = os.path.join(tmp, f"norm_{i}.mp4")
                has_audio = VideoMergeService._has_audio_stream(raw_path)

                def normalize(video_filter: str) -> None:
                    cmd = ["ffmpeg", "-y", "-i", raw_path]
                    if has_audio:
                        cmd += [
                            "-vf", video_filter, "-r", str(_TARGET_FPS),
                            "-map", "0:v:0", "-map", "0:a:0",
                        ]
                    else:
                        # Synthesize a silent track so every normalized clip has
                        # an audio stream — concat with some clips silent and some
                        # not is its own source of desync/glitching.
                        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
                        cmd += [
                            "-vf", video_filter, "-r", str(_TARGET_FPS),
                            "-map", "0:v:0", "-map", "1:a:0", "-shortest",
                        ]
                    cmd += [
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", _ENCODE_PRESET, "-crf", _NORMALIZE_CRF,
                        "-c:a", "aac", "-ar", "44100", "-ac", "2",
                        norm_path,
                    ]
                    subprocess.run(cmd, check=True, capture_output=True)

                # On-screen text is drawn AFTER the scale to 1080x1920 so it is crisp at the final size.
                wrapped, fontsize = overlay.wrap_overlay_text(
                    overlay_texts[i] if overlay_texts and i < len(overlay_texts) else "", _TARGET_WIDTH)
                if wrapped:
                    text_path = os.path.join(tmp, f"text_{i}.txt")
                    with open(text_path, "w", encoding="utf-8") as tf:
                        tf.write(wrapped)
                    drawtext = overlay.drawtext_filter(text_path, fontsize, VideoMergeService._duration_seconds(raw_path))
                    try:
                        normalize(f"{vf},{drawtext}")
                    except subprocess.CalledProcessError as e:
                        # Never lose the clip over its caption (e.g. the font is missing on this host).
                        print(f"[VideoMerge] clip {i}: text overlay failed, merging without it: "
                              f"{(e.stderr or b'').decode(errors='replace')[-300:]}", flush=True)
                        normalize(vf)
                else:
                    normalize(vf)
                normalized_paths.append(norm_path)

            list_path = os.path.join(tmp, "list.txt")
            with open(list_path, "w") as f:
                for p in normalized_paths:
                    f.write(f"file '{p}'\n")

            # Inputs are now uniform by construction, so a fast stream-copy
            # concat is safe and correct here — the normalization pass above
            # is what does the real work.
            out_path = os.path.join(tmp, "merged.mp4")
            subprocess.run(
                ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", out_path],
                check=True,
                capture_output=True,
            )

            if logo_bytes:
                with_logo = VideoMergeService._stamp_logo(out_path, logo_bytes, logo_position, logo_size, tmp)
                if with_logo:
                    out_path = with_logo

            with open(out_path, "rb") as f:
                return f.read()

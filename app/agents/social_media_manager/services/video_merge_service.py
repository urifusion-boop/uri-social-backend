import asyncio
import json
import os
import subprocess
import tempfile
from typing import List

import httpx

from app.utils.s3_upload import upload_bytes

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


class VideoMergeService:

    @staticmethod
    async def merge_clips(clip_urls: List[str]) -> str:
        """Download clips, normalize to one common format, concatenate, upload. Returns Cloudinary URL."""
        async with httpx.AsyncClient(timeout=120) as client:
            responses = await asyncio.gather(*[client.get(url) for url in clip_urls])

        clip_bytes_list = [r.content for r in responses]

        loop = asyncio.get_running_loop()
        merged_bytes = await loop.run_in_executor(
            None,
            lambda: VideoMergeService._normalize_and_concat(clip_bytes_list),
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
    def _normalize_and_concat(clips: List[bytes]) -> bytes:
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

                cmd = ["ffmpeg", "-y", "-i", raw_path]
                if has_audio:
                    cmd += [
                        "-vf", vf, "-r", str(_TARGET_FPS),
                        "-map", "0:v:0", "-map", "0:a:0",
                    ]
                else:
                    # Synthesize a silent track so every normalized clip has
                    # an audio stream — concat with some clips silent and some
                    # not is its own source of desync/glitching.
                    cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
                    cmd += [
                        "-vf", vf, "-r", str(_TARGET_FPS),
                        "-map", "0:v:0", "-map", "1:a:0", "-shortest",
                    ]
                cmd += [
                    "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-ar", "44100", "-ac", "2",
                    norm_path,
                ]
                subprocess.run(cmd, check=True, capture_output=True)
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

            with open(out_path, "rb") as f:
                return f.read()

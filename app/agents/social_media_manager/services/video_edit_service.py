import asyncio
import json
import os
import subprocess
import tempfile
import uuid
from datetime import datetime

from app.database import get_db
from app.utils.cloudinary_upload import upload_bytes

PLATFORM_TARGETS = {
    "instagram_reels": {"duration": 15, "width": 1080, "height": 1920},
    "tiktok":          {"duration": 20, "width": 1080, "height": 1920},
    "facebook_reels":  {"duration": 15, "width": 1080, "height": 1920},
}


class VideoEditService:

    @staticmethod
    async def create_job(user_id: str, original_video_url: str, platform: str, enhancements: dict) -> str:
        job_id = uuid.uuid4().hex
        db = get_db()
        await db["video_edit_jobs"].insert_one({
            "job_id": job_id,
            "user_id": user_id,
            "status": "processing",
            "progress": 0,
            "original_video_url": original_video_url,
            "platform": platform,
            "enhancements": enhancements,
            "edited_video_url": None,
            "edits_applied": [],
            "draft_id": None,
            "error": None,
            "created_at": datetime.utcnow(),
        })
        return job_id

    @staticmethod
    async def run_job(job_id: str, user_id: str, video_bytes: bytes, platform: str, enhancements: dict):
        db = get_db()
        col = db["video_edit_jobs"]

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: VideoEditService._run_ffmpeg_pipeline(video_bytes, platform, enhancements),
            )
            edited_bytes: bytes = result["bytes"]
            edits_applied: list = result["edits_applied"]

            edited_url = await upload_bytes(
                edited_bytes,
                folder="uri-social/edited-reels",
                resource_type="video",
            )

            draft_id = uuid.uuid4().hex
            platform_key = platform.replace("_reels", "")
            await db["content_drafts"].insert_one({
                "id": draft_id,
                "draft_id": draft_id,
                "user_id": user_id,
                "platform": platform_key,
                "post_type": "reel",
                "video_url": edited_url,
                "content": "",
                "status": "draft",
                "has_image": False,
                "auto_generated": False,
                "created_at": datetime.utcnow(),
            })

            await col.update_one({"job_id": job_id}, {"$set": {
                "status": "complete",
                "progress": 100,
                "edited_video_url": edited_url,
                "edits_applied": edits_applied,
                "draft_id": draft_id,
            }})

        except Exception as e:
            import traceback
            traceback.print_exc()
            await col.update_one({"job_id": job_id}, {"$set": {
                "status": "failed",
                "error": str(e),
            }})

    # ─────────────────────────────────────────────────────────────────────────
    # FFmpeg pipeline
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _run_ffmpeg_pipeline(video_bytes: bytes, platform: str, enhancements: dict) -> dict:
        target = PLATFORM_TARGETS.get(platform, PLATFORM_TARGETS["instagram_reels"])
        target_duration = target["duration"]
        target_w, target_h = target["width"], target["height"]

        edits_applied = []

        with tempfile.TemporaryDirectory() as tmp:
            input_path = os.path.join(tmp, "input.mp4")
            output_path = os.path.join(tmp, "output.mp4")

            with open(input_path, "wb") as f:
                f.write(video_bytes)

            meta = VideoEditService._get_metadata(input_path)
            duration = meta.get("duration", 30.0)
            width = meta.get("width", 1920)
            height = meta.get("height", 1080)
            has_audio = meta.get("has_audio", False)

            trim_duration = min(float(duration), float(target_duration))
            filters = []

            # Crop to 9:16
            if enhancements.get("crop_916", True):
                input_ratio = width / height if height > 0 else 1.78
                target_ratio = 9 / 16
                if abs(input_ratio - target_ratio) > 0.05:
                    if input_ratio > target_ratio:
                        new_w = int(height * 9 / 16)
                        crop_x = int((width - new_w) / 2)
                        filters.append(f"crop={new_w}:{height}:{crop_x}:0")
                    else:
                        new_h = int(width * 16 / 9)
                        crop_y = int((height - new_h) / 2)
                        filters.append(f"crop={width}:{new_h}:0:{crop_y}")
                    edits_applied.append("Cropped to 9:16")

            # Scale to target resolution with letterbox padding
            filters.append(f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease")
            filters.append(f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black")

            # Colour grade (warm eq)
            if enhancements.get("colour_grade", True):
                filters.append("eq=brightness=0.03:saturation=1.15:gamma_r=1.05:gamma_b=0.95")
                edits_applied.append("Colour graded")

            # Text overlays
            if enhancements.get("add_text_overlays", True):
                headline = (enhancements.get("headline_text") or "").strip()
                cta = (enhancements.get("cta_text") or "").strip()
                font = VideoEditService._find_font()
                if font and (headline or cta):
                    if headline:
                        safe = headline.replace("'", "\\'").replace(":", "\\:").replace("\\", "\\\\")
                        show_until = min(trim_duration, 4.0)
                        filters.append(
                            f"drawtext=text='{safe}':fontfile='{font}':"
                            f"fontsize=52:fontcolor=white:x=(w-text_w)/2:y=h*0.12:"
                            f"box=1:boxcolor=black@0.45:boxborderw=10:"
                            f"enable='between(t,0,{show_until:.1f})'"
                        )
                    if cta:
                        safe = cta.replace("'", "\\'").replace(":", "\\:").replace("\\", "\\\\")
                        cta_start = max(trim_duration - 4.0, trim_duration * 0.7)
                        filters.append(
                            f"drawtext=text='{safe}':fontfile='{font}':"
                            f"fontsize=36:fontcolor=white:x=(w-text_w)/2:y=h*0.85:"
                            f"box=1:boxcolor=black@0.45:boxborderw=8:"
                            f"enable='between(t,{cta_start:.1f},{trim_duration:.1f})'"
                        )
                    edits_applied.append("Text overlays added")

            # Build and run FFmpeg command
            vf = ",".join(filters) if filters else None
            cmd = ["ffmpeg", "-y", "-i", input_path, "-t", str(trim_duration)]
            if vf:
                cmd += ["-vf", vf]
            cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "23", "-r", "30", "-movflags", "+faststart"]
            if has_audio:
                cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
            else:
                cmd += ["-an"]
            cmd.append(output_path)

            proc = subprocess.run(cmd, capture_output=True, timeout=180)
            if proc.returncode != 0:
                raise RuntimeError(f"FFmpeg error: {proc.stderr.decode()[-800:]}")

            if enhancements.get("smart_trim", True):
                edits_applied.append(f"Trimmed to {int(trim_duration)}s")
            edits_applied.append("Exported 1080×1920 H.264")

            with open(output_path, "rb") as f:
                return {"bytes": f.read(), "edits_applied": edits_applied}

    @staticmethod
    def _get_metadata(path: str) -> dict:
        try:
            proc = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", path],
                capture_output=True, timeout=30,
            )
            if proc.returncode != 0:
                return {"duration": 15.0, "width": 1920, "height": 1080, "has_audio": False}
            data = json.loads(proc.stdout)
            video_s = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
            audio_s = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
            duration = float(data.get("format", {}).get("duration", 15.0))
            width = int(video_s.get("width", 1920)) if video_s else 1920
            height = int(video_s.get("height", 1080)) if video_s else 1080
            return {"duration": duration, "width": width, "height": height, "has_audio": audio_s is not None}
        except Exception:
            return {"duration": 15.0, "width": 1920, "height": 1080, "has_audio": False}

    @staticmethod
    def _find_font() -> str | None:
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
        for p in candidates:
            if os.path.exists(p):
                return p
        return None

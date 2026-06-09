import asyncio
import glob
import json
import os
import random
import subprocess
import tempfile
import uuid
from datetime import datetime

import httpx

from app.core.config import settings
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
    async def run_job(
        job_id: str,
        user_id: str,
        video_bytes: bytes,
        platform: str,
        enhancements: dict,
        brand_name: str = "",
        brand_cta: str = "",
        brand_colors: list = None,
        logo_url: str = "",
        logo_position: str = "bottom_right",
        tagline: str = "",
    ):
        db = get_db()
        col = db["video_edit_jobs"]

        # Download logo bytes async before entering the executor
        logo_bytes: bytes | None = None
        if logo_url:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(logo_url)
                    if r.status_code == 200:
                        logo_bytes = r.content
            except Exception:
                pass

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: VideoEditService._run_ffmpeg_pipeline(
                    video_bytes, platform, enhancements,
                    brand_name, brand_cta,
                    brand_colors or [], logo_bytes, logo_position, tagline,
                ),
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
                "request_id": draft_id,
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
    # Main FFmpeg pipeline
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _run_ffmpeg_pipeline(
        video_bytes: bytes,
        platform: str,
        enhancements: dict,
        brand_name: str,
        brand_cta: str,
        brand_colors: list = None,
        logo_bytes: bytes | None = None,
        logo_position: str = "bottom_right",
        tagline: str = "",
    ) -> dict:
        target = PLATFORM_TARGETS.get(platform, PLATFORM_TARGETS["instagram_reels"])
        target_duration = target["duration"]
        target_w, target_h = target["width"], target["height"]
        edits_applied = []

        with tempfile.TemporaryDirectory() as tmp:
            input_path = os.path.join(tmp, "input.mp4")
            with open(input_path, "wb") as f:
                f.write(video_bytes)

            meta = VideoEditService._get_metadata(input_path)
            duration   = meta.get("duration", 30.0)
            width      = meta.get("width", 1920)
            height     = meta.get("height", 1080)
            has_audio  = meta.get("has_audio", False)
            trim_dur   = min(float(duration), float(target_duration))

            # ── Step 1: stabilise ─────────────────────────────────────────
            working_path = input_path
            if enhancements.get("stabilise", True):
                stabilised = VideoEditService._stabilise(input_path, tmp, trim_dur, has_audio)
                if stabilised:
                    working_path = stabilised
                    edits_applied.append("Stabilised")

            # ── Step 2-5: crop → scale → colour → text ────────────────────
            filters = []

            if enhancements.get("crop_916", True):
                input_ratio  = width / height if height > 0 else 1.78
                target_ratio = 9 / 16
                if abs(input_ratio - target_ratio) > 0.05:
                    if input_ratio > target_ratio:
                        new_w  = int(height * 9 / 16)
                        crop_x = int((width - new_w) / 2)
                        filters.append(f"crop={new_w}:{height}:{crop_x}:0")
                    else:
                        new_h  = int(width * 16 / 9)
                        crop_y = int((height - new_h) / 2)
                        filters.append(f"crop={width}:{new_h}:0:{crop_y}")
                    edits_applied.append("Cropped to 9:16")

            filters.append(f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease")
            filters.append(f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black")

            if enhancements.get("colour_grade", True):
                filters.append("eq=brightness=0.03:saturation=1.15:gamma_r=1.05:gamma_b=0.95")
                edits_applied.append("Colour graded")

            if enhancements.get("add_text_overlays", True):
                headline = (enhancements.get("headline_text") or "").strip()
                cta_text = (enhancements.get("cta_text") or "").strip()
                font     = VideoEditService._find_font()
                if font and (headline or cta_text):
                    if headline:
                        safe = headline.replace("'", "\\'").replace(":", "\\:")
                        show_until = min(trim_dur, 4.0)
                        filters.append(
                            f"drawtext=text='{safe}':fontfile='{font}':"
                            f"fontsize=52:fontcolor=white:x=(w-text_w)/2:y=h*0.12:"
                            f"box=1:boxcolor=black@0.45:boxborderw=10:"
                            f"enable='between(t,0,{show_until:.1f})'"
                        )
                    if cta_text:
                        safe      = cta_text.replace("'", "\\'").replace(":", "\\:")
                        cta_start = max(trim_dur - 4.0, trim_dur * 0.7)
                        filters.append(
                            f"drawtext=text='{safe}':fontfile='{font}':"
                            f"fontsize=36:fontcolor=white:x=(w-text_w)/2:y=h*0.85:"
                            f"box=1:boxcolor=black@0.45:boxborderw=8:"
                            f"enable='between(t,{cta_start:.1f},{trim_dur:.1f})'"
                        )
                    edits_applied.append("Text overlays added")

            # ── Step 6: export main clip ───────────────────────────────────
            # Always include an audio track (silent if source has none) so
            # all parts share the same stream layout for concat.
            main_path = os.path.join(tmp, "main.mp4")
            vf = ",".join(filters) if filters else None
            if has_audio:
                cmd = ["ffmpeg", "-y", "-i", working_path, "-t", str(trim_dur)]
                if vf:
                    cmd += ["-vf", vf]
                cmd += [
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-r", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
                    main_path,
                ]
            else:
                # No source audio — mix in a silent track so concat works cleanly
                cmd = [
                    "ffmpeg", "-y",
                    "-i", working_path,
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-t", str(trim_dur),
                ]
                if vf:
                    cmd += ["-vf", vf]
                cmd += [
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-r", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
                    "-shortest", main_path,
                ]
            VideoEditService._run(cmd)
            # All parts now have audio — treat as has_audio for concat
            has_audio = True

            if enhancements.get("smart_trim", True):
                edits_applied.append(f"Trimmed to {int(trim_dur)}s")

            # ── Step 7: logo overlay on the main clip ─────────────────────
            primary_color = (brand_colors or ["#000000"])[0] if brand_colors else "#000000"
            logo_path = None
            if logo_bytes:
                logo_path = os.path.join(tmp, "logo.png")
                with open(logo_path, "wb") as f:
                    f.write(logo_bytes)

            if logo_path:
                logo_main = VideoEditService._apply_logo(main_path, logo_path, logo_position, tmp, "logo_main.mp4")
                if logo_main:
                    main_path = logo_main
                    edits_applied.append("Logo overlaid")

            # ── Step 8: branded intro ─────────────────────────────────────
            intro_path = None
            if enhancements.get("add_intro", True):
                intro_path = VideoEditService._make_intro(
                    tmp, brand_name, tagline, primary_color, logo_path, target_w, target_h
                )
                if intro_path:
                    edits_applied.append("Branded intro added")

            # ── Step 9: branded outro ─────────────────────────────────────
            outro_path = None
            if enhancements.get("add_outro", True):
                cta_line   = brand_cta or enhancements.get("cta_text") or "Follow us for more"
                outro_path = VideoEditService._make_outro(tmp, cta_line, primary_color, target_w, target_h)
                if outro_path:
                    edits_applied.append("Branded outro added")

            # ── Step 10: concatenate intro + main + outro ──────────────────
            assembled_path = main_path
            parts = [p for p in [intro_path, main_path, outro_path] if p]
            if len(parts) > 1:
                assembled_path = VideoEditService._concat(parts, tmp, has_audio)

            # ── Step 11: background music ──────────────────────────────────
            final_path = assembled_path
            if enhancements.get("add_music", True):
                mood        = enhancements.get("music_mood", "upbeat")
                music_track = VideoEditService._find_music_track(mood)
                if music_track:
                    final_path = VideoEditService._mix_music(assembled_path, music_track, tmp, has_audio)
                    edits_applied.append(f"Background music added ({mood})")

            edits_applied.append("Exported 1080×1920 H.264")

            with open(final_path, "rb") as f:
                return {"bytes": f.read(), "edits_applied": edits_applied}

    # ─────────────────────────────────────────────────────────────────────────
    # Stabilisation
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _stabilise(input_path: str, tmp: str, duration: float, has_audio: bool) -> str | None:
        """Two-pass vidstab with fallback to deshake. Handles audio/no-audio."""
        stabilised = os.path.join(tmp, "stabilised.mp4")
        audio_args = ["-c:a", "aac", "-b:a", "128k"] if has_audio else ["-an"]

        # Try vidstab (requires libvidstab compiled into ffmpeg)
        transforms = os.path.join(tmp, "transforms.trf")
        try:
            VideoEditService._run([
                "ffmpeg", "-y", "-i", input_path, "-t", str(duration),
                "-vf", f"vidstabdetect=shakiness=5:accuracy=15:result={transforms}",
                "-f", "null", "-",
            ])
            VideoEditService._run([
                "ffmpeg", "-y", "-i", input_path, "-t", str(duration),
                "-vf", f"vidstabtransform=smoothing=10:crop=black:zoom=1:input={transforms}",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                *audio_args, "-movflags", "+faststart", stabilised,
            ])
            return stabilised
        except subprocess.CalledProcessError:
            pass

        # Fallback: deshake (always available in standard ffmpeg builds)
        try:
            VideoEditService._run([
                "ffmpeg", "-y", "-i", input_path, "-t", str(duration),
                "-vf", "deshake",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                *audio_args, "-movflags", "+faststart", stabilised,
            ])
            return stabilised
        except subprocess.CalledProcessError:
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Intro / Outro generation (programmatic — no asset files required)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _apply_logo(
        video_path: str,
        logo_path: str,
        position: str,
        tmp: str,
        out_name: str = "with_logo.mp4",
    ) -> str | None:
        """Overlay a scaled logo in a corner of the video throughout."""
        out = os.path.join(tmp, out_name)
        # Scale logo to 10% of video width, preserve aspect ratio
        logo_w = 108  # 10% of 1080
        POSITIONS = {
            "top_left":     f"10:10",
            "top_right":    f"W-w-10:10",
            "bottom_left":  f"10:H-h-10",
            "bottom_right": f"W-w-10:H-h-10",
        }
        overlay_xy = POSITIONS.get(position, POSITIONS["bottom_right"])
        fc = (
            f"[1:v]scale={logo_w}:-1,format=rgba,"
            f"colorchannelmixer=aa=0.85[logo];"
            f"[0:v][logo]overlay={overlay_xy}:format=auto[vout]"
        )
        try:
            VideoEditService._run([
                "ffmpeg", "-y", "-i", video_path, "-i", logo_path,
                "-filter_complex", fc,
                "-map", "[vout]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "copy", "-movflags", "+faststart", out,
            ])
            return out
        except subprocess.CalledProcessError:
            return None

    @staticmethod
    def _make_intro(
        tmp: str,
        brand_name: str,
        tagline: str,
        bg_color: str,
        logo_path: str | None,
        w: int,
        h: int,
    ) -> str | None:
        """1.5s branded intro — brand color background, logo (if available), brand name + tagline."""
        font = VideoEditService._find_font()
        if not font:
            return None
        out = os.path.join(tmp, "intro.mp4")
        safe_color = bg_color.lstrip("#") if bg_color else "000000"
        name_label = (brand_name or "URI Social").replace("'", "\\'").replace(":", "\\:")
        tag_label  = (tagline or "").replace("'", "\\'").replace(":", "\\:")

        # Build filter chain on the color source
        filters = [
            f"drawtext=text='{name_label}':fontfile='{font}':"
            f"fontsize=64:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2-30:"
            f"alpha='if(lt(t,0.4),t/0.4,if(gt(t,1.1),(1.5-t)/0.4,1))'"
        ]
        if tag_label:
            filters.append(
                f"drawtext=text='{tag_label}':fontfile='{font}':"
                f"fontsize=32:fontcolor=white@0.8:x=(w-text_w)/2:y=(h-text_h)/2+50:"
                f"alpha='if(lt(t,0.4),t/0.4,if(gt(t,1.1),(1.5-t)/0.4,1))'"
            )

        try:
            # Step 1: generate color + text clip
            base_out = os.path.join(tmp, "intro_base.mp4")
            VideoEditService._run([
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", f"color=c=0x{safe_color}:s={w}x{h}:r=30:d=1.5",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-vf", ",".join(filters),
                "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-shortest", base_out,
            ])

            # Step 2: overlay logo if available
            if logo_path:
                logo_out = VideoEditService._apply_logo(base_out, logo_path, "top_right", tmp, "intro_logo.mp4")
                if logo_out:
                    base_out = logo_out

            # Copy to final name
            VideoEditService._run(["ffmpeg", "-y", "-i", base_out, "-c", "copy", out])
            return out
        except subprocess.CalledProcessError:
            return None

    @staticmethod
    def _make_outro(
        tmp: str,
        cta_text: str,
        bg_color: str,
        w: int,
        h: int,
    ) -> str | None:
        """2s branded outro — brand color background with CTA text."""
        font = VideoEditService._find_font()
        if not font:
            return None
        out = os.path.join(tmp, "outro.mp4")
        safe_color = bg_color.lstrip("#") if bg_color else "000000"
        label = (cta_text or "Follow us for more").replace("'", "\\'").replace(":", "\\:")
        vf = (
            f"drawtext=text='{label}':fontfile='{font}':"
            f"fontsize=48:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2:"
            f"alpha='if(lt(t,0.3),t/0.3,if(gt(t,1.7),(2-t)/0.3,1))'"
        )
        try:
            VideoEditService._run([
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", f"color=c=0x{safe_color}:s={w}x{h}:r=30:d=2.0",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-vf", vf,
                "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-shortest", out,
            ])
            return out
        except subprocess.CalledProcessError:
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Concatenation — all parts must have video+audio (intro/outro carry silent audio)
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _concat(parts: list[str], tmp: str, has_audio: bool) -> str:
        """Concat parts using filter_complex so codec/stream mismatches are always resolved."""
        out = os.path.join(tmp, "assembled.mp4")
        n = len(parts)

        # Build -i args and filter_complex concat
        input_args = []
        for p in parts:
            input_args += ["-i", p]

        if has_audio:
            streams = "".join(f"[{i}:v][{i}:a]" for i in range(n))
            fc = f"{streams}concat=n={n}:v=1:a=1[vout][aout]"
            VideoEditService._run([
                "ffmpeg", "-y", *input_args,
                "-filter_complex", fc,
                "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out,
            ])
        else:
            streams = "".join(f"[{i}:v]" for i in range(n))
            fc = f"{streams}concat=n={n}:v=1:a=0[vout]"
            VideoEditService._run([
                "ffmpeg", "-y", *input_args,
                "-filter_complex", fc,
                "-map", "[vout]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-an", "-movflags", "+faststart", out,
            ])
        return out

    # ─────────────────────────────────────────────────────────────────────────
    # Background music mixing
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _find_music_track(mood: str) -> str | None:
        """Look for a track in MUSIC_LIBRARY_PATH/{mood}/*.mp3 (or .wav/m4a)."""
        base = (settings.MUSIC_LIBRARY_PATH or "").strip()
        if not base:
            return None
        mood_dir = os.path.join(base, mood)
        tracks = (
            glob.glob(os.path.join(mood_dir, "*.mp3")) +
            glob.glob(os.path.join(mood_dir, "*.wav")) +
            glob.glob(os.path.join(mood_dir, "*.m4a"))
        )
        if not tracks:
            # Try root of music library as fallback
            tracks = (
                glob.glob(os.path.join(base, "*.mp3")) +
                glob.glob(os.path.join(base, "*.wav"))
            )
        return random.choice(tracks) if tracks else None

    @staticmethod
    def _mix_music(video_path: str, music_path: str, tmp: str, has_original_audio: bool) -> str:
        out = os.path.join(tmp, "with_music.mp4")
        if has_original_audio:
            # Mix at 18% volume behind original audio
            filter_complex = "[1:a]volume=0.18[bg];[0:a][bg]amix=inputs=2:duration=shortest[aout]"
            cmd = [
                "ffmpeg", "-y", "-i", video_path, "-i", music_path,
                "-filter_complex", filter_complex,
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
                "-shortest", out,
            ]
        else:
            # No original audio — use music at 25%
            cmd = [
                "ffmpeg", "-y", "-i", video_path, "-i", music_path,
                "-filter_complex", "[1:a]volume=0.25,atrim=duration=60[bg]",
                "-map", "0:v", "-map", "[bg]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
                "-shortest", out,
            ]
        VideoEditService._run(cmd)
        return out

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _run(cmd: list[str]) -> None:
        result = subprocess.run(cmd, capture_output=True, timeout=180)
        if result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode, cmd,
                output=result.stdout, stderr=result.stderr,
            )

    @staticmethod
    def _get_metadata(path: str) -> dict:
        try:
            proc = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-show_format", path],
                capture_output=True, timeout=30,
            )
            if proc.returncode != 0:
                return {"duration": 15.0, "width": 1920, "height": 1080, "has_audio": False}
            data     = json.loads(proc.stdout)
            video_s  = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
            audio_s  = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
            duration = float(data.get("format", {}).get("duration", 15.0))
            width    = int(video_s.get("width",  1920)) if video_s else 1920
            height   = int(video_s.get("height", 1080)) if video_s else 1080
            return {"duration": duration, "width": width, "height": height, "has_audio": audio_s is not None}
        except Exception:
            return {"duration": 15.0, "width": 1920, "height": 1080, "has_audio": False}

    @staticmethod
    def _find_font() -> str | None:
        for p in [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]:
            if os.path.exists(p):
                return p
        return None

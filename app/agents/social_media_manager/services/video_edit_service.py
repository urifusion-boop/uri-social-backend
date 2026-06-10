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
from openai import OpenAI

from app.core.config import settings
from app.database import get_db
from app.utils.cloudinary_upload import upload_bytes

PLATFORM_TARGETS = {
    "instagram_reels": {"duration": 15, "width": 1080, "height": 1920},
    "tiktok":          {"duration": 20, "width": 1080, "height": 1920},
    "facebook_reels":  {"duration": 15, "width": 1080, "height": 1920},
}

FILLER_WORDS = {"um", "uh", "ah", "er", "hmm", "mm"}

MOOD_TO_QUERY = {
    "upbeat":    "upbeat energetic happy",
    "ambient":   "ambient relaxing calm",
    "dramatic":  "cinematic dramatic epic",
    "playful":   "playful fun cheerful",
    "afrobeats": "afrobeat africa rhythm",
    "lo-fi":     "lofi chill relaxing",
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
            "status_message": "Starting…",
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

        async def _progress(pct: int, msg: str = ""):
            await col.update_one({"job_id": job_id}, {"$set": {"progress": pct, "status_message": msg}})

        # Download logo bytes async before entering the executor
        logo_bytes: bytes | None = None
        if logo_url:
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    r = await client.get(logo_url)
                    if r.status_code == 200:
                        logo_bytes = r.content
                        print(f"[VideoEdit] logo downloaded: {len(logo_bytes)} bytes from {logo_url[:80]}")
                    else:
                        print(f"[VideoEdit] logo download failed: HTTP {r.status_code} for {logo_url[:80]}")
            except Exception as e:
                print(f"[VideoEdit] logo download exception: {e}")

        # Fetch music from Pixabay async before entering the executor
        music_bytes: bytes | None = None
        if enhancements.get("add_music", True):
            mood = enhancements.get("music_mood", "upbeat")
            music_bytes = await VideoEditService.fetch_music_bytes(mood)

        try:
            await _progress(5, "Archiving original…")
            try:
                original_url = await upload_bytes(
                    video_bytes, folder="uri-social/original-videos", resource_type="video"
                )
                await col.update_one({"job_id": job_id}, {"$set": {"original_video_url": original_url}})
            except Exception:
                pass  # Archival failure should not block the edit

            await _progress(15, "Running AI pipeline…")
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: VideoEditService._run_ffmpeg_pipeline(
                    video_bytes, platform, enhancements,
                    brand_name, brand_cta,
                    brand_colors or [], logo_bytes, logo_position, tagline,
                    music_bytes=music_bytes,
                ),
            )
            edited_bytes: bytes = result["bytes"]
            edits_applied: list = result["edits_applied"]

            await _progress(88, "Uploading edited reel…")
            edited_url = await upload_bytes(
                edited_bytes,
                folder="uri-social/edited-reels",
                resource_type="video",
            )

            await _progress(95, "Saving to drafts…")
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
                "status_message": "Done!",
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
        music_bytes: bytes | None = None,
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

            working_path = input_path

            # ── Step 0: remove filler words ───────────────────────────────
            if enhancements.get("remove_fillers", True) and has_audio:
                print("[VideoEdit] transcribing for filler removal…")
                filler_words = VideoEditService._transcribe_words(working_path, tmp, "filler")
                if filler_words:
                    cleaned = VideoEditService._remove_fillers(working_path, filler_words, tmp, has_audio, trim_dur)
                    if cleaned:
                        working_path = cleaned
                        edits_applied.append("Filler words removed")
                        print("[VideoEdit] filler words removed")

            # ── Step 1: stabilise ─────────────────────────────────────────
            if enhancements.get("stabilise", True):
                stabilised = VideoEditService._stabilise(working_path, tmp, trim_dur, has_audio)
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
            main_path = os.path.join(tmp, "main.mp4")
            vf = ",".join(filters) if filters else None
            noise_af = "afftdn=nf=-25" if enhancements.get("noise_reduction", True) and has_audio else None

            if has_audio:
                cmd = ["ffmpeg", "-y", "-i", working_path, "-t", str(trim_dur)]
                if vf:
                    cmd += ["-vf", vf]
                if noise_af:
                    cmd += ["-af", noise_af]
                cmd += [
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-r", "30", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                    "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
                    main_path,
                ]
                if noise_af:
                    edits_applied.append("Noise reduced")
            else:
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
            has_audio = True  # all parts now carry audio for clean concat

            if enhancements.get("smart_trim", True):
                edits_applied.append(f"Trimmed to {int(trim_dur)}s")

            # ── Step 6b: auto-captions (Whisper → brand-styled ASS) ────────
            if enhancements.get("auto_captions", True):
                print("[VideoEdit] transcribing for captions…")
                caption_style = enhancements.get("caption_style", "word_by_word")
                words = VideoEditService._transcribe_words(main_path, tmp, prefix="cap")
                if words:
                    ass_path = VideoEditService._generate_ass(
                        words, brand_colors or [], caption_style, tmp, trim_dur, target_w, target_h
                    )
                    if ass_path:
                        captioned = VideoEditService._apply_captions(main_path, ass_path, tmp)
                        if captioned:
                            main_path = captioned
                            style_label = caption_style.replace("_", " ").title()
                            edits_applied.append(f"Auto-captions — {style_label}")
                            print(f"[VideoEdit] captions applied ({caption_style})")
                        else:
                            print("[VideoEdit] captions apply failed, skipping")
                else:
                    print("[VideoEdit] no words transcribed, skipping captions")

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
            mood = enhancements.get("music_mood", "upbeat")
            if enhancements.get("add_music", True):
                music_path: str | None = None
                if music_bytes:
                    music_path = os.path.join(tmp, "music.mp3")
                    with open(music_path, "wb") as f:
                        f.write(music_bytes)
                else:
                    # Fallback: local library (MUSIC_LIBRARY_PATH env var)
                    music_path = VideoEditService._find_music_track(mood)
                if music_path:
                    final_path = VideoEditService._mix_music(assembled_path, music_path, tmp, has_audio)
                    edits_applied.append(f"Background music added ({mood})")
                else:
                    print(f"[VideoEdit] no music track available for mood '{mood}', skipping")

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
    # Auto-captions: Whisper transcription → brand-styled ASS subtitles
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _transcribe_words(video_path: str, tmp: str, prefix: str = "tr") -> list:
        """Extract audio and call Whisper API for word-level timestamps."""
        try:
            audio_path = os.path.join(tmp, f"{prefix}_audio.wav")
            VideoEditService._run([
                "ffmpeg", "-y", "-i", video_path,
                "-vn", "-ar", "16000", "-ac", "1", "-f", "wav", audio_path,
            ])
            if not os.path.exists(audio_path) or os.path.getsize(audio_path) < 1000:
                return []

            client = OpenAI(api_key=settings.OPENAI_API_KEY)
            with open(audio_path, "rb") as f:
                result = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=f,
                    response_format="verbose_json",
                    timestamp_granularities=["word"],
                )

            raw_words = result.words if hasattr(result, "words") and result.words else []

            # Normalize to plain dicts with stripped word text
            normalized = []
            for w in raw_words:
                text = str(getattr(w, "word", "") if hasattr(w, "word") else w.get("word", "")).strip()
                start = float(getattr(w, "start", 0) if hasattr(w, "start") else w.get("start", 0))
                end = float(getattr(w, "end", 0) if hasattr(w, "end") else w.get("end", start + 0.3))
                if text:
                    normalized.append({"word": text, "start": start, "end": end})

            print(f"[VideoEdit] transcribed {len(normalized)} words")
            return normalized

        except Exception as e:
            print(f"[VideoEdit] transcription failed: {e}")
            return []

    @staticmethod
    def _hex_to_ass_color(hex_color: str) -> str:
        """Convert CSS hex color (#RRGGBB) to ASS BGR color (&H00BBGGRR)."""
        h = hex_color.lstrip("#")
        if len(h) == 3:
            h = h[0] * 2 + h[1] * 2 + h[2] * 2
        r = int(h[0:2], 16)
        g = int(h[2:4], 16)
        b = int(h[4:6], 16)
        return f"&H00{b:02X}{g:02X}{r:02X}"

    @staticmethod
    def _seconds_to_ass_time(seconds: float) -> str:
        """Convert seconds to ASS timestamp H:MM:SS.CC (centiseconds)."""
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        cs = int((seconds % 1) * 100)
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    @staticmethod
    def _generate_ass(
        words: list,
        brand_colors: list,
        caption_style: str,
        tmp: str,
        duration: float,
        w: int,
        h: int,
    ) -> str | None:
        """Generate an ASS subtitle file with brand-styled captions."""
        if not words:
            return None
        try:
            primary_hex = (brand_colors[0] if brand_colors else "#FFFFFF") or "#FFFFFF"
            brand_ass   = VideoEditService._hex_to_ass_color(primary_hex)
            white_ass   = "&H00FFFFFF"
            shadow_ass  = "&H80000000"
            outline_ass = "&H00000000"

            # Alignment: 2=bottom-center, 5=mid-center
            if caption_style == "bold_pop":
                font_size   = 80
                alignment   = 5
                bold        = -1
                border_style = 3   # opaque box
                outline_size = 0
                back_color  = "&HAA000000"
                primary     = brand_ass
            elif caption_style == "word_by_word":
                font_size   = 62
                alignment   = 2
                bold        = -1
                border_style = 1
                outline_size = 3
                back_color  = shadow_ass
                primary     = brand_ass
            else:  # full_line
                font_size   = 54
                alignment   = 2
                bold        = 0
                border_style = 1
                outline_size = 2
                back_color  = shadow_ass
                primary     = white_ass

            style_line = (
                f"Style: Default,Arial,{font_size},{primary},{white_ass},"
                f"{outline_ass},{back_color},{bold},0,0,0,100,100,0,0,"
                f"{border_style},{outline_size},1,{alignment},20,20,100,1"
            )

            ass_lines = [
                "[Script Info]",
                "ScriptType: v4.00+",
                f"PlayResX: {w}",
                f"PlayResY: {h}",
                "ScaledBorderAndShadow: yes",
                "",
                "[V4+ Styles]",
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
                "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
                "Alignment, MarginL, MarginR, MarginV, Encoding",
                style_line,
                "",
                "[Events]",
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            ]

            def safe(text: str) -> str:
                return text.replace("{", "").replace("}", "").replace("\\", "")

            if caption_style in ("word_by_word", "bold_pop"):
                for i, word_data in enumerate(words):
                    word  = safe(word_data["word"])
                    start = word_data["start"]
                    # Extend display to next word start to avoid blank flicker
                    if i < len(words) - 1:
                        end = min(word_data["end"] + 0.05, words[i + 1]["start"])
                    else:
                        end = word_data["end"]
                    if caption_style == "bold_pop":
                        word = word.upper()
                    t_start = VideoEditService._seconds_to_ass_time(start)
                    t_end   = VideoEditService._seconds_to_ass_time(end)
                    ass_lines.append(f"Dialogue: 0,{t_start},{t_end},Default,,0,0,0,,{word}")

            else:  # full_line
                groups = []
                current: list = []
                for i, word_data in enumerate(words):
                    current.append(word_data)
                    elapsed  = current[-1]["end"] - current[0]["start"]
                    next_gap = 0.0
                    if i < len(words) - 1:
                        next_gap = words[i + 1]["start"] - word_data["end"]
                    if len(current) >= 5 or elapsed >= 2.5 or next_gap > 0.4 or i == len(words) - 1:
                        groups.append(current)
                        current = []

                for group in groups:
                    t_start = VideoEditService._seconds_to_ass_time(group[0]["start"])
                    t_end   = VideoEditService._seconds_to_ass_time(group[-1]["end"])
                    text    = safe(" ".join(wd["word"] for wd in group))
                    ass_lines.append(f"Dialogue: 0,{t_start},{t_end},Default,,0,0,0,,{text}")

            ass_path = os.path.join(tmp, "captions.ass")
            with open(ass_path, "w", encoding="utf-8") as f:
                f.write("\n".join(ass_lines))
            return ass_path

        except Exception as e:
            print(f"[VideoEdit] ASS generation failed: {e}")
            return None

    @staticmethod
    def _apply_captions(video_path: str, ass_path: str, tmp: str) -> str | None:
        """Bake ASS subtitles into the video."""
        out = os.path.join(tmp, "captioned.mp4")
        # Escape path for FFmpeg filter syntax (colons and backslashes are special)
        escaped = ass_path.replace("\\", "/").replace(":", "\\:")
        try:
            VideoEditService._run([
                "ffmpeg", "-y", "-i", video_path,
                "-vf", f"ass='{escaped}'",
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-c:a", "copy",
                "-movflags", "+faststart", out,
            ])
            return out
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode(errors="replace")[-500:] if e.stderr else ""
            print(f"[VideoEdit] caption apply failed:\n{stderr}")
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Filler word removal
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _remove_fillers(
        input_path: str,
        words: list,
        tmp: str,
        has_audio: bool,
        max_dur: float,
    ) -> str | None:
        """Cut filler word segments using FFmpeg trim+concat filter_complex."""
        fillers = [
            w for w in words
            if w["word"].lower().strip(".,!?") in FILLER_WORDS
            and w["start"] < max_dur
        ]
        if not fillers:
            return None

        PADDING = 0.06  # 60 ms around each filler
        excluded = [
            (max(0.0, f["start"] - PADDING), min(max_dur, f["end"] + PADDING))
            for f in fillers
        ]
        # Merge overlapping exclusions
        excluded.sort()
        merged: list[list[float]] = []
        for s, e in excluded:
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])

        # Build keep segments
        keep = []
        pos = 0.0
        for excl_s, excl_e in merged:
            if pos < excl_s:
                keep.append((pos, excl_s))
            pos = excl_e
        if pos < max_dur:
            keep.append((pos, max_dur))

        if len(keep) <= 1 and not merged:
            return None

        n = len(keep)
        vf_parts, af_parts = [], []
        for i, (s, e) in enumerate(keep):
            vf_parts.append(f"[0:v]trim={s:.3f}:{e:.3f},setpts=PTS-STARTPTS[v{i}]")
            if has_audio:
                af_parts.append(f"[0:a]atrim={s:.3f}:{e:.3f},asetpts=PTS-STARTPTS[a{i}]")

        v_in = "".join(f"[v{i}]" for i in range(n))
        a_in = "".join(f"[a{i}]" for i in range(n))

        if has_audio:
            fc = (
                ";".join(vf_parts + af_parts)
                + f";{v_in}{a_in}concat=n={n}:v=1:a=1[vout][aout]"
            )
            map_args = ["-map", "[vout]", "-map", "[aout]",
                        "-c:a", "aac", "-b:a", "128k"]
        else:
            fc = ";".join(vf_parts) + f";{v_in}concat=n={n}:v=1:a=0[vout]"
            map_args = ["-map", "[vout]", "-an"]

        out = os.path.join(tmp, "no_fillers.mp4")
        try:
            VideoEditService._run([
                "ffmpeg", "-y", "-i", input_path,
                "-filter_complex", fc,
                *map_args,
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-movflags", "+faststart", out,
            ])
            print(f"[VideoEdit] removed {len(fillers)} filler word(s)")
            return out
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode(errors="replace")[-400:] if e.stderr else ""
            print(f"[VideoEdit] filler removal failed:\n{stderr}")
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Logo / Intro / Outro
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _apply_logo(
        video_path: str,
        logo_path: str,
        position: str,
        tmp: str,
        out_name: str = "with_logo.mp4",
    ) -> str | None:
        out = os.path.join(tmp, out_name)
        logo_scale = "scale=120:trunc(ow/a/2)*2"
        POSITIONS = {
            "top_left":     "10:10",
            "top_right":    "W-w-10:10",
            "bottom_left":  "10:H-h-10",
            "bottom_right": "W-w-10:H-h-10",
        }
        overlay_xy = POSITIONS.get(position, POSITIONS["bottom_right"])
        fc = (
            f"[1:v]{logo_scale},format=rgba[logo];"
            f"[0:v][logo]overlay={overlay_xy}:format=auto[vout]"
        )
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-i", video_path, "-i", logo_path,
                    "-filter_complex", fc,
                    "-map", "[vout]", "-map", "0:a?",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", out,
                ],
                capture_output=True,
                timeout=180,
            )
            if result.returncode != 0:
                print(f"[VideoEdit] logo overlay failed:\n{result.stderr.decode()[-600:]}")
                return None
            return out
        except Exception as e:
            print(f"[VideoEdit] logo overlay exception: {e}")
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
        font = VideoEditService._find_font()
        if not font:
            return None
        out = os.path.join(tmp, "intro.mp4")
        safe_color = bg_color.lstrip("#") if bg_color else "000000"
        name_label = (brand_name or "URI Social").replace("'", "\\'").replace(":", "\\:")
        tag_label  = (tagline or "").replace("'", "\\'").replace(":", "\\:")

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
            base_out = os.path.join(tmp, "intro_base.mp4")
            VideoEditService._run([
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", f"color=c=0x{safe_color}:s={w}x{h}:r=30:d=1.5",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                "-vf", ",".join(filters),
                "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-shortest", base_out,
            ])

            if logo_path:
                logo_out = VideoEditService._apply_logo(base_out, logo_path, "top_right", tmp, "intro_logo.mp4")
                if logo_out:
                    base_out = logo_out

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
    # Concatenation
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _concat(parts: list[str], tmp: str, has_audio: bool) -> str:
        out = os.path.join(tmp, "assembled.mp4")
        n = len(parts)
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
    # Background music — Pixabay API fetch + local library fallback
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    async def fetch_music_bytes(mood: str) -> bytes | None:
        """Fetch a royalty-free track from Pixabay by mood. Returns MP3 bytes or None."""
        api_key = (settings.PIXABAY_API_KEY or "").strip()
        if not api_key:
            return None
        query = MOOD_TO_QUERY.get(mood, mood)
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.get(
                    "https://pixabay.com/api/",
                    params={
                        "key": api_key,
                        "q": query,
                        "media_type": "music",
                        "per_page": 10,
                        "safesearch": "true",
                    },
                )
                if r.status_code != 200:
                    print(f"[VideoEdit] Pixabay API error {r.status_code}: {r.text[:200]}")
                    return None

                data = r.json()
                hits = data.get("hits", [])
                if not hits:
                    print(f"[VideoEdit] Pixabay: no music found for '{mood}' (query: {query})")
                    return None

                track = random.choice(hits[:min(5, len(hits))])
                # Try common field names — log keys on first miss so we can fix fast
                audio_url = (
                    track.get("audio_url")
                    or track.get("previewURL")
                    or track.get("mp3")
                    or track.get("url")
                )
                if not audio_url:
                    print(f"[VideoEdit] Pixabay: no audio URL found. Track keys: {list(track.keys())}")
                    return None

                print(f"[VideoEdit] Pixabay music: {audio_url[:80]}")
                audio_r = await client.get(audio_url, timeout=30)
                if audio_r.status_code == 200:
                    print(f"[VideoEdit] Pixabay music downloaded: {len(audio_r.content):,} bytes")
                    return audio_r.content
                print(f"[VideoEdit] Pixabay audio download failed: HTTP {audio_r.status_code}")
                return None
        except Exception as e:
            print(f"[VideoEdit] Pixabay fetch exception: {e}")
            return None

    @staticmethod
    def _find_music_track(mood: str) -> str | None:
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
            tracks = (
                glob.glob(os.path.join(base, "*.mp3")) +
                glob.glob(os.path.join(base, "*.wav"))
            )
        return random.choice(tracks) if tracks else None

    @staticmethod
    def _mix_music(video_path: str, music_path: str, tmp: str, has_original_audio: bool) -> str:
        out = os.path.join(tmp, "with_music.mp4")
        if has_original_audio:
            filter_complex = "[1:a]volume=0.18[bg];[0:a][bg]amix=inputs=2:duration=shortest[aout]"
            cmd = [
                "ffmpeg", "-y", "-i", video_path, "-i", music_path,
                "-filter_complex", filter_complex,
                "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
                "-shortest", out,
            ]
        else:
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
        result = subprocess.run(cmd, capture_output=True, timeout=300)
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

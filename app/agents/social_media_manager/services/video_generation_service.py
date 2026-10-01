import asyncio
import os
import uuid
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings
from app.database import get_db
from app.utils.s3_upload import upload_bytes

# Every model now runs through fal.ai — Veo was moved off the direct Google
# Gemini API call (google-genai client, DEFAULT_MODEL = "veo-3.1-generate-preview")
# onto fal.ai's own Veo 3.1 endpoint, same pattern as Kling/Seedance already used.
# Two independent reasons, both real: (1) the Google-direct path kept failing on
# a real Veo quota/billing gate on that Google Cloud project (confirmed live via
# 429 RESOURCE_EXHAUSTED on every attempt, 2026-09-30/10-01) that fal.ai's own
# account sidesteps entirely; (2) fal.ai's price for the SAME Veo 3.1 Standard
# model is HALF what Google charges directly for it ($0.20/s vs $0.40/s, no
# audio, 720p/1080p — confirmed from fal.ai's own pricing) — so this is strictly
# better even once the Google quota issue is eventually resolved.
#
# MODEL_REGISTRY is the single source of truth for every selectable model: its
# fal.ai endpoint id, its display label (the UI shows this now, not "Version N"),
# and its published per-second rate. cost_per_second is fal.ai's own documented
# rate (confirmed 2026-10-01, no-audio / mid-resolution tier for each — the tier
# actually requested below) — used to show an ESTIMATED cost per generated clip.
# fal_client.subscribe_async's response has no real-time billing field, so this
# is computed (duration actually sent to the model × its rate), not read back
# from fal.ai; label it as an estimate wherever it's shown.
MODEL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "fal-ai/veo3.1/image-to-video": {
        "label": "Veo 3.1",
        "cost_per_second": 0.20,
    },
    "fal-ai/kling-video/v3/pro/image-to-video": {
        "label": "Kling 3.0 Pro",
        "cost_per_second": 0.112,
    },
    "bytedance/seedance-2.0/image-to-video": {
        "label": "Seedance 2.0",
        "cost_per_second": 0.2419,
    },
    "fal-ai/luma-dream-machine/ray-2/image-to-video": {
        "label": "Luma Ray 2",
        "cost_per_second": 0.10,
    },
    "fal-ai/minimax/hailuo-02/standard/image-to-video": {
        "label": "MiniMax Hailuo 02",
        "cost_per_second": 0.045,
    },
    "fal-ai/wan/v2.2-a14b/image-to-video": {
        "label": "Wan 2.2",
        "cost_per_second": 0.08,
    },
}

DEFAULT_MODEL = "fal-ai/veo3.1/image-to-video"


def _jobs_collection():
    return get_db()["video_generation_jobs"]


async def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    doc = await _jobs_collection().find_one({"job_id": job_id}, {"_id": 0})
    return doc


class VideoGenerationService:

    @staticmethod
    async def create_job(storyboard: dict, model: str) -> str:
        job_id = uuid.uuid4().hex
        await _jobs_collection().insert_one({
            "job_id": job_id,
            "status": "queued",
            "model": model,
            "total_scenes": len(storyboard.get("scenes", [])),
            "current_scene": 0,
            "clips": [],
            "error": None,
        })
        return job_id

    @staticmethod
    async def run_job(
        job_id: str,
        storyboard: dict,
        brand_images: List[str],
        model: str,
    ) -> None:
        col = _jobs_collection()
        scenes = storyboard.get("scenes", [])
        await col.update_one({"job_id": job_id}, {"$set": {"status": "generating"}})

        for scene in scenes:
            scene_num = scene.get("scene_number", 0)
            await col.update_one({"job_id": job_id}, {"$set": {"current_scene": scene_num}})

            try:
                video_url, cost_usd = await VideoGenerationService._generate_scene_fal(scene, model)
                clip = {
                    "scene_number": scene_num,
                    "shot_type": scene.get("shot_type", ""),
                    "duration_seconds": scene.get("duration_seconds", 5),
                    "motion": scene.get("motion", ""),
                    "text_overlay": scene.get("text_overlay"),
                    "video_prompt": scene.get("video_prompt", ""),
                    "video_url": video_url,
                    "cost_usd": cost_usd,
                }
            except Exception as e:
                print(f"[VideoGenJob {job_id}] Scene {scene_num} failed: {e}")
                clip = {
                    "scene_number": scene_num,
                    "shot_type": scene.get("shot_type", ""),
                    "duration_seconds": scene.get("duration_seconds", 5),
                    "motion": scene.get("motion", ""),
                    "text_overlay": scene.get("text_overlay"),
                    "video_prompt": scene.get("video_prompt", ""),
                    "video_url": None,
                    "cost_usd": None,
                    "error": str(e),
                }

            await col.update_one({"job_id": job_id}, {"$push": {"clips": clip}})

        await col.update_one(
            {"job_id": job_id},
            {"$set": {"status": "complete", "current_scene": len(scenes)}},
        )

    # ── fal.ai — every model (Veo 3.1, Kling 3.0 Pro, Seedance 2.0, Luma Ray 2,
    #    MiniMax Hailuo 02, Wan 2.2) goes through this one entry point. Each
    #    model's request shape differs (confirmed from fal.ai's own per-model API
    #    docs, 2026-10-01) — duration as a string-with-"s" suffix for Veo/Luma,
    #    a bare integer for Hailuo, or not a direct field at all for Wan (it's
    #    frames ÷ fps instead) — so this dispatches per model id rather than
    #    pretending they share one shape. ─────────────────────────────────────

    @staticmethod
    async def _generate_scene_fal(scene: dict, model: str) -> tuple[str, Optional[float]]:
        import fal_client

        frame_image_url = scene.get("frame_image_url")
        if not frame_image_url:
            raise ValueError(f"{model} requires a storyboard frame image — generate the storyboard frames first")

        info = MODEL_REGISTRY.get(model)
        if info is None:
            raise ValueError(f"Unknown video model: {model}")

        prompt = scene.get("video_prompt", "")
        duration_req = scene.get("duration_seconds", 5)
        scene_num = scene.get("scene_number")
        actual_seconds: float

        if model == "fal-ai/veo3.1/image-to-video":
            seconds = 8 if duration_req >= 7 else (6 if duration_req >= 5 else 4)
            actual_seconds = seconds
            arguments: Dict[str, Any] = {
                "image_url": frame_image_url,
                "prompt": prompt,
                "duration": f"{seconds}s",
                "aspect_ratio": "9:16",
                "resolution": "720p",
                "generate_audio": False,  # audio doubles the price for no benefit on a silent storyboard clip
            }
            print(f"[VideoGen] Scene {scene_num}: Veo 3.1 (fal.ai), {seconds}s")

        elif model == "fal-ai/kling-video/v3/pro/image-to-video":
            seconds = max(3, min(15, duration_req))
            actual_seconds = seconds
            arguments = {
                "start_image_url": frame_image_url,
                "prompt": prompt,
                "duration": str(seconds),
                "generate_audio": True,
            }
            print(f"[VideoGen] Scene {scene_num}: Kling 3.0 Pro, {seconds}s")

        elif model == "bytedance/seedance-2.0/image-to-video":
            seconds = max(4, min(15, duration_req))
            actual_seconds = seconds
            arguments = {
                "image_url": frame_image_url,
                "prompt": prompt,
                "duration": str(seconds),
                "aspect_ratio": "9:16",
                "resolution": "720p",
                "generate_audio": True,
            }
            print(f"[VideoGen] Scene {scene_num}: Seedance 2.0, {seconds}s")

        elif model == "fal-ai/luma-dream-machine/ray-2/image-to-video":
            seconds = 9 if duration_req >= 7 else 5
            actual_seconds = seconds
            arguments = {
                "image_url": frame_image_url,
                "prompt": prompt,
                "duration": f"{seconds}s",
                "aspect_ratio": "9:16",
                "resolution": "720p",
            }
            print(f"[VideoGen] Scene {scene_num}: Luma Ray 2, {seconds}s")

        elif model == "fal-ai/minimax/hailuo-02/standard/image-to-video":
            seconds = 10 if duration_req >= 8 else 6
            actual_seconds = seconds
            arguments = {
                "image_url": frame_image_url,
                "prompt": prompt,
                "duration": seconds,  # integer, not a string, per this model's own schema
                "resolution": "768P",
            }
            print(f"[VideoGen] Scene {scene_num}: MiniMax Hailuo 02, {seconds}s")

        elif model == "fal-ai/wan/v2.2-a14b/image-to-video":
            # No direct duration field — Wan takes frame count + fps instead.
            # 16 fps (its own default) keeps this a plain multiply, clamped to
            # the model's accepted 17-161 frame range.
            fps = 16
            seconds = max(2, min(10, duration_req))
            num_frames = max(17, min(161, round(seconds * fps)))
            actual_seconds = num_frames / fps
            arguments = {
                "image_url": frame_image_url,
                "prompt": prompt,
                "num_frames": num_frames,
                "frames_per_second": fps,
                "aspect_ratio": "9:16",
                "resolution": "720p",
            }
            print(f"[VideoGen] Scene {scene_num}: Wan 2.2, {num_frames}f @ {fps}fps (~{actual_seconds:.1f}s)")

        else:
            raise ValueError(f"Unknown video model: {model}")

        # fal_client reads FAL_KEY; map our FAL_API_KEY if needed
        fal_key = settings.FAL_API_KEY
        if fal_key:
            os.environ["FAL_KEY"] = fal_key

        try:
            result = await fal_client.subscribe_async(model, arguments)
        except Exception as e:
            err_str = str(e)
            if "generate_audio" in arguments and (
                "content_policy_violation" in err_str or "sensitive content" in err_str.lower()
            ):
                print(f"[VideoGen] Scene {scene_num}: audio flagged, retrying without audio")
                arguments = {**arguments, "generate_audio": False}
                result = await fal_client.subscribe_async(model, arguments)
            else:
                raise

        video_url = result["video"]["url"]

        # Download and store in Cloudinary
        async with httpx.AsyncClient(timeout=120) as client:
            video_resp = await client.get(video_url)
            video_resp.raise_for_status()
            video_bytes = video_resp.content

        stored_url = await upload_bytes(
            video_bytes,
            folder="uri-social/generated-videos",
            resource_type="video",
        )
        cost_usd = round(actual_seconds * info["cost_per_second"], 3)
        return stored_url, cost_usd

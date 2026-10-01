import asyncio
import os
import uuid
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings
from app.database import get_db
from app.utils.s3_upload import upload_bytes

# Model lineup matches the "URI_AI_Video_API_Model_Selection_Engineering_Brief"
# (Uzuri Creative / URI, 1 October 2026) exactly — its 7 selected launch routes,
# minus Seedance 2.5 which the brief explicitly says not to make a launch
# default ("Do not expose as standard launch infrastructure without evidence
# that the quality uplift improves conversion or reduces retries enough to
# justify the cost" — §3.8). Every model here is fal.ai's own IMAGE-to-video
# endpoint for that model (confirmed per-model from fal.ai's own API docs,
# 2026-10-01) since this pipeline always has a real, brand-grounded storyboard
# frame to animate — never a bare text prompt.
#
# Two of the brief's picks (MiniMax H3 Max Turbo, MiniMax H3 Max) are listed
# under fal.ai's bare `minimax/` namespace rather than `fal-ai/` — confirmed
# correct, not a typo; that's how fal.ai itself hosts this specific partner's
# models.
#
# cost_per_second below is the brief's own figure (§1's table), converted from
# its stated NGN back to USD at the brief's own FX assumption (₦1,327.84/$1,
# 1 October 2026) so it's directly comparable with the rest of this file's USD
# math — not re-derived from a separate pricing lookup. The brief itself warns
# prices change and should be re-checked against live fal.ai pricing before any
# production rollout; this is a snapshot, not a live-fetched rate.
MODEL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "minimax/h3-max-turbo/image-to-video": {
        "label": "H3 Max Turbo",
        "role": "Default generation",
        "cost_per_second": 0.0399,
    },
    "fal-ai/kling-video/v2.5-turbo/standard/image-to-video": {
        "label": "Kling 2.5 Standard",
        "role": "Product image animation",
        "cost_per_second": 0.0422,
    },
    "fal-ai/pixverse/v6/image-to-video": {
        "label": "PixVerse V6",
        "role": "Social content",
        "cost_per_second": 0.0603,
    },
    "minimax/h3-max/image-to-video": {
        "label": "H3 Max",
        "role": "Premium quality",
        "cost_per_second": 0.0798,
    },
    "alibaba/wan-3.0/image-to-video": {
        "label": "Wan 3.0",
        "role": "Complex motion",
        "cost_per_second": 0.1002,
    },
    "fal-ai/veo3.1/fast/image-to-video": {
        "label": "Veo 3.1 Fast",
        "role": "Dialogue / speaking",
        "cost_per_second": 0.1499,
    },
    "bytedance/seedance-2.0/image-to-video": {
        "label": "Seedance 2.0 Fast",
        "role": "Advanced references",
        "cost_per_second": 0.2419,
    },
}

DEFAULT_MODEL = "minimax/h3-max-turbo/image-to-video"


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

    # ── fal.ai — every model in MODEL_REGISTRY goes through this one entry
    #    point. Each model's request shape genuinely differs (confirmed from
    #    fal.ai's own per-model API docs, 2026-10-01) — Wan 3.0 uniquely uses
    #    `start_image_url` where every other model here uses `image_url`;
    #    Kling 2.5 Standard only accepts duration 5 or 10; the two MiniMax H3
    #    models have no resolution/audio toggle beyond a 480P/768P/1080P enum
    #    (audio is automatic, not optional) — so this dispatches per model id
    #    rather than pretending they share one shape. ───────────────────────

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

        if model in ("minimax/h3-max-turbo/image-to-video", "minimax/h3-max/image-to-video"):
            # No documented min/max on `duration` beyond "integer, default 5" —
            # clamped to the 5-10s range every other MiniMax model in this file
            # uses, rather than sending an unvalidated raw value.
            seconds = max(5, min(10, duration_req))
            actual_seconds = seconds
            arguments: Dict[str, Any] = {
                "image_url": frame_image_url,
                "prompt": prompt,
                "duration": seconds,
                "resolution": "768P",
                "prompt_expansion_mode": "balanced",
            }
            print(f"[VideoGen] Scene {scene_num}: {info['label']}, {seconds}s")

        elif model == "fal-ai/kling-video/v2.5-turbo/standard/image-to-video":
            seconds = 10 if duration_req >= 8 else 5  # only valid values per this model's own schema
            actual_seconds = seconds
            arguments = {
                "image_url": frame_image_url,
                "prompt": prompt,
                "duration": seconds,
            }
            print(f"[VideoGen] Scene {scene_num}: Kling 2.5 Standard, {seconds}s")

        elif model == "fal-ai/pixverse/v6/image-to-video":
            seconds = max(1, min(15, duration_req))
            actual_seconds = seconds
            arguments = {
                "image_url": frame_image_url,
                "prompt": prompt,
                "duration": seconds,
                "resolution": "720p",
                "generate_audio_switch": True,  # brief's own ₦80/sec figure is priced "720p + audio"
            }
            print(f"[VideoGen] Scene {scene_num}: PixVerse V6, {seconds}s")

        elif model == "alibaba/wan-3.0/image-to-video":
            seconds = max(3, min(10, duration_req))
            actual_seconds = seconds
            arguments = {
                "start_image_url": frame_image_url,  # NOT image_url — this model's own field name
                "prompt": prompt,
                "duration": seconds,
                "resolution": "720p",
                "aspect_ratio": "9:16",
            }
            print(f"[VideoGen] Scene {scene_num}: Wan 3.0, {seconds}s")

        elif model == "fal-ai/veo3.1/fast/image-to-video":
            seconds = 8 if duration_req >= 7 else (6 if duration_req >= 5 else 4)
            actual_seconds = seconds
            arguments = {
                "image_url": frame_image_url,
                "prompt": prompt,
                "duration": f"{seconds}s",
                "aspect_ratio": "9:16",
                "resolution": "720p",
                "generate_audio": True,  # brief's own ₦199/sec figure is priced "Fast + audio"
            }
            print(f"[VideoGen] Scene {scene_num}: Veo 3.1 Fast, {seconds}s")

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
            print(f"[VideoGen] Scene {scene_num}: Seedance 2.0 Fast, {seconds}s")

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
            has_audio_toggle = "generate_audio" in arguments or "generate_audio_switch" in arguments
            if has_audio_toggle and (
                "content_policy_violation" in err_str or "sensitive content" in err_str.lower()
            ):
                print(f"[VideoGen] Scene {scene_num}: audio flagged, retrying without audio")
                toggle_key = "generate_audio" if "generate_audio" in arguments else "generate_audio_switch"
                arguments = {**arguments, toggle_key: False}
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

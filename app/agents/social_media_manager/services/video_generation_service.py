import asyncio
import uuid
from typing import Any, Dict, List, Optional

import httpx

from app.core.config import settings
from app.database import get_db
from app.utils.cloudinary_upload import upload_bytes

try:
    from google import genai
    from google.genai import types as genai_types
    _gemini_client = genai.Client(api_key=settings.GOOGLE_GEMINI_API_KEY)
except Exception as _e:
    _gemini_client = None
    print(f"[VideoGenerationService] google-genai init failed: {_e}")

DEFAULT_MODEL = "veo-3.1-generate-preview"

# fal.ai model IDs
KLING_MODEL = "fal-ai/kling-video/v3/pro/image-to-video"
SEEDANCE_MODEL = "bytedance/seedance-2.0/image-to-video"


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

        if model == DEFAULT_MODEL and not _gemini_client:
            await col.update_one(
                {"job_id": job_id},
                {"$set": {"status": "failed", "error": "Google Gemini client not initialised"}},
            )
            return

        scenes = storyboard.get("scenes", [])
        await col.update_one({"job_id": job_id}, {"$set": {"status": "generating"}})

        for scene in scenes:
            scene_num = scene.get("scene_number", 0)
            await col.update_one({"job_id": job_id}, {"$set": {"current_scene": scene_num}})

            try:
                video_url = await VideoGenerationService._generate_scene(scene, brand_images, model)
                clip = {
                    "scene_number": scene_num,
                    "shot_type": scene.get("shot_type", ""),
                    "duration_seconds": scene.get("duration_seconds", 5),
                    "motion": scene.get("motion", ""),
                    "text_overlay": scene.get("text_overlay"),
                    "video_prompt": scene.get("video_prompt", ""),
                    "video_url": video_url,
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
                    "error": str(e),
                }

            await col.update_one({"job_id": job_id}, {"$push": {"clips": clip}})

        await col.update_one(
            {"job_id": job_id},
            {"$set": {"status": "complete", "current_scene": len(scenes)}},
        )

    @staticmethod
    async def _generate_scene(scene: dict, brand_images: List[str], model: str) -> str:
        if "kling" in model or "seedance" in model:
            return await VideoGenerationService._generate_scene_fal(scene, model)
        return await VideoGenerationService._generate_scene_veo(scene, brand_images, model)

    # ── Veo 3.1 ──────────────────────────────────────────────────────────────

    @staticmethod
    async def _generate_scene_veo(scene: dict, brand_images: List[str], model: str) -> str:
        prompt = scene.get("video_prompt", "")
        duration_req = scene.get("duration_seconds", 5)
        # Veo only accepts 4, 6, or 8 seconds
        duration = 8 if duration_req >= 7 else (6 if duration_req >= 5 else 4)

        first_frame: Optional[genai_types.Image] = None
        frame_image_url = scene.get("frame_image_url")
        if frame_image_url:
            try:
                async with httpx.AsyncClient(timeout=30) as http:
                    resp = await http.get(frame_image_url)
                    frame_bytes = resp.content
                mime = "image/webp" if ".webp" in frame_image_url else "image/jpeg"
                first_frame = genai_types.Image(image_bytes=frame_bytes, mime_type=mime)
                print(f"[VideoGen] Scene {scene.get('scene_number')}: animating from storyboard frame (Veo)")
            except Exception as e:
                print(f"[VideoGen] Scene {scene.get('scene_number')}: could not load frame: {e}")

        config = genai_types.GenerateVideosConfig(
            aspect_ratio="9:16",
            duration_seconds=duration,
            number_of_videos=1,
        )

        loop = asyncio.get_running_loop()
        operation = await loop.run_in_executor(
            None,
            lambda: _gemini_client.models.generate_videos(
                model=model,
                prompt=prompt,
                image=first_frame,
                config=config,
            ),
        )

        for _ in range(60):
            if operation.done:
                break
            await asyncio.sleep(10)
            operation = await loop.run_in_executor(
                None,
                lambda op=operation: _gemini_client.operations.get(op),
            )

        if not operation.done:
            raise TimeoutError("Veo generation timed out after 10 minutes")
        if not operation.response:
            raise ValueError("Veo returned no response — prompt may have been rejected by content policy")

        generated = operation.response.generated_videos
        if not generated:
            raise ValueError("Veo returned no generated videos")

        video_file = generated[0].video
        video_bytes = await loop.run_in_executor(
            None,
            lambda: _gemini_client.files.download(file=video_file),
        )

        return await upload_bytes(
            bytes(video_bytes),
            folder="uri-social/generated-videos",
            resource_type="video",
        )

    # ── fal.ai (Kling 3.0 Pro + Seedance 2.0) ────────────────────────────────

    @staticmethod
    async def _generate_scene_fal(scene: dict, model: str) -> str:
        import os
        import fal_client

        frame_image_url = scene.get("frame_image_url")
        if not frame_image_url:
            raise ValueError(f"{model} requires a storyboard frame image — generate the storyboard frames first")

        prompt = scene.get("video_prompt", "")
        duration_req = scene.get("duration_seconds", 5)

        if "kling" in model:
            duration = str(max(3, min(15, duration_req)))
            arguments: Dict[str, Any] = {
                "start_image_url": frame_image_url,
                "prompt": prompt,
                "duration": duration,
                "generate_audio": True,
            }
            print(f"[VideoGen] Scene {scene.get('scene_number')}: Kling 3.0 Pro, {duration}s")
        else:  # seedance
            duration = str(max(4, min(15, duration_req)))
            arguments = {
                "image_url": frame_image_url,
                "prompt": prompt,
                "duration": duration,
                "aspect_ratio": "9:16",
                "resolution": "720p",
                "generate_audio": True,
            }
            print(f"[VideoGen] Scene {scene.get('scene_number')}: Seedance 2.0, {duration}s")

        # fal_client reads FAL_KEY; map our FAL_API_KEY if needed
        fal_key = settings.FAL_API_KEY
        if fal_key:
            os.environ["FAL_KEY"] = fal_key

        result = await fal_client.subscribe_async(model, arguments)
        video_url = result["video"]["url"]

        # Download and store in Cloudinary
        async with httpx.AsyncClient(timeout=120) as client:
            video_resp = await client.get(video_url)
            video_resp.raise_for_status()
            video_bytes = video_resp.content

        return await upload_bytes(
            video_bytes,
            folder="uri-social/generated-videos",
            resource_type="video",
        )

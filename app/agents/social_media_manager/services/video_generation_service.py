import asyncio
import uuid
from typing import Any, Dict, List, Optional

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
        """
        Background task: generate one Veo 3.1 video clip per storyboard scene,
        upload each to Cloudinary, and update the job record progressively.
        """
        col = _jobs_collection()

        if not _gemini_client:
            await col.update_one(
                {"job_id": job_id},
                {"$set": {"status": "failed", "error": "Google Gemini client not initialised — check GOOGLE_GEMINI_API_KEY"}},
            )
            return

        scenes = storyboard.get("scenes", [])
        await col.update_one({"job_id": job_id}, {"$set": {"status": "generating"}})

        for scene in scenes:
            scene_num = scene.get("scene_number", 0)
            await col.update_one({"job_id": job_id}, {"$set": {"current_scene": scene_num}})

            try:
                video_url = await VideoGenerationService._generate_scene(
                    scene, brand_images, model
                )
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
    async def _generate_scene(
        scene: dict,
        brand_images: List[str],
        model: str,
    ) -> str:
        """Generate one clip for a storyboard scene and return its Cloudinary URL."""
        prompt = scene.get("video_prompt", "")
        duration_req = scene.get("duration_seconds", 5)
        # Veo 3.1 only accepts 4, 6, or 8 seconds
        duration = 8 if duration_req >= 7 else (6 if duration_req >= 5 else 4)

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
                config=config,
            ),
        )

        # Poll every 10 s, max 10 minutes
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

        generated = operation.response.generated_videos
        if not generated:
            raise ValueError("Veo returned no generated videos")

        video_file = generated[0].video
        video_bytes = await loop.run_in_executor(
            None,
            lambda: _gemini_client.files.download(file=video_file),
        )

        cloudinary_url = await upload_bytes(
            bytes(video_bytes),
            folder="uri-social/generated-videos",
            resource_type="video",
        )
        return cloudinary_url

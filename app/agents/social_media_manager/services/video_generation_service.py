import asyncio
import base64
import re
import uuid
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.utils.cloudinary_upload import upload_bytes

try:
    from google import genai
    from google.genai import types as genai_types
    _gemini_client = genai.Client(api_key=settings.GOOGLE_GEMINI_API_KEY)
except Exception as _e:
    _gemini_client = None
    print(f"[VideoGenerationService] google-genai init failed: {_e}")

# In-memory job store — survives the HTTP request, cleared on container restart
_jobs: Dict[str, Dict[str, Any]] = {}

DEFAULT_MODEL = "veo-3.1-generate-preview"


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    return _jobs.get(job_id)


class VideoGenerationService:

    @staticmethod
    def create_job(storyboard: dict, model: str) -> str:
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "model": model,
            "total_scenes": len(storyboard.get("scenes", [])),
            "current_scene": 0,
            "clips": [],
            "error": None,
        }
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
        job = _jobs.get(job_id)
        if not job:
            return

        if not _gemini_client:
            job["status"] = "failed"
            job["error"] = "Google Gemini client not initialised — check GOOGLE_GEMINI_API_KEY"
            return

        scenes = storyboard.get("scenes", [])
        job["status"] = "generating"

        for scene in scenes:
            scene_num = scene.get("scene_number", 0)
            job["current_scene"] = scene_num

            try:
                video_url = await VideoGenerationService._generate_scene(
                    scene, brand_images, model
                )
                job["clips"].append({
                    "scene_number": scene_num,
                    "shot_type": scene.get("shot_type", ""),
                    "duration_seconds": scene.get("duration_seconds", 5),
                    "motion": scene.get("motion", ""),
                    "text_overlay": scene.get("text_overlay"),
                    "video_prompt": scene.get("video_prompt", ""),
                    "video_url": video_url,
                })
            except Exception as e:
                print(f"[VideoGenJob {job_id}] Scene {scene_num} failed: {e}")
                job["clips"].append({
                    "scene_number": scene_num,
                    "shot_type": scene.get("shot_type", ""),
                    "duration_seconds": scene.get("duration_seconds", 5),
                    "motion": scene.get("motion", ""),
                    "text_overlay": scene.get("text_overlay"),
                    "video_prompt": scene.get("video_prompt", ""),
                    "video_url": None,
                    "error": str(e),
                })

        job["status"] = "complete"
        job["current_scene"] = len(scenes)

    @staticmethod
    async def _generate_scene(
        scene: dict,
        brand_images: List[str],
        model: str,
    ) -> str:
        """Generate one clip for a storyboard scene and return its Cloudinary URL."""
        prompt = scene.get("video_prompt", "")
        duration_req = scene.get("duration_seconds", 5)
        # Veo 3.1 accepts 5 or 8 seconds
        duration = 8 if duration_req >= 7 else 5
        ref_idx = scene.get("reference_image_index", 0)

        # Attach the reference brand image as the first frame
        ref_image = None
        if brand_images and ref_idx < len(brand_images):
            img_data = brand_images[ref_idx]
            match = re.match(r"data:([^;]+);base64,(.+)", img_data, re.DOTALL)
            if match:
                mime_type = match.group(1)
                img_bytes = base64.b64decode(match.group(2))
                ref_image = genai_types.Image(
                    image_bytes=img_bytes,
                    mime_type=mime_type,
                )

        config = genai_types.GenerateVideosConfig(
            aspect_ratio="9:16",
            duration_seconds=duration,
            number_of_videos=1,
        )

        loop = asyncio.get_running_loop()

        # Submit — returns a long-running operation immediately
        generate_kwargs = dict(model=model, prompt=prompt, config=config)
        if ref_image:
            generate_kwargs["image"] = ref_image

        operation = await loop.run_in_executor(
            None,
            lambda: _gemini_client.models.generate_videos(**generate_kwargs),
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

        # Download video bytes from Google, upload to Cloudinary
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

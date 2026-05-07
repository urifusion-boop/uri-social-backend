import asyncio
import base64
import json
import re
import uuid
from io import BytesIO
from typing import Any, Dict, List, Optional

from app.database import get_db
from app.services.AIService import client as openai_client
from app.utils.cloudinary_upload import upload_bytes

_SYSTEM_PROMPT = """You are a creative director specialising in short-form social video for brands.

You receive:
  1. One or more brand images (logo, product photos, sample posts, lifestyle shots).
  2. Optional creative direction text from the marketer.
  3. Brand context: name, industry, color palette, voice, region, target platform.

Your job: study the brand images carefully, then produce a JSON video storyboard.

Rules:
- The brand color palette MUST dominate every scene — no other colors allowed.
- video_prompt fields must be motion-aware: describe exactly what moves, camera direction, speed, and lighting.
- reference_image_index tells which supplied image becomes the first frame of that clip (0-based).
- Each scene must work as a self-contained 3–5 second moment.
- text_overlay is a short on-screen caption/tagline string, or null.
- shot_type must be one of: product_hero | lifestyle | brand_close_up | text_card | transition

Return ONLY valid JSON — no markdown fences, no explanation:
{
  "total_duration_seconds": <int>,
  "target_platform": "<string>",
  "aspect_ratio": "9:16",
  "scenes": [
    {
      "scene_number": <int>,
      "duration_seconds": <int>,
      "shot_type": "<product_hero|lifestyle|brand_close_up|text_card|transition>",
      "motion": "<plain-English camera/subject motion description>",
      "video_prompt": "<full motion-aware prompt for the video model>",
      "reference_image_index": <int 0-based>,
      "text_overlay": <string or null>
    }
  ]
}"""


def _frame_jobs_collection():
    return get_db()["storyboard_frame_jobs"]


class VideoStoryboardService:

    @staticmethod
    def _decode_brand_image(img_data: str) -> bytes:
        """Extract raw bytes from a base64 data URL or plain base64 string."""
        if img_data.startswith("data:"):
            _, encoded = img_data.split(",", 1)
        else:
            encoded = img_data
        return base64.b64decode(encoded)

    @staticmethod
    async def _generate_scene_frame(scene: dict, brand_images: List[str]) -> Optional[str]:
        """
        Edit the reference brand image for this scene using gpt-image-2 so the frame
        is faithfully grounded in the real brand visual rather than hallucinated.
        """
        try:
            ref_idx = scene.get("reference_image_index", 0)
            if not brand_images:
                return None

            ref_idx = max(0, min(ref_idx, len(brand_images) - 1))
            img_bytes = VideoStoryboardService._decode_brand_image(brand_images[ref_idx])

            shot = scene.get("shot_type", "").replace("_", " ")
            video_prompt = scene.get("video_prompt", "")
            motion = scene.get("motion", "")
            text = scene.get("text_overlay") or ""

            prompt = (
                f"Cinematic storyboard frame, {shot} shot. "
                f"{video_prompt} "
                f"Camera movement: {motion}. "
                + (f'On-screen text: "{text}". ' if text else "")
                + "Keep the brand product, colors, and visual identity exactly as shown. "
                "Photorealistic, dramatic lighting. Vertical 9:16 composition."
            )

            img_file = BytesIO(img_bytes)
            img_file.name = "reference.png"

            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None,
                lambda: openai_client.images.edit(
                    image=img_file,
                    model="gpt-image-2",
                    prompt=prompt,
                    n=1,
                    size="1024x1536",
                    quality="medium",
                ),
            )
            out_bytes = base64.b64decode(resp.data[0].b64_json)
            url = await upload_bytes(
                out_bytes,
                folder="uri-social/storyboard-frames",
                resource_type="image",
            )
            return url
        except Exception as e:
            print(f"[StoryboardFrame] Scene {scene.get('scene_number')} frame failed: {e}")
            return None

    @staticmethod
    async def create_frame_job(scenes: list) -> str:
        job_id = uuid.uuid4().hex
        await _frame_jobs_collection().insert_one({
            "job_id": job_id,
            "status": "generating",
            "total_scenes": len(scenes),
            "frames": [],
        })
        return job_id

    @staticmethod
    async def run_frame_job(job_id: str, scenes: list, brand_images: List[str] = None) -> None:
        """Background task: generate one frame image per scene, store progressively."""
        col = _frame_jobs_collection()
        brand_images = brand_images or []
        for scene in scenes:
            url = await VideoStoryboardService._generate_scene_frame(scene, brand_images)
            if url:
                await col.update_one(
                    {"job_id": job_id},
                    {"$push": {"frames": {
                        "scene_number": scene.get("scene_number"),
                        "frame_image_url": url,
                    }}},
                )
        await col.update_one({"job_id": job_id}, {"$set": {"status": "complete"}})

    @staticmethod
    async def get_frame_job(job_id: str) -> Optional[Dict]:
        return await _frame_jobs_collection().find_one({"job_id": job_id}, {"_id": 0})

    @staticmethod
    async def generate_storyboard(
        brand_images: List[str],
        optional_text: Optional[str],
        brand_context: Dict[str, Any],
        target_platform: str = "instagram_reels",
        target_duration_seconds: int = 15,
    ) -> Dict[str, Any]:
        """
        Send brand images + optional creative text to GPT-4o Vision.
        Returns a structured storyboard JSON dict. Frame images are generated
        separately via the /generate-storyboard-frames background job.
        """
        if not brand_images:
            return {"status": False, "error": "At least one brand image is required."}

        brand_images = brand_images[:5]
        target_duration_seconds = max(5, min(target_duration_seconds, 30))
        num_scenes = max(1, round(target_duration_seconds / 5))

        brand_colors = brand_context.get("brand_colors") or []
        color_str = ", ".join(str(c) for c in brand_colors[:4]) if brand_colors else ""
        brand_name = brand_context.get("brand_name") or "this brand"
        industry = brand_context.get("industry") or "general"
        region = brand_context.get("region") or ""
        voice = brand_context.get("brand_voice") or ""
        platform_label = target_platform.replace("_", " ").title()

        preamble_lines = [
            f"Brand: {brand_name}",
            f"Industry: {industry}",
            f"Target platform: {platform_label}",
            f"Video length: {target_duration_seconds}s total | {num_scenes} scenes (~5s each)",
            f"Aspect ratio: 9:16 vertical",
        ]
        if color_str:
            preamble_lines.append(f"Brand colors (STRICT — must dominate every scene): {color_str}")
        if voice:
            preamble_lines.append(f"Brand voice: {voice}")
        if region:
            preamble_lines.append(f"Market/region: {region}")
        if optional_text and optional_text.strip():
            preamble_lines.append(f"\nCreative direction from marketer:\n{optional_text.strip()}")
        preamble_lines.append(
            f"\n{len(brand_images)} brand image(s) attached below (indices 0–{len(brand_images) - 1}). "
            "Study each carefully — they define the visual identity.\n"
            f"Generate exactly {num_scenes} scenes totalling {target_duration_seconds}s."
        )

        content: List[Dict] = [{"type": "text", "text": "\n".join(preamble_lines)}]

        for i, img_data in enumerate(brand_images):
            url = img_data if img_data.startswith("data:") else f"data:image/jpeg;base64,{img_data}"
            content.append({"type": "text", "text": f"Image {i} (use reference_image_index={i}):"})
            content.append({"type": "image_url", "image_url": {"url": url, "detail": "high"}})

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ]

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: openai_client.chat.completions.create(
                model="gpt-5.4",
                messages=messages,
                temperature=0.7,
                max_completion_tokens=2000,
            ),
        )

        raw = response.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        try:
            storyboard = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"Storyboard JSON parse error: {e}\nRaw: {raw[:300]}")
            return {"status": False, "error": "Failed to parse storyboard from model response."}

        return {"status": True, "storyboard": storyboard}

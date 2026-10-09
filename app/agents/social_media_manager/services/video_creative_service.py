import asyncio
import base64
import json
import re
from io import BytesIO
from typing import Any, Dict, List, Optional

import httpx

from app.services.AIService import client as openai_client
from app.utils.s3_upload import upload_bytes
from app.agents.social_media_manager.services.video_storyboard_service import (
    VideoStoryboardService,
    VIDEO_STYLE_DIRECTIVES,
    _frame_jobs_collection,
)

# Script + creative-direction writing for the "describe it" flow — a GPT text
# model, not gpt-image-2 (that one only generates/edits images). gpt-5.4
# matches the existing precedent for creative long-form text generation in
# this codebase (see auto_content_service.py's content-seed writer); unlike
# video_storyboard_service.py's Gemini Flash-Lite step (a grounded, "read
# these real photos and extract structured scenes" task), this one is an
# imaginative, from-a-brief task — a different shape that doesn't fit the
# same cheap-extraction justification, so it stays on a full text model.
SCRIPT_MODEL = "gpt-5.4"

_SYSTEM_PROMPT = """You are a creative director and scriptwriter for short-form social video ads.

You receive:
  1. A free-text brief describing what the video should be about.
  2. Optional reference images (brand/product/person photos) — there may be none at all; you are inventing the visual world from the brief, not grounded in real photos the way a photo-first storyboard tool would be.
  3. Brand context: name, industry, color palette, voice, region, target platform.
  4. A VIDEO STYLE directive — follow it strictly for camera, pacing, transitions, and energy.

Your job: invent a complete creative concept, then produce a JSON script that fully embodies the selected video style.

Rules:
- creative_direction: 2-4 sentences stating the overall concept — the story being told, the mood, the strategy, why it will work for this brief. Write this BEFORE the scenes, as the thing the scenes exist to serve.
- The brand color palette MUST dominate every scene — no other colors allowed.
- video_prompt fields must be a COMPLETE, self-sufficient visual description an image generator can draw from scratch — specify subject, setting, framing, lighting, and motion in full; never assume prior context.
- APPLY THE VIDEO STYLE DIRECTIVE to every scene's motion, video_prompt, and text_overlay decisions.
- reference_image_index: 0 if reference images were supplied and this scene draws on them, otherwise 0 (unused placeholder — frame generation for this flow chains scene-to-scene for consistency instead of picking by index).
- text_overlay is a short on-screen caption/tagline string, or null.
- shot_type must be one of: product_hero | lifestyle | brand_close_up | text_card | transition

CONTINUITY — the scenes are NOT independent moments. They are chapters of ONE
continuous story and must read that way end to end:
- The whole script needs a clear through-line: scene 1 hooks/sets up, the
  middle scenes build or demonstrate, the final scene pays off with a CTA or
  resolution — never a sequence of unrelated vignettes sharing only a style.
- Subject/setting continuity: once a scene establishes a specific subject,
  product, person, or setting, describe that SAME subject again in every
  later scene's video_prompt (full visual description, not a shorthand
  reference) so an image generator chaining from the previous scene's frame
  stays consistent — unless the narrative deliberately cuts away, which must
  be an intentional story beat, not a random switch.
- Each scene's motion/video_prompt should read as a natural continuation of
  the previous scene's action and energy, as if one camera/story is flowing
  forward, not disconnected clips stitched together after the fact.
- continuity_note: one short phrase stating how this scene follows directly
  from the previous one (for scene 1, how it sets up what follows).
- dialogue: ONLY when this video features a person speaking on camera
  (testimonial/direct-to-camera/talking-head content). Write ONE continuous
  spoken script for that speaker and split it naturally across scenes — scene
  1's words must lead directly into scene 2's, like one take cut into pieces,
  never separate unrelated lines. If no one speaks on camera in a scene, set
  dialogue to null.

Return ONLY valid JSON — no markdown fences, no explanation:
{
  "creative_direction": "<2-4 sentence overall creative concept>",
  "video_style": "<the exact style slug you picked, e.g. clean_commercial — echo it back even if it was given to you>",
  "total_duration_seconds": <int>,
  "target_platform": "<string>",
  "aspect_ratio": "9:16",
  "scenes": [
    {
      "scene_number": <int>,
      "duration_seconds": <int>,
      "shot_type": "<product_hero|lifestyle|brand_close_up|text_card|transition>",
      "motion": "<plain-English camera/subject motion description>",
      "video_prompt": "<complete, self-sufficient motion-aware prompt for the image/video model>",
      "reference_image_index": 0,
      "text_overlay": <string or null>,
      "continuity_note": "<how this scene follows the previous one>",
      "dialogue": <string spoken on camera this scene, or null>
    }
  ]
}"""


class VideoCreativeService:

    @staticmethod
    async def generate_creative_storyboard(
        brief: str,
        reference_images: List[str],
        brand_context: Dict[str, Any],
        target_platform: str = "instagram_reels",
        target_duration_seconds: int = 15,
        video_style: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Write a creative_direction + scene-by-scene script from a free-text
        brief (reference images optional). Frame images are generated
        separately and consistently via create_frame_job/run_frame_job below
        — once scenes carry frame_image_url, the existing outcome-routed
        fal.ai generation pipeline (video_generation_service.py) picks up
        from there completely unchanged.

        video_style: pass an explicit slug to force one (matches the upload-
        first flow's behavior); leave None (the describe-it UI's default) and
        the model picks whichever of VIDEO_STYLE_DIRECTIVES best fits the
        brief itself, returned in the storyboard's own "video_style" field so
        the choice is visible rather than a silent internal decision.
        """
        if not brief or not brief.strip():
            return {"status": False, "error": "Describe what the video should be about."}

        reference_images = reference_images[:5]
        target_duration_seconds = max(5, min(target_duration_seconds, 60))
        num_scenes = max(1, round(target_duration_seconds / 5))

        brand_colors = brand_context.get("brand_colors") or []
        color_str = ", ".join(str(c) for c in brand_colors[:4]) if brand_colors else ""
        brand_name = brand_context.get("brand_name") or "this brand"
        industry = brand_context.get("industry") or "general"
        region = brand_context.get("region") or ""
        voice = brand_context.get("brand_voice") or ""
        platform_label = target_platform.replace("_", " ").title()

        preamble_lines = [
            f"Brief: {brief.strip()}",
            f"Brand: {brand_name}",
            f"Industry: {industry}",
            f"Target platform: {platform_label}",
            f"Video length: {target_duration_seconds}s total | {num_scenes} scenes (~5s each)",
            "Aspect ratio: 9:16 vertical",
        ]
        if color_str:
            preamble_lines.append(f"Brand colors (STRICT — must dominate every scene): {color_str}")
        if voice:
            preamble_lines.append(f"Brand voice: {voice}")
        if region:
            preamble_lines.append(f"Market/region: {region}")
        if reference_images:
            preamble_lines.append(
                f"\n{len(reference_images)} reference image(s) attached below — use them to inform "
                "subject/product appearance, but you are still writing complete, self-sufficient "
                "video_prompt descriptions for each scene, not relying on the images being re-shown later."
            )
        else:
            preamble_lines.append(
                "\nNo reference images supplied — invent the entire visual world from the brief and brand context."
            )
        preamble_lines.append(f"\nGenerate exactly {num_scenes} scenes totalling {target_duration_seconds}s.")

        forced_style = VIDEO_STYLE_DIRECTIVES.get(video_style) if video_style else None
        if forced_style:
            system_prompt = f"{_SYSTEM_PROMPT}\n\n{forced_style}"
        else:
            # No style given — list every available style in full and let the
            # model choose whichever best fits the brief, applying only that
            # one's rules. Each block is tagged with its own slug so the model
            # can echo an exact, valid key back in "video_style" rather than
            # deriving one from prose.
            all_styles = "\n\n".join(
                f"STYLE SLUG: {slug}\n{directive}" for slug, directive in VIDEO_STYLE_DIRECTIVES.items()
            )
            system_prompt = (
                f"{_SYSTEM_PROMPT}\n\n"
                "VIDEO STYLE — CHOOSE ONE: no style was specified. Read the brief and brand "
                "context, pick exactly ONE of the styles below that best fits the brief's "
                "subject, tone, and goal, then apply ONLY that style's rules to every scene. "
                "Return its exact STYLE SLUG value as \"video_style\" in your JSON.\n\n"
                f"{all_styles}"
            )

        content: List[Dict[str, Any]] = [{"type": "text", "text": "\n".join(preamble_lines)}]
        for img in reference_images:
            content.append({"type": "image_url", "image_url": {"url": img}})

        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: openai_client.chat.completions.create(
                    model=SCRIPT_MODEL,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": content},
                    ],
                    temperature=0.8,
                    # gpt-5.4 rejects max_tokens outright — live-confirmed
                    # 2026-10-02: "Unsupported parameter: 'max_tokens' is not
                    # supported with this model. Use 'max_completion_tokens'
                    # instead." The one other gpt-5.4 call site in this
                    # codebase (auto_content_service.py) never hit this
                    # because it never passes a token limit at all. Doubled
                    # from 3000 — duration now goes up to 60s (12 scenes,
                    # same scene-count increase as video_storyboard_service.py).
                    max_completion_tokens=6000,
                    response_format={"type": "json_object"},
                ),
            )
        except Exception as e:
            print(f"[VideoCreativeService] script generation error: {type(e).__name__}: {e}", flush=True)
            return {"status": False, "error": "Failed to generate creative storyboard."}

        raw = (response.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        try:
            storyboard = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"Creative storyboard JSON parse error: {e}\nRaw: {raw[:300]}")
            return {"status": False, "error": "Failed to parse creative storyboard from model response."}

        # Forced style: trust our own value over whatever the model echoed.
        # Inferred style: trust the model's pick only if it's a real slug —
        # a hallucinated one would otherwise silently fall through to no
        # style directive at all on any later regeneration of this storyboard.
        if forced_style:
            storyboard["video_style"] = video_style
        elif storyboard.get("video_style") not in VIDEO_STYLE_DIRECTIVES:
            print(f"[VideoCreativeService] model returned unknown video_style "
                  f"{storyboard.get('video_style')!r}, defaulting to clean_commercial")
            storyboard["video_style"] = "clean_commercial"

        return {"status": True, "storyboard": storyboard}

    # ── Frame generation — reuses VideoStoryboardService's own job collection
    #    and shape (same storyboard_frame_jobs doc, same GET /storyboard-frame-
    #    job/{job_id} polling endpoint) so the frontend's existing polling code
    #    works unchanged. What differs is per-scene generation: instead of
    #    editing a fixed uploaded brand photo by index, each scene chains off
    #    the PREVIOUS scene's own generated frame (or a user reference image
    #    for scene 1, or a from-scratch generation if neither exists) — that
    #    chain is what keeps the subject/setting visually consistent across
    #    scenes when there's no single real photo to anchor every edit to. ──

    @staticmethod
    async def create_frame_job(scenes: list) -> str:
        return await VideoStoryboardService.create_frame_job(scenes)

    @staticmethod
    async def run_frame_job(job_id: str, scenes: list, reference_images: List[str] = None) -> None:
        col = _frame_jobs_collection()
        reference_images = reference_images or []
        prev_image_url: Optional[str] = None
        for scene in scenes:
            url = await VideoCreativeService._generate_scene_frame(scene, reference_images, prev_image_url)
            if url:
                prev_image_url = url
                await col.update_one(
                    {"job_id": job_id},
                    {"$push": {"frames": {
                        "scene_number": scene.get("scene_number"),
                        "frame_image_url": url,
                    }}},
                )
        await col.update_one({"job_id": job_id}, {"$set": {"status": "complete"}})

    @staticmethod
    async def _generate_scene_frame(
        scene: dict, reference_images: List[str], prev_image_url: Optional[str]
    ) -> Optional[str]:
        try:
            shot = scene.get("shot_type", "").replace("_", " ")
            video_prompt = scene.get("video_prompt", "")
            motion = scene.get("motion", "")
            continuity = scene.get("continuity_note") or ""
            text = scene.get("text_overlay") or ""

            prompt = (
                f"Cinematic storyboard frame, {shot} shot. "
                f"{video_prompt} "
                f"Camera movement: {motion}. "
                + (f"Continuity with the surrounding scenes: {continuity}. " if continuity else "")
                + (f'On-screen text: "{text}". ' if text else "")
                + "Photorealistic, dramatic lighting. Vertical 9:16 composition."
            )

            loop = asyncio.get_running_loop()

            if prev_image_url:
                # Chain off the previous scene's own output — this is what
                # keeps subject/setting/style consistent scene-to-scene when
                # there's no single real photo to anchor every edit to.
                img_bytes = await VideoCreativeService._download_image_bytes(prev_image_url)
                chained_prompt = (
                    "Keep the same subject, setting, and visual style as this image — "
                    "this is the next moment in the same continuous scene. " + prompt
                )
                resp = await loop.run_in_executor(
                    None,
                    lambda: openai_client.images.edit(
                        image=VideoCreativeService._as_file(img_bytes),
                        model="gpt-image-2",
                        prompt=chained_prompt,
                        n=1,
                        size="1024x1536",
                        quality="medium",
                    ),
                )
            elif reference_images:
                img_bytes = VideoStoryboardService._decode_brand_image(reference_images[0])
                grounded_prompt = (
                    "Keep the real subject, product, and visual identity from the reference "
                    "image exactly as shown. " + prompt
                )
                resp = await loop.run_in_executor(
                    None,
                    lambda: openai_client.images.edit(
                        image=VideoCreativeService._as_file(img_bytes),
                        model="gpt-image-2",
                        prompt=grounded_prompt,
                        n=1,
                        size="1024x1536",
                        quality="medium",
                    ),
                )
            else:
                resp = await loop.run_in_executor(
                    None,
                    lambda: openai_client.images.generate(
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
            print(f"[CreativeFrame] Scene {scene.get('scene_number')} frame failed: {e}")
            return None

    @staticmethod
    async def _download_image_bytes(url: str) -> bytes:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content

    @staticmethod
    def _as_file(img_bytes: bytes):
        f = BytesIO(img_bytes)
        f.name = "reference.png"
        return f

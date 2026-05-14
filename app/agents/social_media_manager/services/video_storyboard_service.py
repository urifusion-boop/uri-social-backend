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
  4. A VIDEO STYLE directive — follow it strictly for camera, pacing, transitions, and energy.

Your job: study the brand images carefully, then produce a JSON video storyboard that fully embodies the selected video style.

Rules:
- The brand color palette MUST dominate every scene — no other colors allowed.
- video_prompt fields must be motion-aware: describe exactly what moves, camera direction, speed, and lighting.
- APPLY THE VIDEO STYLE DIRECTIVE to every scene's motion, video_prompt, and text_overlay decisions.
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

VIDEO_STYLE_DIRECTIVES: Dict[str, str] = {
    "clean_commercial": """VIDEO STYLE — CLEAN COMMERCIAL:
Apply a polished, professional brand commercial aesthetic throughout.
• Camera: smooth dolly/slider moves, locked-off product beauty shots, subtle push-ins on hero product.
• Pacing: steady 3–5 second scenes with clean cuts on beats; no jump cuts.
• Color grading: bright, high-key lighting; pure whites, neutral backgrounds; brand colors as accents only.
• Composition: product centered with generous negative space; rule-of-thirds for lifestyle shots.
• Text overlays: clean sans-serif, minimal — tagline on final scene only.
• Transitions: cut or subtle cross-dissolve only; no wipes or zoom transitions.
• Energy: confident and calm — trust-building, not hype.""",

    "luxury_slow_burn": """VIDEO STYLE — LUXURY SLOW BURN:
Apply a cinematic, high-fashion luxury aesthetic throughout.
• Camera: extreme macro close-ups on product texture and detail; slow push-ins (0.2× speed); deliberate rack-focus between foreground and background; shallow depth-of-field.
• Pacing: long 5–8 second scenes; let moments breathe; no rush between cuts.
• Color grading: deep shadows, rich mid-tones; desaturated except brand color accents; warm gold or cool silver tones depending on brand palette.
• Lighting: dramatic side-lighting, rim lighting on product; intentional lens flare.
• Text overlays: sparse — one word or short phrase maximum; elegant serif or ultra-thin typeface.
• Transitions: smooth fades, slow dissolves — never hard cuts.
• Energy: aspirational tension — make the viewer crave the product.""",

    "viral_fast_cut": """VIDEO STYLE — VIRAL FAST CUT:
Apply a high-energy, social-native fast-cut aesthetic throughout.
• Camera: handheld energy, quick zoom-ins, snap pans, 360° spins around product; unpredictable angles keep it fresh.
• Pacing: cut every 0.5–1.5 seconds; match cuts to a strong beat; first 2 seconds must hook immediately.
• Color grading: saturated, punchy, high contrast; brand colors popped to maximum vibrancy.
• Composition: product fills the frame; tight crops; exaggerated close-ups.
• Text overlays: bold, large, center-frame; 1–3 words per scene; animated (pop-on or slide-in).
• Transitions: whip pans, zoom transitions, glitch cuts — high kinetic energy.
• Energy: chaotic-fun — surprise the viewer in every scene.""",

    "ingredient_reveal": """VIDEO STYLE — INGREDIENT REVEAL:
Build the video as a progressive sensory reveal of raw materials and process.
• Camera: extreme macro close-ups on textures (grain, liquid, powder); slow overhead pours and sprinkles; sequential flat-lay reveals with items entering frame one by one.
• Pacing: each scene introduces one new element; build anticipation then cut to hero product.
• Color grading: warm, natural tones; enhance material colors without oversaturation; clean white or natural wood backgrounds.
• Text overlays: ingredient name on each reveal scene in a minimal label style.
• Transitions: quick cuts on textural peak moments; overhead-to-product cut at the end.
• Energy: curiosity and appetite-driven — make each ingredient irresistible.""",

    "street_style": """VIDEO STYLE — STREET STYLE:
Apply an authentic urban documentary aesthetic throughout.
• Camera: handheld with intentional natural shake; following shots behind talent; low street-level angles looking up; candid-feel framing.
• Pacing: natural rhythm — not every cut is on a beat; let real-life moments dictate timing.
• Color grading: gritty, slightly desaturated; pushed blacks; urban color palette (concrete, neon accents); film-grain texture.
• Composition: environment tells the story — include urban context, not just product.
• Text overlays: minimal; street-sign aesthetic or no-frills bold text if used.
• Transitions: hard cuts, match cuts on movement — never fancy effects.
• Energy: real and unfiltered — authenticity over perfection.""",

    "unboxing_drama": """VIDEO STYLE — UNBOXING DRAMA:
Structure every scene as a theatrical build-up and reveal sequence.
• Camera: tight close-up on hands interacting with packaging; slow controlled lift reveals; cutaway to product face-on at reveal; reaction-style angle on the "wow" moment.
• Pacing: deliberate slow build (3–4s) → snappy reveal cut → linger on hero product (2–3s).
• Color grading: slightly dark and moody during anticipation; brightness increases sharply at reveal.
• Lighting: spotlight-style on packaging and product; dark background to focus attention.
• Text overlays: "What's inside?" style teaser text; product name on reveal; value proposition on final scene.
• Transitions: hard cut on reveal moment for maximum impact.
• Energy: mount tension then release it — make the reveal unforgettable.""",

    "before_after": """VIDEO STYLE — BEFORE / AFTER:
Build the video as a clear transformation narrative — problem then solution.
• Camera: mirror the exact same angle and framing for "before" and "after" shots; dolly in on the "after" to signal improvement.
• Pacing: first half shows the problem (slightly slower, muted energy); second half shows the result (brighter, faster energy).
• Color grading: desaturated/cooler tones for "before" scenes; warm, saturated, vibrant tones for "after" scenes.
• Composition: side-by-side split frame or sequential cut at the midpoint; clear visual contrast.
• Text overlays: "Before" label and "After" label at transformation moment; result stat or claim on final scene.
• Transitions: dramatic wipe or flash-cut between before and after.
• Energy: shift from tension to satisfaction — resolution is the emotional payoff.""",

    "mood_film": """VIDEO STYLE — MOOD FILM:
Prioritise atmosphere and emotional resonance over product information.
• Camera: wide establishing shots to set mood; slow floating moves (drone-style or slider); golden-hour and blue-hour lighting; silhouette moments; reflections and natural light play.
• Pacing: long unhurried scenes (4–6s); silence and space are assets; let the emotion arrive slowly.
• Color grading: rich, cinematic LUT-style; warm amber and rose for golden-hour scenes; deep teals in shadows; anamorphic lens flare where appropriate.
• Composition: negative space is intentional; talent or product is small against a large beautiful environment.
• Text overlays: one evocative word or brand tagline only; fade in gently.
• Transitions: slow cross-dissolves; long fades to black between major moments.
• Energy: contemplative and aspirational — sell a feeling, not a feature.""",

    "product_explosion": """VIDEO STYLE — PRODUCT EXPLOSION:
Use dynamic, physics-defying visuals to make the product look spectacular.
• Camera: orbiting 360° rotation around the hero product; extreme close-up fly-throughs along product surface; dramatic upward reveal from below; slow-motion splashes or bursts of brand-colored particles.
• Pacing: fast movement with sudden freeze-frames on the most dramatic angles.
• Color grading: high contrast, deep blacks, hyper-saturated brand colors; light trails and glow effects.
• Composition: product isolated against solid dark or gradient backgrounds; multiple product angles layered.
• Text overlays: product name in bold as a climactic reveal; key feature callouts with short animated labels.
• Transitions: instant cut at peak energy moments; burst/flash transitions.
• Energy: maximum visual spectacle — this product deserves to be shown off.""",

    "testimonial_style": """VIDEO STYLE — TESTIMONIAL STYLE:
Create the feel of an authentic user-generated or interview-style testimonial.
• Camera: talking-head framing (person at slight angle, not dead-center); subtle natural reframe mid-scene; background shows real-life context relevant to the product.
• Pacing: conversational rhythm — let spoken cadence guide cuts; natural pauses are okay.
• Color grading: warm, natural, slightly faded — feels real not over-produced; avoid heavy LUTs.
• Lighting: natural window light or simple ring-light feel; imperfect is fine.
• Composition: person occupies 60% of frame; product visible but not forced into foreground.
• Text overlays: quote highlights as bold lower-thirds; result stat or name/title for social proof.
• Transitions: jump cuts (intentionally casual) or simple cross-dissolve.
• Energy: honest and relatable — the viewer should think "that could be me." """,

    "menu_showcase": """VIDEO STYLE — MENU SHOWCASE:
Make every scene look so good the viewer gets hungry.
• Camera: overhead flat-lay for composition shots; 45° "glamour angle" for plated dishes; slow close-up pull-back revealing the full dish; extreme macro on textures (sauce drizzle, cheese pull, steam rising).
• Pacing: linger 2–3s on the most appetizing textures; quick cut away just before it becomes static.
• Color grading: warm, saturated food tones — enhance reds and ambers; clean white plates; lush green garnishes.
• Lighting: soft overhead with subtle rim light; use steam and condensation as visual elements.
• Composition: food fills 80% of the frame; minimal props.
• Text overlays: dish name in an elegant script or bold sans; "Order now" CTA on final scene.
• Transitions: match cut from raw ingredient to finished dish; wipe on a plating action.
• Energy: indulgent and irresistible — trigger appetite, then provide the CTA.""",

    "countdown_hype": """VIDEO STYLE — COUNTDOWN HYPE:
Build urgency and excitement that escalates scene by scene.
• Camera: each scene zooms in slightly tighter than the last — start wide, end extreme close-up on product; quick snap zooms on key moments.
• Pacing: deliberately increase cut speed across scenes (scene 1: 4s, scene 2: 3s, scene 3: 2s, final: 1s flash); final scene holds on product.
• Color grading: pumped saturation; brand colors pushed to max; subtle red/warm vignette to signal urgency.
• Composition: countdown number or timer element visible in corner; product dominant in frame.
• Text overlays: bold countdown numbers each scene ("3…", "2…", "1…"); CTA on final frame ("Available now", "Limited drop", "Shop today").
• Transitions: fast whip cuts, flash frames between countdown moments.
• Energy: builds to a peak — the viewer must feel compelled to act immediately.""",
}


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
        video_style: Optional[str] = "clean_commercial",
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

        style_directive = VIDEO_STYLE_DIRECTIVES.get(video_style or "clean_commercial", "")
        system_prompt = _SYSTEM_PROMPT
        if style_directive:
            system_prompt = f"{_SYSTEM_PROMPT}\n\n{style_directive}"

        content: List[Dict] = [{"type": "text", "text": "\n".join(preamble_lines)}]

        for i, img_data in enumerate(brand_images):
            url = img_data if img_data.startswith("data:") else f"data:image/jpeg;base64,{img_data}"
            content.append({"type": "text", "text": f"Image {i} (use reference_image_index={i}):"})
            content.append({"type": "image_url", "image_url": {"url": url, "detail": "high"}})

        messages = [
            {"role": "system", "content": system_prompt},
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

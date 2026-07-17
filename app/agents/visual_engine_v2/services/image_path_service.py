"""
Image Path Service - Visual Engine V2
Handles Path A (generate imagery-only) and Path B (upload + cleanup)

PRD Section 8: Imagery Layer Routes
- Path A: GPT Image 2 generates imagery-only (no text/brand)
- Path B: User uploads image + background removal
"""

from typing import Dict, List, Optional
from PIL import Image
import io
import base64
import asyncio
import httpx
from openai import AsyncOpenAI

from app.agents.visual_engine_v2.config.vendor_config import VendorConfig
from app.utils.cloudinary_upload import upload_bytes


class ImageGenerationError(Exception):
    """Raised when Path A imagery generation fails after its retry, per PRD Section 12."""
    pass


# Shared by _resize_to_format and _smart_crop_to_format so the two crop
# strategies always target the exact same output dimensions.
_ASPECT_RATIOS = {"1:1": 1.0, "4:5": 0.8, "9:16": 0.5625}
_SIZE_MAP = {"1:1": (1024, 1024), "4:5": (1024, 1280), "9:16": (1024, 1792)}


class ImagePathService:
    """
    Handles imagery generation and upload cleanup for Visual Engine V2.

    PRD Section 8: The Imagery Layer is ONLY about the background visual.
    No text, no logo, no brand elements - just pure imagery.
    """

    def __init__(self, openai_client: AsyncOpenAI):
        self.openai_client = openai_client
        self.vendor_config = VendorConfig()

    async def generate_imagery_path_a(
        self,
        content_plan: str,
        style_hint: Optional[str] = None,
        format: str = "1:1",
        negative_space: str = "left_third"
    ) -> Dict[str, str]:
        """
        Path A: Generate imagery-only using GPT Image 2.

        Args:
            content_plan: The content text (used for visual context)
            style_hint: Optional style guidance from brand profile
            format: "1:1", "4:5", or "9:16"
            negative_space: where to keep the composition clean for the
                template to later overlay text/logo — "left_third",
                "right_third", "top_third", or "bottom_third"

        Returns:
            {
                "imagery_url": "https://cloudinary.../imagery.png",
                "path": "A",
                "cost": 0.04
            }
        """
        # Map format to the sizes GPT Image 2 actually supports (square, portrait, landscape)
        dimension_map = {
            "1:1": "1024x1024",
            "4:5": "1024x1536",
            "9:16": "1024x1536"
        }
        size = dimension_map.get(format, "1024x1024")

        # Build imagery-only prompt (no text, no logo, no brand elements)
        prompt = self._build_imagery_prompt(content_plan, style_hint, negative_space)

        # PRD Section 12: "retry once with the same brief" before treating this as a failure
        last_error: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                print(f"🎨 [Path A] Generating imagery-only with GPT Image 2 ({size}), attempt {attempt}/2")
                response = await self.openai_client.images.generate(
                    model="gpt-image-2",
                    prompt=prompt,
                    size=size,
                    quality="high",
                    n=1
                )

                img_bytes = base64.b64decode(response.data[0].b64_json)

                cloudinary_url = await upload_bytes(
                    img_bytes,
                    folder="visual_engine_v2/imagery",
                    resource_type="image"
                )

                print(f"✅ [Path A] Imagery generated: {cloudinary_url[:80]}...")

                return {
                    "imagery_url": cloudinary_url,
                    "path": "A",
                    "cost": self.vendor_config.cost_model.image_generation_cost
                }
            except Exception as e:
                last_error = e
                print(f"⚠️ [Path A] Attempt {attempt}/2 failed: {e}")
                if attempt == 1:
                    await asyncio.sleep(1)

        raise ImageGenerationError(f"Path A generation failed after retry: {last_error}")

    async def generate_carousel_imagery_path_a(
        self,
        image_briefs: List[str],
        brand_primary: Optional[str] = None,
        style_hint: Optional[str] = None,
        format: str = "1:1",
        negative_space: str = "left_third"
    ) -> List[Dict[str, str]]:
        """
        PRD Section 9.1: "each slide's image slot is filled independently —
        Path A generates an image per slide from that slide's brief." Runs
        every slide's generation concurrently rather than one image reused
        across the whole carousel.

        A single slide's generation failing (both retries exhausted) doesn't
        drop the rest of the carousel — that slide gets a brand-colored
        placeholder and is flagged needs_attention, matching the same
        degrade-don't-drop behavior single-post Path A already has.
        """
        print(f"🎨 [Path A] Generating {len(image_briefs)} independent carousel slide images")

        async def _generate_one(brief: str) -> Dict[str, str]:
            try:
                return await self.generate_imagery_path_a(
                    content_plan=brief, style_hint=style_hint, format=format, negative_space=negative_space
                )
            except ImageGenerationError as e:
                print(f"⚠️ [Path A] Carousel slide image failed, using placeholder: {e}")
                result = await self.generate_placeholder_image(brand_primary, format=format)
                return {**result, "needs_attention": True}

        return await asyncio.gather(*[_generate_one(brief) for brief in image_briefs])

    # Carousel Path B upload cleanup (per-image, with the PRD Section 10.2
    # cleaned-image cache and the "repeat the last photo" padding rule) lives
    # in the router's _job_carousel_upload_images — it needs `db` for the
    # cache lookup, which this DB-agnostic service deliberately doesn't hold.

    @staticmethod
    async def generate_placeholder_image(brand_primary: Optional[str], format: str = "1:1") -> Dict[str, str]:
        """
        PRD Section 12 fallback: when imagery/rendering fails even after a retry,
        produce a brand-colored placeholder rather than posting nothing.
        """
        width, height = _SIZE_MAP.get(format, (1024, 1024))

        color = (brand_primary or "#CCCCCC").strip()
        if not color.startswith("#"):
            color = "#CCCCCC"

        placeholder = Image.new("RGB", (width, height), color=color)
        buffer = io.BytesIO()
        placeholder.save(buffer, format="PNG")
        buffer.seek(0)

        cloudinary_url = await upload_bytes(
            buffer.read(),
            folder="visual_engine_v2/placeholders",
            resource_type="image"
        )

        return {
            "imagery_url": cloudinary_url,
            "path": "placeholder",
            "cost": 0.0
        }

    async def process_uploaded_image_path_b(
        self,
        image_data: bytes,
        remove_background: bool = False,
        format: str = "1:1",
        cleanup_level: str = "background_removal"
    ) -> Dict[str, str]:
        """
        Path B: Process user-uploaded image with optional background removal.

        Args:
            image_data: Raw image bytes from user upload
            remove_background: Whether to remove background (default: False)
            format: Target format ("1:1", "4:5", "9:16")
            cleanup_level: "reframe" uses content-aware smart cropping
                (_smart_crop_to_format) instead of a blind center-crop;
                every other level uses the plain center-crop.

        Returns:
            {
                "imagery_url": "https://cloudinary.../imagery.png",
                "path": "B",
                "cost": 0.015  # BG removal cost
            }
        """
        print(f"📤 [Path B] Processing uploaded image (remove_bg={remove_background}, cleanup_level={cleanup_level})")

        # Load image
        img = Image.open(io.BytesIO(image_data))

        # Resize/crop to target format — "reframe" gets a content-aware crop,
        # everything else keeps the plain center-crop that's always run.
        if cleanup_level == "reframe":
            img = self._smart_crop_to_format(img, format)
        else:
            img = self._resize_to_format(img, format)

        # Background removal if requested
        cost = 0.0
        if remove_background:
            img, removal_cost = await self._remove_background(img)
            cost += removal_cost

        # Convert to bytes
        output = io.BytesIO()
        img.save(output, format="PNG")
        output.seek(0)
        final_bytes = output.read()

        # Upload to Cloudinary
        cloudinary_url = await upload_bytes(
            final_bytes,
            folder="visual_engine_v2/imagery",
            resource_type="image"
        )

        print(f"✅ [Path B] Image processed: {cloudinary_url[:80]}...")

        return {
            "imagery_url": cloudinary_url,
            "path": "B",
            "cost": cost
        }

    async def process_uploaded_image_path_b_recomposite(
        self,
        image_data: bytes,
        content_plan: str,
        style_hint: Optional[str] = None,
        format: str = "1:1"
    ) -> Dict[str, str]:
        """
        Path B, AI re-compositing (PRD Section 8, premium/opt-in): preserve
        the real product pixel-for-pixel, replace only what's around it.

        Deterministic by construction rather than trusting an image-edit
        model to leave the product untouched: cut the product out via
        background removal (the product's own pixels are never sent through
        an image model), generate a brand-new scene with GPT Image 2 (the
        same machinery Path A uses), then paste the untouched cutout onto
        that new scene. A whole-photo edit-mode call can't actually
        guarantee "preserve the product exactly" — a hard pixel boundary can.

        Deliberately not cached in ImageCacheServiceV2: unlike a plain
        cleanup, the output here depends on content_plan (the new scene's
        brief), not just the raw upload — the same photo re-composited for a
        different post should get a different scene, not a stale cache hit.
        """
        print("🎨 [Path B] AI re-compositing: cutting out product + generating new scene")

        # 1. Cut the product out at full original resolution — before any
        # resize, so the cutout keeps maximum real detail.
        original = Image.open(io.BytesIO(image_data)).convert("RGBA")
        cutout, removal_cost = await self._remove_background(original)
        cutout = cutout.convert("RGBA")

        # 2. Generate a brand-new scene — imagery-only, no product/text baked
        # in — at this target format, reusing Path A's own retry/fallback logic.
        scene_result = await self.generate_imagery_path_a(
            content_plan=content_plan, style_hint=style_hint, format=format, negative_space="left_third"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            scene_resp = await client.get(scene_result["imagery_url"])
            scene_resp.raise_for_status()
        scene = Image.open(io.BytesIO(scene_resp.content)).convert("RGBA")
        scene = self._resize_to_format(scene, format)

        # 3. Composite the untouched cutout onto the new scene: centered
        # horizontally, sitting near the bottom third like a real product shot.
        target_w, target_h = scene.size
        cw, ch = cutout.size
        if cw and ch:
            max_h = int(target_h * 0.72)
            max_w = int(target_w * 0.85)
            scale = min(max_h / ch, max_w / cw)
            if scale != 1.0:
                cutout = cutout.resize((max(1, int(cw * scale)), max(1, int(ch * scale))), Image.Resampling.LANCZOS)
            cw, ch = cutout.size
            paste_x = (target_w - cw) // 2
            paste_y = target_h - ch - int(target_h * 0.06)
            scene.paste(cutout, (paste_x, paste_y), cutout)

        output = io.BytesIO()
        scene.convert("RGB").save(output, format="PNG")
        output.seek(0)

        cloudinary_url = await upload_bytes(output.read(), folder="visual_engine_v2/imagery", resource_type="image")
        total_cost = removal_cost + scene_result["cost"]

        print(f"✅ [Path B] AI re-composited: {cloudinary_url[:80]}... (cost=${total_cost:.3f})")
        return {"imagery_url": cloudinary_url, "path": "B", "cost": total_cost}

    def _build_imagery_prompt(
        self, content_plan: str, style_hint: Optional[str], negative_space: str = "left_third"
    ) -> str:
        """
        Build imagery-only prompt for GPT Image 2.

        CRITICAL: No text, no logo, no brand elements.
        Pure background imagery only.

        PRD Section 5.1: the template will overlay text/logo on top of this
        image afterward, so the prompt must reserve a clean, uncluttered area
        for it — otherwise generated subjects/detail collide with the
        template's text block.
        """
        negative_space_phrases = {
            "left_third": "an uncluttered area down the left third of the frame",
            "right_third": "an uncluttered area down the right third of the frame",
            "top_third": "an uncluttered area across the top third of the frame",
            "bottom_third": "an uncluttered area across the bottom third of the frame",
        }
        space_phrase = negative_space_phrases.get(negative_space, negative_space_phrases["left_third"])

        base_prompt = f"""Create a high-quality background image for social media.

Content context: {content_plan[:200]}

REQUIREMENTS:
- NO text, words, or letters
- NO logos or brand elements
- NO product labels or signage
- Pure background imagery only
- Professional, clean aesthetic
- Visually appealing composition
- Leave {space_phrase} empty and simple — this is where a template will
  overlay text and a logo afterward. Keep the main subject/focal point out
  of that area so the overlaid text stays readable against a clean
  background there."""

        if style_hint:
            base_prompt += f"\n- Style: {style_hint}"

        return base_prompt

    def _resize_to_format(self, img: Image.Image, format: str) -> Image.Image:
        """
        Resize/crop image to target format maintaining aspect ratio (blind
        center-crop — used for every cleanup_level except "reframe").
        """
        target_ratio = _ASPECT_RATIOS.get(format, 1.0)

        # Calculate crop dimensions
        current_ratio = img.width / img.height

        if current_ratio > target_ratio:
            # Too wide - crop width
            new_width = int(img.height * target_ratio)
            left = (img.width - new_width) // 2
            img = img.crop((left, 0, left + new_width, img.height))
        elif current_ratio < target_ratio:
            # Too tall - crop height
            new_height = int(img.width / target_ratio)
            top = (img.height - new_height) // 2
            img = img.crop((0, top, img.width, top + new_height))

        target_size = _SIZE_MAP.get(format, (1024, 1024))
        img = img.resize(target_size, Image.Resampling.LANCZOS)

        return img

    def _smart_crop_to_format(self, img: Image.Image, format: str) -> Image.Image:
        """
        Content-aware crop for the "reframe" cleanup level (PRD Section 5.2):
        instead of always cropping to center, slide the crop window along
        the axis being cut and keep the position with the most edge detail —
        the product/subject is far more likely to sit where the image is
        visually "busy" than in a flat, empty margin a blind center-crop
        might keep instead. Falls back to the plain center-crop on any error.
        """
        target_ratio = _ASPECT_RATIOS.get(format, 1.0)
        current_ratio = img.width / img.height

        try:
            from PIL import ImageFilter, ImageStat
            edges = img.convert("L").filter(ImageFilter.FIND_EDGES)

            if current_ratio > target_ratio:
                new_width = int(img.height * target_ratio)
                max_left = img.width - new_width
                if max_left <= 0:
                    img = img.crop((0, 0, new_width, img.height))
                else:
                    steps = min(12, max_left)
                    best_left, best_score = 0, -1.0
                    for i in range(steps + 1):
                        left = int(max_left * i / steps)
                        score = ImageStat.Stat(edges.crop((left, 0, left + new_width, img.height))).sum[0]
                        if score > best_score:
                            best_score, best_left = score, left
                    img = img.crop((best_left, 0, best_left + new_width, img.height))
            elif current_ratio < target_ratio:
                new_height = int(img.width / target_ratio)
                max_top = img.height - new_height
                if max_top <= 0:
                    img = img.crop((0, 0, img.width, new_height))
                else:
                    steps = min(12, max_top)
                    best_top, best_score = 0, -1.0
                    for i in range(steps + 1):
                        top = int(max_top * i / steps)
                        score = ImageStat.Stat(edges.crop((0, top, img.width, top + new_height))).sum[0]
                        if score > best_score:
                            best_score, best_top = score, top
                    img = img.crop((0, best_top, img.width, best_top + new_height))
        except Exception as e:
            print(f"⚠️ Smart crop failed, falling back to center-crop: {e}")
            return self._resize_to_format(img, format)

        target_size = _SIZE_MAP.get(format, (1024, 1024))
        return img.resize(target_size, Image.Resampling.LANCZOS)

    async def _remove_background(self, img: Image.Image) -> tuple[Image.Image, float]:
        """
        Remove background using remove.bg API.

        Returns:
            (processed_image, cost)
        """
        # Convert image to bytes
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)
        img_bytes = buffer.read()

        # Encode to base64
        img_base64 = base64.b64encode(img_bytes).decode()

        print("🔧 Removing background with remove.bg...")

        # Call remove.bg API
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.remove.bg/v1.0/removebg",
                data={
                    "image_file_b64": img_base64,
                    "size": "auto",
                    "format": "png"
                },
                headers={"X-Api-Key": self.vendor_config.removebg_api_key}
            )

        if response.status_code != 200:
            print(f"⚠️ Background removal failed: {response.text}")
            # Return original image if removal fails
            return img, 0.0

        # Load processed image
        processed_bytes = response.content
        processed_img = Image.open(io.BytesIO(processed_bytes))

        cost = self.vendor_config.cost_model.background_removal_cost
        print(f"✅ Background removed (cost: ${cost:.3f})")

        return processed_img, cost

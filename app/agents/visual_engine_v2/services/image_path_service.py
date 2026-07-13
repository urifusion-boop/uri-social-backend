"""
Image Path Service - Visual Engine V2
Handles Path A (generate imagery-only) and Path B (upload + cleanup)

PRD Section 8: Imagery Layer Routes
- Path A: GPT Image 2 generates imagery-only (no text/brand)
- Path B: User uploads image + background removal
"""

from typing import Dict, Optional
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
        format: str = "1:1"
    ) -> Dict[str, str]:
        """
        Path A: Generate imagery-only using GPT Image 2.

        Args:
            content_plan: The content text (used for visual context)
            style_hint: Optional style guidance from brand profile
            format: "1:1", "4:5", or "9:16"

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
        prompt = self._build_imagery_prompt(content_plan, style_hint)

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

    @staticmethod
    async def generate_placeholder_image(brand_primary: Optional[str], format: str = "1:1") -> Dict[str, str]:
        """
        PRD Section 12 fallback: when imagery/rendering fails even after a retry,
        produce a brand-colored placeholder rather than posting nothing.
        """
        size_map = {
            "1:1": (1024, 1024),
            "4:5": (1024, 1280),
            "9:16": (1024, 1792)
        }
        width, height = size_map.get(format, (1024, 1024))

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
        format: str = "1:1"
    ) -> Dict[str, str]:
        """
        Path B: Process user-uploaded image with optional background removal.

        Args:
            image_data: Raw image bytes from user upload
            remove_background: Whether to remove background (default: False)
            format: Target format ("1:1", "4:5", "9:16")

        Returns:
            {
                "imagery_url": "https://cloudinary.../imagery.png",
                "path": "B",
                "cost": 0.015  # BG removal cost
            }
        """
        print(f"📤 [Path B] Processing uploaded image (remove_bg={remove_background})")

        # Load image
        img = Image.open(io.BytesIO(image_data))

        # Resize/crop to target format
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

    def _build_imagery_prompt(self, content_plan: str, style_hint: Optional[str]) -> str:
        """
        Build imagery-only prompt for GPT Image 2.

        CRITICAL: No text, no logo, no brand elements.
        Pure background imagery only.
        """
        base_prompt = f"""Create a high-quality background image for social media.

Content context: {content_plan[:200]}

REQUIREMENTS:
- NO text, words, or letters
- NO logos or brand elements
- NO product labels or signage
- Pure background imagery only
- Professional, clean aesthetic
- Visually appealing composition"""

        if style_hint:
            base_prompt += f"\n- Style: {style_hint}"

        return base_prompt

    def _resize_to_format(self, img: Image.Image, format: str) -> Image.Image:
        """
        Resize/crop image to target format maintaining aspect ratio.
        """
        aspect_ratios = {
            "1:1": 1.0,      # 1024x1024
            "4:5": 0.8,      # 1024x1280
            "9:16": 0.5625   # 1024x1792
        }

        target_ratio = aspect_ratios.get(format, 1.0)

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

        # Resize to standard dimensions
        size_map = {
            "1:1": (1024, 1024),
            "4:5": (1024, 1280),
            "9:16": (1024, 1792)
        }
        target_size = size_map.get(format, (1024, 1024))
        img = img.resize(target_size, Image.Resampling.LANCZOS)

        return img

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

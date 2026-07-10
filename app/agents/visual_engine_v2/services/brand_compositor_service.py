"""
Brand Compositor Service - Visual Engine V2
The core 4-layer compositing engine

PRD Section 11: Four-Layer Architecture
Layer 1: Content (AI text)
Layer 2: Imagery (GPT Image 2 or user upload)
Layer 3: Brand (exact colors, logo, fonts)
Layer 4: Typesetting (template-fill render)
"""

from typing import Dict, List, Optional
from datetime import datetime
from bson import ObjectId

from app.agents.visual_engine_v2.models.visual_engine_models import (
    VisualEngineRenderV2,
    LayerData
)
from app.agents.visual_engine_v2.services.template_service import TemplateService
from app.agents.visual_engine_v2.config.template_config import TemplateConfig
from app.utils.cloudinary_upload import upload_bytes
import httpx


class BrandCompositorService:
    """
    Orchestrates the 4-layer compositing process:
    Content → Imagery → Brand → Typesetting → Final Render
    """

    def __init__(self, db):
        self.db = db
        self.template_service = TemplateService()
        self.template_config = TemplateConfig()

    async def compose_final_render(
        self,
        user_id: str,
        brand_profile_id: str,
        content_layer: LayerData,
        imagery_layer: LayerData,
        format: str = "1:1",
        carousel_count: int = 1
    ) -> VisualEngineRenderV2:
        """
        Main composition pipeline: 4-layer → Final render.

        Args:
            user_id: User ID
            brand_profile_id: Brand profile ID
            content_layer: Layer 1 (AI text: headline, subtext, cta)
            imagery_layer: Layer 2 (imagery URL + metadata)
            format: "1:1", "4:5", or "9:16"
            carousel_count: Number of carousel slides (1 = single post)

        Returns:
            VisualEngineRenderV2 document ready to save to DB
        """
        print(f"🎬 Starting 4-layer composition (format={format}, carousel={carousel_count})")

        # Layer 3: Extract brand context from database
        brand_layer = await self._extract_brand_layer(brand_profile_id)

        # Layer 4: Select template and render
        typesetting_layer = await self._render_typesetting_layer(
            content_layer=content_layer,
            imagery_layer=imagery_layer,
            brand_layer=brand_layer,
            format=format,
            carousel_count=carousel_count
        )

        # Build final render document
        render = VisualEngineRenderV2(
            user_id=user_id,
            brand_profile_id=brand_profile_id,
            content_layer=content_layer,
            imagery_layer=imagery_layer,
            brand_layer=brand_layer,
            typesetting_layer=typesetting_layer,
            final_outputs=typesetting_layer.rendered_urls,
            status="completed",
            created_at=datetime.utcnow(),
            total_cost=self._calculate_total_cost(content_layer, imagery_layer, typesetting_layer)
        )

        print(f"✅ 4-layer composition complete: {len(render.final_outputs)} output(s)")
        return render

    async def _extract_brand_layer(self, brand_profile_id: str) -> LayerData:
        """
        Layer 3: Extract exact brand assets from database.

        PRD Section 10: Brand Layer
        - Exact hex colors from database
        - Logo URL
        - Font family names
        - No AI interpretation - exact values only
        """
        print("🏢 Extracting Layer 3: Brand assets")

        brand_profile = await self.db["brand_profiles"].find_one(
            {"_id": ObjectId(brand_profile_id)}
        )

        if not brand_profile:
            raise ValueError(f"Brand profile not found: {brand_profile_id}")

        # Extract exact brand values (no defaults, no AI interpretation)
        brand_data = {
            "brand_name": brand_profile.get("brand_name", ""),
            "logo_url": brand_profile.get("logo_url"),
            "logo_position": brand_profile.get("logo_position", "bottom_right"),

            # Exact colors from database
            "primary_color": brand_profile.get("primary_color"),
            "secondary_color": brand_profile.get("secondary_color"),
            "accent_color": brand_profile.get("accent_color"),
            "background_color": brand_profile.get("background_color"),

            # Font families
            "primary_font": brand_profile.get("primary_font"),
            "secondary_font": brand_profile.get("secondary_font"),

            # Brand metadata
            "industry": brand_profile.get("industry"),
            "voice_tone": brand_profile.get("voice_profile", {}).get("tone", [])
        }

        print(f"✓ Brand Layer: {brand_data['brand_name']}, Logo: {bool(brand_data['logo_url'])}, Colors: {bool(brand_data['primary_color'])}")

        return LayerData(
            layer_type="brand",
            data=brand_data,
            metadata={"source": "database", "brand_profile_id": brand_profile_id}
        )

    async def _render_typesetting_layer(
        self,
        content_layer: LayerData,
        imagery_layer: LayerData,
        brand_layer: LayerData,
        format: str,
        carousel_count: int
    ) -> LayerData:
        """
        Layer 4: Template-fill rendering (Orshot/Placid).

        PRD Section 12: Typesetting Layer
        - Select template based on style/intent/format
        - Fill template with Layers 1-3 data
        - Render via Orshot (primary) or Placid (fallback)
        """
        print(f"🎨 Rendering Layer 4: Typesetting (carousel={carousel_count})")

        content_data = content_layer.data
        imagery_data = imagery_layer.data
        brand_data = brand_layer.data

        # Determine post intent from content metadata
        post_intent = content_layer.metadata.get("post_intent", "announcement")
        style_family = brand_data.get("style_family", "modern")
        image_path = imagery_layer.metadata.get("path", "both")  # "A", "B", or "both"

        # Select template
        template_id = self.template_config.select_template(
            style_family=style_family,
            post_intent=post_intent,
            format=format,
            image_path=image_path
        )

        print(f"📋 Selected template: {template_id} (intent={post_intent}, format={format})")

        # Prepare template data (merge all 3 layers)
        template_data = {
            # Layer 1: Content
            "headline": content_data.get("headline", ""),
            "subtext": content_data.get("subtext", ""),
            "cta": content_data.get("cta", ""),

            # Layer 2: Imagery
            "background_image_url": imagery_data.get("imagery_url", ""),

            # Layer 3: Brand
            "logo_url": brand_data.get("logo_url", ""),
            "logo_position": brand_data.get("logo_position", "bottom_right"),
            "primary_color": brand_data.get("primary_color", "#000000"),
            "secondary_color": brand_data.get("secondary_color", "#FFFFFF"),
            "accent_color": brand_data.get("accent_color", "#FF5722"),
            "primary_font": brand_data.get("primary_font", "Inter"),
            "secondary_font": brand_data.get("secondary_font", "Inter"),
        }

        # Render based on carousel count
        if carousel_count == 1:
            # Single post
            rendered_url = await self.template_service.render_with_fallback(
                template_id=template_id,
                data=template_data,
                format=format
            )
            rendered_urls = [rendered_url]
            print(f"✓ Single render: {rendered_url[:80]}...")

        else:
            # Carousel (multi-slide)
            # Split content into slides (e.g., headline → slide 1, subtext → slide 2, etc.)
            slides_data = self._prepare_carousel_slides(template_data, carousel_count)

            rendered_urls = await self.template_service.render_carousel(
                template_id=template_id,
                slides_data=slides_data,
                format=format
            )
            print(f"✓ Carousel rendered: {len(rendered_urls)} slides")

        return LayerData(
            layer_type="typesetting",
            data={
                "template_id": template_id,
                "rendered_urls": rendered_urls,
                "format": format,
                "carousel_count": carousel_count
            },
            metadata={
                "template_vendor": "orshot",  # or "placid" if fallback
                "render_timestamp": datetime.utcnow().isoformat()
            }
        )

    def _prepare_carousel_slides(
        self,
        base_template_data: Dict,
        carousel_count: int
    ) -> List[Dict]:
        """
        Split content into carousel slides.

        Simple strategy:
        - Slide 1: Headline + background image
        - Slide 2: Subtext + background image
        - Slide 3+: CTA or additional content
        """
        slides = []

        # Slide 1: Headline
        slides.append({
            **base_template_data,
            "headline": base_template_data["headline"],
            "subtext": "",
            "cta": ""
        })

        # Slide 2: Subtext (if carousel_count >= 2)
        if carousel_count >= 2:
            slides.append({
                **base_template_data,
                "headline": "",
                "subtext": base_template_data["subtext"],
                "cta": ""
            })

        # Slide 3+: CTA (if carousel_count >= 3)
        if carousel_count >= 3:
            slides.append({
                **base_template_data,
                "headline": "",
                "subtext": "",
                "cta": base_template_data["cta"]
            })

        # Extend with duplicates if needed
        while len(slides) < carousel_count:
            slides.append(slides[-1])

        return slides[:carousel_count]

    def _calculate_total_cost(
        self,
        content_layer: LayerData,
        imagery_layer: LayerData,
        typesetting_layer: LayerData
    ) -> float:
        """
        Calculate total cost from all layers.
        """
        total = 0.0

        # Layer 1: Content cost
        total += content_layer.metadata.get("cost", 0.0)

        # Layer 2: Imagery cost
        total += imagery_layer.metadata.get("cost", 0.0)

        # Layer 4: Typesetting cost (from template service)
        total += typesetting_layer.metadata.get("cost", 0.0)

        return round(total, 4)

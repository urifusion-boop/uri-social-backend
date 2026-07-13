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

from app.agents.visual_engine_v2.models.visual_engine_models import (
    VisualEngineRenderV2,
    LayerData
)
from app.agents.visual_engine_v2.services.template_service import TemplateService
from app.agents.visual_engine_v2.services.image_path_service import ImagePathService
from app.agents.visual_engine_v2.config.template_config import select_template
from app.agents.visual_engine_v2.config.vendor_config import VendorConfig
from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService
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

    async def compose_final_render(
        self,
        user_id: str,
        brand_id: str,
        content_layer: LayerData,
        imagery_layer: LayerData,
        format: str = "1:1",
        formats: Optional[List[str]] = None,
        carousel_count: int = 1
    ) -> VisualEngineRenderV2:
        """
        Main composition pipeline: 4-layer → Final render.

        PRD Section 14: one content+image plan, rendered into every requested
        aspect-ratio format — the marginal cost of an extra format is just the
        extra render, not a new content/image generation.

        Args:
            user_id: User ID (from JWT)
            brand_id: Active brand ID (from get_active_brand_context — personal or
                agency brand), the same identifier every other endpoint in this
                app uses to resolve a brand profile. Never a raw Mongo _id.
            content_layer: Layer 1 (AI text: headline, subhead, promo, cta)
            imagery_layer: Layer 2 (imagery URL + metadata)
            format: single aspect ratio, used when `formats` isn't given
            formats: one or more aspect ratios ("1:1", "4:5", "9:16") to render
                the same content+image plan into
            carousel_count: Number of carousel slides (1 = single post)

        Returns:
            VisualEngineRenderV2 document ready to save to DB
        """
        target_formats = formats or [format]
        print(f"🎬 Starting 4-layer composition (formats={target_formats}, carousel={carousel_count})")

        # Layer 3: Extract brand context from database (shared across every format)
        brand_layer = await self._extract_brand_layer(user_id, brand_id)

        # Layer 4: select template + render, once per requested format
        typesetting_layers: Dict[str, LayerData] = {}
        for fmt in target_formats:
            typesetting_layers[fmt] = await self._render_typesetting_layer(
                content_layer=content_layer,
                imagery_layer=imagery_layer,
                brand_layer=brand_layer,
                format=fmt,
                carousel_count=carousel_count
            )

        primary_format = target_formats[0]
        primary_layer = typesetting_layers[primary_format]
        format_outputs = {fmt: layer.data.get("rendered_urls", []) for fmt, layer in typesetting_layers.items()}

        # PRD Section 12: surface failures rather than hide them — a fallback that
        # succeeded still needs a human to look at it, whether it came from the
        # imagery step (Path A retry exhausted) or the render step (both vendors down),
        # in any of the requested formats.
        needs_attention = imagery_layer.metadata.get("needs_attention", False) or any(
            layer.metadata.get("needs_attention", False) for layer in typesetting_layers.values()
        )
        error_message = imagery_layer.metadata.get("error_message") or next(
            (layer.metadata.get("error_message") for layer in typesetting_layers.values() if layer.metadata.get("error_message")),
            None
        )

        total_cost = content_layer.metadata.get("cost", 0.0) + imagery_layer.metadata.get("cost", 0.0)
        total_cost += sum(layer.metadata.get("cost", 0.0) for layer in typesetting_layers.values())

        # Build final render document
        render = VisualEngineRenderV2(
            user_id=user_id,
            brand_profile_id=brand_id,
            content_layer=content_layer,
            imagery_layer=imagery_layer,
            brand_layer=brand_layer,
            typesetting_layer=primary_layer,
            final_outputs=format_outputs[primary_format],
            format_outputs=format_outputs,
            status="completed",
            created_at=datetime.utcnow(),
            total_cost=round(total_cost, 4),
            needs_attention=needs_attention,
            error_message=error_message,
            used_fallback_background=any(layer.metadata.get("used_fallback_background", False) for layer in typesetting_layers.values())
        )

        print(f"✅ 4-layer composition complete: {len(target_formats)} format(s), {len(render.final_outputs)} output(s) in primary format")
        return render

    async def _extract_brand_layer(self, user_id: str, brand_id: str) -> LayerData:
        """
        Layer 3: Extract exact brand assets from database.

        PRD Section 10: Brand Layer
        - Exact hex colors from database
        - Logo URL
        - Font family names
        - No AI interpretation - exact values only

        Resolved via the same BrandProfileService.get() every other endpoint in
        this app uses (JWT user_id + active brand_id), not a raw Mongo _id lookup —
        there is no client-facing "brand profile id" anywhere else in this system.
        """
        print("🏢 Extracting Layer 3: Brand assets")

        result = await BrandProfileService.get(user_id, self.db, brand_id=brand_id)
        brand_profile = result.get("responseData") if isinstance(result, dict) else None

        if not brand_profile:
            raise ValueError(f"Brand profile not found for brand_id={brand_id}")

        # Colors are stored as an ordered list (brand_colors), not separate named
        # fields — map primary/secondary/accent by position, matching how every
        # other content-generation path in this app reads brand color.
        brand_colors = brand_profile.get("brand_colors") or []

        # Extract exact brand values (no defaults, no AI interpretation)
        brand_data = {
            "brand_name": brand_profile.get("brand_name", ""),
            "logo_url": brand_profile.get("logo_url"),
            "logo_position": brand_profile.get("logo_position", "bottom_right"),

            # Exact colors from database
            "primary_color": brand_colors[0] if len(brand_colors) > 0 else None,
            "secondary_color": brand_colors[1] if len(brand_colors) > 1 else None,
            "accent_color": brand_colors[2] if len(brand_colors) > 2 else None,
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
            metadata={"source": "database", "brand_id": brand_id}
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
        template_id = select_template(
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
            "subhead": content_data.get("subtext", ""),  # template slots use "subhead" (PRD naming)
            "promo": content_data.get("promo", ""),
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

        # Render based on carousel count. PRD Section 12: if the render vendor(s) are
        # down or fail outright, fall back to a brand-colored placeholder per slide
        # rather than raising — a delayed/degraded post beats a dropped job.
        needs_attention = False
        error_message = None
        try:
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
        except Exception as e:
            print(f"⚠️ Template render failed on all vendors, falling back to brand-colored placeholder(s): {e}")
            needs_attention = True
            error_message = f"Template render failed: {e}"
            rendered_urls = [
                ImagePathService.generate_placeholder_image(
                    brand_data.get("primary_color"), format=format
                )["imagery_url"]
                for _ in range(max(carousel_count, 1))
            ]

        # PRD Section 11: cost model — a fallback placeholder never actually reached
        # a paid vendor, so it costs nothing; a real render costs once per output image.
        render_cost = 0.0 if needs_attention else VendorConfig().cost_model.template_render_cost * len(rendered_urls)

        return LayerData(
            layer_type="typesetting",
            data={
                "template_id": template_id,
                "style_family": style_family,
                "post_intent": post_intent,
                "rendered_urls": rendered_urls,
                "format": format,
                "carousel_count": carousel_count
            },
            metadata={
                "template_vendor": "orshot",  # or "placid" if fallback
                "render_timestamp": datetime.utcnow().isoformat(),
                "needs_attention": needs_attention,
                "error_message": error_message,
                "used_fallback_background": needs_attention,
                "cost": round(render_cost, 4)
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

        # Slide 1: Headline (hook)
        slides.append({
            **base_template_data,
            "headline": base_template_data["headline"],
            "subtext": "",
            "subhead": "",
            "promo": "",
            "cta": ""
        })

        # Slide 2: Subtext / value (if carousel_count >= 2)
        if carousel_count >= 2:
            slides.append({
                **base_template_data,
                "headline": "",
                "subtext": base_template_data["subtext"],
                "subhead": base_template_data["subtext"],
                "promo": "",
                "cta": ""
            })

        # Slide 3+: CTA + promo, the closing slide (if carousel_count >= 3)
        if carousel_count >= 3:
            slides.append({
                **base_template_data,
                "headline": "",
                "subtext": "",
                "subhead": "",
                "promo": base_template_data.get("promo", ""),
                "cta": base_template_data["cta"]
            })

        # Extend with duplicates if needed
        while len(slides) < carousel_count:
            slides.append(slides[-1])

        return slides[:carousel_count]

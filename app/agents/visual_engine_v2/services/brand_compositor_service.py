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
from app.agents.social_media_manager.services.image_content_service import ImageContentService
from app.agents.visual_engine_v2.services.brand_prefs_service import BrandPrefsServiceV2
from app.utils.cloudinary_upload import upload_bytes
import asyncio
import base64
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
        imagery_layer: Optional[LayerData] = None,
        imagery_layers: Optional[List[LayerData]] = None,
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
            content_layer: Layer 1 (AI text). Single post: flat headline/
                subtext/promo/cta. Carousel: data.slides[] narrative arc
                (PRD Section 9.1) — see ContentLayerService.generate_carousel_content_plan.
            imagery_layer: Layer 2 for a single post (carousel_count == 1).
            imagery_layers: Layer 2 for a carousel (carousel_count > 1) — one
                independently-generated/uploaded image per slide, same order
                as content_layer.data.slides (PRD Section 9.1: never one
                image reused across every slide).
            format: single aspect ratio, used when `formats` isn't given
            formats: one or more aspect ratios ("1:1", "4:5", "9:16") to render
                the same content+image plan into
            carousel_count: Number of carousel slides (1 = single post)

        Returns:
            VisualEngineRenderV2 document ready to save to DB
        """
        target_formats = formats or [format]
        print(f"🎬 Starting 4-layer composition (formats={target_formats}, carousel={carousel_count})")

        is_carousel = carousel_count > 1
        if is_carousel and not imagery_layers:
            raise ValueError("imagery_layers is required when carousel_count > 1")
        if not is_carousel and not imagery_layer:
            raise ValueError("imagery_layer is required when carousel_count == 1")

        # Layer 3: Extract brand context from database (shared across every format)
        brand_layer = await self._extract_brand_layer(user_id, brand_id)

        # Layer 4: select template + render, once per requested format. PRD
        # Section 14: "the marginal cost of an extra format is just the
        # extra render" — run every format's render concurrently rather
        # than serially, since they're fully independent of each other.
        rendered_layers = await asyncio.gather(*[
            self._render_typesetting_layer(
                content_layer=content_layer,
                imagery_layer=imagery_layer,
                imagery_layers=imagery_layers,
                brand_layer=brand_layer,
                format=fmt,
                carousel_count=carousel_count
            )
            for fmt in target_formats
        ])
        typesetting_layers: Dict[str, LayerData] = dict(zip(target_formats, rendered_layers))

        primary_format = target_formats[0]
        primary_layer = typesetting_layers[primary_format]
        format_outputs = {fmt: layer.data.get("rendered_urls", []) for fmt, layer in typesetting_layers.items()}

        # A single imagery LayerData for the DB document either way: for a
        # carousel this summarizes all N slide images into one wrapper so the
        # rest of the schema (built around one post) doesn't need to change.
        imagery_layer_for_doc = imagery_layer or LayerData(
            layer_type="imagery",
            data={"slides": [l.data for l in (imagery_layers or [])]},
            metadata={
                "path": (imagery_layers[0].metadata.get("path") if imagery_layers else "A"),
                "needs_attention": any(l.metadata.get("needs_attention", False) for l in (imagery_layers or [])),
                "error_message": next((l.metadata.get("error_message") for l in (imagery_layers or []) if l.metadata.get("error_message")), None),
                "cost": sum(l.metadata.get("cost", 0.0) for l in (imagery_layers or [])),
            }
        )

        # PRD Section 12: surface failures rather than hide them — a fallback that
        # succeeded still needs a human to look at it, whether it came from the
        # imagery step (Path A retry exhausted) or the render step (both vendors down),
        # in any of the requested formats.
        needs_attention = imagery_layer_for_doc.metadata.get("needs_attention", False) or any(
            layer.metadata.get("needs_attention", False) for layer in typesetting_layers.values()
        )
        error_message = imagery_layer_for_doc.metadata.get("error_message") or next(
            (layer.metadata.get("error_message") for layer in typesetting_layers.values() if layer.metadata.get("error_message")),
            None
        )

        total_cost = content_layer.metadata.get("cost", 0.0) + imagery_layer_for_doc.metadata.get("cost", 0.0)
        total_cost += sum(layer.metadata.get("cost", 0.0) for layer in typesetting_layers.values())

        # Build final render document
        render = VisualEngineRenderV2(
            user_id=user_id,
            brand_profile_id=brand_id,
            content_layer=content_layer,
            imagery_layer=imagery_layer_for_doc,
            brand_layer=brand_layer,
            typesetting_layer=primary_layer,
            final_outputs=format_outputs[primary_format],
            format_outputs=format_outputs,
            status="completed",
            created_at=datetime.utcnow(),
            total_cost=round(total_cost, 4),
            needs_attention=needs_attention,
            error_message=error_message,
            used_fallback_background=any(layer.metadata.get("used_fallback_background", False) for layer in typesetting_layers.values()),
            is_carousel=is_carousel,
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

        # Reuse V1's full brand context builder (read-only import) instead of a
        # thin hand-picked field subset, so V2 is influenced by the same rich
        # profile — cta_styles, guardrails, ideal_customer_profile, voice,
        # style_selections, etc. — that V1's own content generation uses.
        context = BrandProfileService.to_brand_context(brand_profile)

        # Colors are stored as an ordered list (brand_colors), not separate named
        # fields — map primary/secondary/accent by position, matching how every
        # other content-generation path in this app reads brand color.
        brand_colors = context.get("brand_colors") or []

        # V2-only preferences (style_family, logo control mode) — stored in V2's
        # own collection, derived from the brand's existing style_selections,
        # never written back onto the V1 brand_profiles document.
        prefs = await BrandPrefsServiceV2.get_or_create(
            self.db,
            user_id=user_id,
            brand_id=brand_id,
            style_selections=context.get("style_selections"),
            industry=context.get("industry"),
        )

        brand_data = {
            "brand_name": context.get("brand_name", ""),
            "logo_url": context.get("logo_url"),
            "logo_position": context.get("logo_position", "bottom_right"),
            "logo_size": context.get("logo_size", "small"),

            # Exact colors from database
            "primary_color": brand_colors[0] if len(brand_colors) > 0 else None,
            "secondary_color": brand_colors[1] if len(brand_colors) > 1 else None,
            "accent_color": brand_colors[2] if len(brand_colors) > 2 else None,

            # Font families
            "primary_font": context.get("primary_font"),
            "secondary_font": context.get("secondary_font"),
            "font_style": context.get("font_style"),

            # Brand voice/content guidance (matches V1's own derivation, not a
            # dead lookup into a nonexistent voice_profile.tone key)
            "industry": context.get("industry"),
            "brand_voice": context.get("brand_voice", ""),
            "voice_profile": context.get("voice_profile") or {},
            "cta_styles": context.get("cta_styles") or [],
            "guardrails": context.get("guardrails") or {},
            "ideal_customer_profile": context.get("ideal_customer_profile", ""),
            "target_audience": context.get("target_audience", ""),
            "content_pillars": context.get("content_pillars") or [],
            "style_selections": context.get("style_selections") or [],
            "default_link": context.get("default_link", ""),

            # V2-only preferences (this brand's V2 rendering behavior)
            "style_family": prefs.get("style_family", "modern_professional"),
            "logo_control_mode": prefs.get("logo_control_mode", "agent"),
            "logo_manual_position": prefs.get("logo_manual_position"),
        }

        print(f"✓ Brand Layer: {brand_data['brand_name']}, Logo: {bool(brand_data['logo_url'])}, "
              f"Colors: {bool(brand_data['primary_color'])}, style_family={brand_data['style_family']}, "
              f"logo_mode={brand_data['logo_control_mode']}")

        return LayerData(
            layer_type="brand",
            data=brand_data,
            metadata={"source": "database", "brand_id": brand_id}
        )

    async def _render_typesetting_layer(
        self,
        content_layer: LayerData,
        brand_layer: LayerData,
        format: str,
        carousel_count: int,
        imagery_layer: Optional[LayerData] = None,
        imagery_layers: Optional[List[LayerData]] = None
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
        brand_data = brand_layer.data
        is_carousel = carousel_count > 1

        # Determine post intent from content metadata
        post_intent = content_layer.metadata.get("post_intent", "announcement")
        style_family = brand_data.get("style_family") or "modern_professional"
        image_path = (imagery_layers[0].metadata.get("path", "both") if is_carousel
                      else imagery_layer.metadata.get("path", "both"))  # "A", "B", or "both"

        # Logo control: "agent" (default) lets Orshot place the logo natively in
        # the template's own logo slot; "user" means Orshot renders WITHOUT a
        # logo and we composite it afterward at the user's exact chosen
        # position — set via the V2-only brand prefs, never the shared profile.
        logo_control_mode = brand_data.get("logo_control_mode") or "agent"
        logo_url = brand_data.get("logo_url") or ""
        logo_size = brand_data.get("logo_size") or "small"
        logo_position = brand_data.get("logo_manual_position") or brand_data.get("logo_position", "bottom_right")

        # Select template
        template_id = select_template(
            style_family=style_family,
            post_intent=post_intent,
            format=format,
            image_path=image_path
        )

        print(f"📋 Selected template: {template_id} (intent={post_intent}, format={format})")

        # Brand fields shared by every slide/render — PRD Section 9.1
        # consistency rule: "all slides in one carousel share the same
        # template family and brand values."
        brand_fields = {
            "logo_url": logo_url if logo_control_mode == "agent" else "",
            "logo_position": logo_position if logo_control_mode == "agent" else "",
            "logo_size": logo_size,
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
            if not is_carousel:
                # Single post: one flat content plan + one image
                imagery_data = imagery_layer.data
                template_data = {
                    "headline": content_data.get("headline", ""),
                    "subtext": content_data.get("subtext", ""),
                    "subhead": content_data.get("subtext", ""),  # template slots use "subhead" (PRD naming)
                    "promo": content_data.get("promo", ""),
                    "cta": content_data.get("cta", ""),
                    "background_image_url": imagery_data.get("imagery_url", ""),
                    **brand_fields,
                }
                rendered_url = await self.template_service.render_with_fallback(
                    template_id=template_id,
                    data=template_data,
                    format=format
                )
                rendered_urls = [rendered_url]
                print(f"✓ Single render: {rendered_url[:80]}...")

            else:
                # Carousel: PRD Section 9.1 — each slide keeps its OWN headline/
                # subtext/cta (the narrative arc from generate_carousel_content_plan)
                # and its OWN independently-generated/uploaded image; only the
                # brand fields are constant across slides.
                slides_content = content_data.get("slides") or []
                while len(slides_content) < carousel_count:
                    slides_content.append(dict(slides_content[-1]) if slides_content else {})
                slides_content = slides_content[:carousel_count]

                slides_data = []
                for i, slide_content in enumerate(slides_content):
                    slide_imagery = imagery_layers[i] if i < len(imagery_layers) else imagery_layers[-1]
                    slides_data.append({
                        "headline": slide_content.get("headline", ""),
                        "subtext": slide_content.get("subtext", ""),
                        "subhead": slide_content.get("subtext", ""),
                        "promo": slide_content.get("promo", ""),
                        "cta": slide_content.get("cta", ""),
                        "background_image_url": slide_imagery.data.get("imagery_url", ""),
                        **brand_fields,
                    })

                rendered_urls = await self.template_service.render_carousel(
                    template_id=template_id,
                    slides_data=slides_data,
                    format=format
                )
                print(f"✓ Carousel rendered: {len(rendered_urls)} slides, each with its own content + image")
        except Exception as e:
            print(f"⚠️ Template render failed on all vendors, falling back to brand-colored placeholder(s): {e}")
            needs_attention = True
            error_message = f"Template render failed: {e}"
            rendered_urls = [
                (await ImagePathService.generate_placeholder_image(
                    brand_data.get("primary_color"), format=format
                ))["imagery_url"]
                for _ in range(max(carousel_count, 1))
            ]

        # User-controlled logo mode: the template above rendered WITHOUT a logo
        # (see template_data above), so stamp it on now at the user's exact
        # chosen position. Skipped on the placeholder-fallback path — a
        # needs-attention render already gets a human look before it can post.
        if logo_control_mode == "user" and logo_url and not needs_attention:
            rendered_urls = [
                await self._composite_user_logo(url, logo_url, logo_position, logo_size)
                for url in rendered_urls
            ]
            print(f"✓ User-controlled logo composited at '{logo_position}' on {len(rendered_urls)} render(s)")

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

    async def _composite_user_logo(
        self, rendered_url: str, logo_url: str, position: str, logo_size: str
    ) -> str:
        """
        User-controlled logo positioning: the template vendor rendered without
        a logo, so stamp the brand logo onto the finished image at the user's
        exact chosen position. Reuses V1's Pillow compositor (read-only
        import) instead of reimplementing image compositing here — same
        function the existing Upload Content flow already relies on.

        Fails safe: if download/composite/upload fails for any reason, returns
        the un-logoed render rather than blocking the job.
        """
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(rendered_url)
                resp.raise_for_status()
            base_b64 = base64.b64encode(resp.content).decode()
            composited_b64 = ImageContentService._overlay_logo(
                base_b64, logo_url, position=position, logo_size=logo_size
            )
            composited_bytes = base64.b64decode(composited_b64)
            return await upload_bytes(composited_bytes, folder="uri-social/visual-engine-v2")
        except Exception as e:
            print(f"⚠️ User-controlled logo compositing failed, using un-logoed render: {e}")
            return rendered_url

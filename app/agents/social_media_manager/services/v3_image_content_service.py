"""
V3 Image Content Service - Isolated Implementation
Uses V3 10-block prompt architecture with existing DALL-E API infrastructure.

This is a PARALLEL implementation for A/B testing. Does NOT modify production code.
Reuses existing ImageContentService._call_dalle_api() for actual image generation.
"""

from typing import Dict, Any, Optional
from datetime import datetime
from bson import ObjectId

from app.domain.responses.uri_response import UriResponse
from app.agents.social_media_manager.services.image_content_service import ImageContentService
from app.agents.social_media_manager.services.v3_prompt_builder import V3PromptBuilder


class V3ImageContentService:
    """
    V3 image generation service using 10-block prompt architecture.

    Key differences from V2 (production):
    - Uses V3PromptBuilder for 10-block prompts instead of 6-section assembly
    - Richer aesthetic vocabulary from V3 modules
    - 11-dimensional style system
    - 100+ sensitive content exclusion rules
    - African realism vocabulary

    Similarities (reused from V2):
    - Same DALL-E/GPT-Image-2 API calls
    - Same Cloudinary upload logic
    - Same database schema (content_drafts)
    - Same credit system
    """

    @staticmethod
    async def generate_image_for_draft_v3(
        seed_content: str,
        brand_context: Dict[str, Any],
        platform: str = "instagram",
        reference_image: Optional[str] = None,
        user_id: Optional[str] = None,
        slide_index: Optional[int] = None,
        total_slides: Optional[int] = None,
        image_model: str = "gpt-image-2",
    ) -> Dict[str, Any]:
        """
        Generate image using V3 prompt system.

        Args:
            seed_content: User's content/request
            brand_context: Brand profile with style_slug, colors, etc.
            platform: Target platform (instagram, linkedin, etc.)
            reference_image: Product image URL (if provided)
            user_id: User ID for tracking
            slide_index: Carousel slide number (0-indexed)
            total_slides: Total carousel slides
            image_model: "gpt-image-2" or "dall-e-3"

        Returns:
            UriResponse with image_url, prompt_used, metadata
        """
        try:
            print(f"\n{'='*70}")
            print(f"V3 IMAGE GENERATION START")
            print(f"{'='*70}")
            print(f"User: {user_id}")
            print(f"Platform: {platform}")
            print(f"Model: {image_model}")
            print(f"Seed: {seed_content[:100]}...")

            # ========== STEP 1: PRODUCT ANALYSIS (if reference image provided) ==========
            product_spec = None
            if reference_image:
                try:
                    from app.agents.social_media_manager.services.product_analysis_service import ProductAnalysisService
                    from app.utils.background_removal import remove_background

                    print(f"\n[V3] Product preservation pipeline activated")

                    # Remove background
                    cutout_url = await remove_background(reference_image, method="auto")
                    print(f"[V3] Background removed: {cutout_url[:80]}...")

                    # Forensic analysis
                    product_spec = await ProductAnalysisService.analyze_product_forensically(cutout_url)
                    print(f"[V3] Product analyzed: {product_spec.get('product_name', 'Unknown')}")

                    # Update reference to use cutout
                    reference_image = cutout_url

                except Exception as e:
                    print(f"[V3] Product analysis error: {e} - continuing without preservation")
                    product_spec = None

            # ========== STEP 2: BUILD V3 10-BLOCK PROMPT ==========
            style_slug = brand_context.get("style_slug")

            prompt_result = V3PromptBuilder.build_complete_prompt(
                seed_content=seed_content,
                brand_context=brand_context,
                platform=platform,
                style_slug=style_slug,
                reference_image=reference_image,
                product_spec=product_spec,
                slide_index=slide_index,
                total_slides=total_slides,
            )

            final_prompt = prompt_result["prompt"]
            metadata = prompt_result["metadata"]
            blocks_used = prompt_result["blocks_used"]

            print(f"\n{'━'*70}")
            print(f"V3 PROMPT GENERATED")
            print(f"{'━'*70}")
            print(f"Architecture: V3 10-Block")
            print(f"Length: {len(final_prompt)} chars")
            print(f"Blocks: {', '.join(blocks_used)}")
            print(f"\n{final_prompt[:500]}...\n")
            print(f"{'━'*70}\n")

            # ========== STEP 3: DETERMINE IMAGE SPECIFICATIONS ==========
            # Platform-specific dimensions (same as V2)
            platform_specs = {
                "instagram": {"width": 1080, "height": 1080, "aspect_ratio": "1:1"},
                "facebook": {"width": 1200, "height": 630, "aspect_ratio": "1.91:1"},
                "twitter": {"width": 1200, "height": 675, "aspect_ratio": "16:9"},
                "x": {"width": 1200, "height": 675, "aspect_ratio": "16:9"},
                "linkedin": {"width": 1200, "height": 627, "aspect_ratio": "1.91:1"},
            }

            specs = platform_specs.get(platform, {"width": 1024, "height": 1024, "aspect_ratio": "1:1"})

            # Check for art-piece mode (9:16 posters)
            # This is set by V3 style library if style_type == "art_piece"
            from app.agents.social_media_manager.services.v3_style_library import get_style_dimensions
            if style_slug:
                style_dims = get_style_dimensions(style_slug, brand_context.get("industry", "general_other"))
                if style_dims and style_dims.get("style_type") == "art_piece":
                    specs = {"width": 1024, "height": 1792, "aspect_ratio": "9:16"}
                    print(f"[V3] Art-Piece Poster Mode: 9:16 format (1024x1792)")

            # ========== STEP 4: CALL GPT-IMAGE-2 API (reuse from V2) ==========
            # Force GPT-Image-2 for V3 testing (same as production)
            gpt_image_2_model = "openai/gpt-image-2"
            print(f"[V3] Calling GPT-Image-2 API...")
            generation_start = datetime.utcnow()

            image_response = await ImageContentService._call_dalle_api(
                prompt=final_prompt,
                size=f"{specs['width']}x{specs['height']}",
                reference_image=reference_image,
                image_model=gpt_image_2_model,
            )

            generation_time_ms = int((datetime.utcnow() - generation_start).total_seconds() * 1000)
            print(f"[V3] Generation completed in {generation_time_ms}ms")

            if not image_response.get('success'):
                error_msg = image_response.get('error', 'Unknown error')
                print(f"[V3] ❌ Generation failed: {error_msg}")
                return UriResponse.error_response(f"V3 image generation failed: {error_msg}")

            # ========== STEP 5: COMPOSE LOGO (if brand has logo) ==========
            image_url = image_response['image_url']
            logo_url = brand_context.get('logo_url')

            if logo_url:
                try:
                    from app.agents.social_media_manager.services.logo_compositor import LogoCompositor

                    print(f"[V3] Compositing brand logo...")
                    image_url = await LogoCompositor.composite_logo(
                        base_image_url=image_url,
                        logo_url=logo_url,
                        logo_position=brand_context.get('logo_position', 'bottom-right'),
                        logo_size=brand_context.get('logo_size', 'small'),
                    )
                    print(f"[V3] ✅ Logo composited")
                except Exception as e:
                    print(f"[V3] Logo composition error: {e} - using image without logo")

            # ========== STEP 6: BUILD RESPONSE ==========
            draft_id = str(ObjectId())

            response_data = {
                "draft_id": draft_id,
                "image_url": image_url,
                "platform": platform,
                "seed_content": seed_content,

                # V3-specific metadata
                "v3_metadata": {
                    "architecture": "v3_10_block",
                    "prompt": final_prompt,
                    "prompt_length": len(final_prompt),
                    "blocks_used": blocks_used,
                    "style_slug": style_slug,
                    "has_product_reference": bool(reference_image),
                    "generation_time_ms": generation_time_ms,
                    "image_model": image_model,
                    "timestamp": datetime.utcnow().isoformat(),
                },

                # Image specs
                "image_specs": specs,

                # Status
                "status": "completed",
                "created_at": datetime.utcnow().isoformat(),
            }

            print(f"\n{'='*70}")
            print(f"V3 IMAGE GENERATION COMPLETE")
            print(f"{'='*70}")
            print(f"✅ Image URL: {image_url[:80]}...")
            print(f"✅ Draft ID: {draft_id}")
            print(f"✅ Generation time: {generation_time_ms}ms")
            print(f"{'='*70}\n")

            return UriResponse.get_single_data_response("v3_image_generation", response_data)

        except Exception as e:
            print(f"\n[V3] ❌ EXCEPTION: {str(e)}")
            import traceback
            traceback.print_exc()
            return UriResponse.error_response(f"V3 image generation exception: {str(e)}")


    @staticmethod
    async def generate_image_for_campaign_v3(
        seed_content: str,
        brand_context: Dict[str, Any],
        platforms: list,
        user_id: str,
        reference_image: Optional[str] = None,
        image_model: str = "gpt-image-2",
    ) -> Dict[str, Any]:
        """
        Generate images for multiple platforms using V3 system.
        Returns comparison data for A/B testing.

        Args:
            seed_content: User's content request
            brand_context: Brand profile
            platforms: List of platforms to generate for
            user_id: User ID
            reference_image: Product image URL (optional)
            image_model: Image generation model

        Returns:
            Dictionary with generated images per platform + comparison metadata
        """
        import asyncio

        print(f"\n🎨 V3 MULTI-PLATFORM GENERATION")
        print(f"Platforms: {platforms}")
        print(f"User: {user_id}")

        # Generate for all platforms concurrently
        tasks = []
        for platform in platforms:
            task = V3ImageContentService.generate_image_for_draft_v3(
                seed_content=seed_content,
                brand_context=brand_context,
                platform=platform,
                reference_image=reference_image,
                user_id=user_id,
                image_model=image_model,
            )
            tasks.append(task)

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        campaign_results = []
        errors = []

        for i, result in enumerate(results):
            platform = platforms[i]

            if isinstance(result, Exception):
                errors.append({"platform": platform, "error": str(result)})
                continue

            if result.get('status'):
                campaign_results.append({
                    "platform": platform,
                    **result['responseData']
                })
            else:
                errors.append({"platform": platform, "error": result.get('responseMessage', 'Unknown error')})

        return {
            "v3_campaign_results": campaign_results,
            "errors": errors,
            "platforms_succeeded": len(campaign_results),
            "platforms_failed": len(errors),
            "user_id": user_id,
            "timestamp": datetime.utcnow().isoformat(),
        }

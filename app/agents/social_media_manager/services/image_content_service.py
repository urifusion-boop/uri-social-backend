# app/agents/social_media_manager/services/image_content_service.py

import asyncio
from typing import Dict, List, Any, Optional
from datetime import datetime
from bson import ObjectId

from app.domain.responses.uri_response import UriResponse
from app.services.AIService import AIService


class ImageContentService:
    """
    AI-powered image generation service for social media content
    
    This service:
    - Generates DALL-E images based on content and brand guidelines
    - Creates platform-optimized images (different sizes/formats)
    - Combines text and images for complete social media posts
    - Manages image assets and storage
    """
    
    # Platform-specific image requirements
    IMAGE_SPECS = {
        "linkedin": {
            "post_image": {"width": 1200, "height": 628, "format": "landscape"},
            "cover_image": {"width": 1584, "height": 396, "format": "banner"},
            "profile_image": {"width": 400, "height": 400, "format": "square"}
        },
        "twitter": {
            "post_image": {"width": 1200, "height": 675, "format": "landscape"},
            "header_image": {"width": 1500, "height": 500, "format": "banner"},
            "profile_image": {"width": 400, "height": 400, "format": "square"}
        },
        "facebook": {
            "post_image": {"width": 1200, "height": 630, "format": "landscape"},
            "post_portrait": {"width": 1080, "height": 1350, "format": "portrait"},
            "cover_image": {"width": 820, "height": 312, "format": "banner"},
            "profile_image": {"width": 180, "height": 180, "format": "square"}
        },
        "instagram": {
            "post_square": {"width": 1080, "height": 1080, "format": "square"},
            "post_portrait": {"width": 1080, "height": 1350, "format": "portrait"},
            "story": {"width": 1080, "height": 1920, "format": "story"},
            "profile_image": {"width": 320, "height": 320, "format": "square"}
        }
    }

    @staticmethod
    async def check_reference_image_quality(image_url: str) -> Dict[str, Any]:
        """
        Quality gate for reference images (PRD Section 3: Quality Gate).
        Checks resolution, blur, and exposure before processing.

        Returns:
            {
                "passed": bool,
                "message": str (only if failed),
                "width": int,
                "height": int
            }
        """
        try:
            import io
            import requests
            from PIL import Image
            import numpy as np

            # Fetch the image
            response = requests.get(image_url, timeout=10)
            response.raise_for_status()
            img = Image.open(io.BytesIO(response.content))

            width, height = img.size

            # Check 1: Minimum resolution (500x500)
            if width < 500 or height < 500:
                return {
                    "passed": False,
                    "message": "This photo is a bit small. Can you send a higher quality version? It'll make your product look much better.",
                    "width": width,
                    "height": height
                }

            # Check 2: Blur detection using Laplacian variance
            # Convert to grayscale and check sharpness
            gray = img.convert('L')
            np_img = np.array(gray)

            # Calculate Laplacian variance (measure of sharpness)
            from scipy import ndimage
            laplacian = ndimage.laplace(np_img)
            variance = laplacian.var()

            # Threshold for blur detection (empirically determined)
            if variance < 100:
                return {
                    "passed": False,
                    "message": "This photo is a bit blurry. A clearer shot will give you a much better result.",
                    "width": width,
                    "height": height
                }

            # Check 3: Exposure check (too dark or too bright)
            # Calculate mean brightness
            brightness = np.array(gray).mean()

            if brightness < 40:
                return {
                    "passed": False,
                    "message": "The lighting on this photo is a bit dark. Can you retake in better lighting, or want me to work with it?",
                    "width": width,
                    "height": height
                }
            elif brightness > 220:
                return {
                    "passed": False,
                    "message": "The lighting on this photo is a bit bright. Can you retake in better lighting, or want me to work with it?",
                    "width": width,
                    "height": height
                }

            # All checks passed
            return {
                "passed": True,
                "width": width,
                "height": height
            }

        except Exception as e:
            # If quality check fails, allow the image to proceed
            # (don't block generation due to quality check errors)
            print(f"⚠️ Quality check error: {str(e)}")
            return {
                "passed": True,
                "width": 0,
                "height": 0
            }

    @staticmethod
    async def detect_product_category(image_url: str) -> Dict[str, Any]:
        """
        Product detection using GPT-4o-mini vision (PRD Section 3: Product Detection).
        Identifies product category and suggests styling elements.

        Returns:
            {
                "category": str,  # e.g., "perfume", "skincare", "food"
                "subcategory": str,  # e.g., "oriental", "moisturizer", "beverage"
                "suggested_props": str,  # e.g., "amber resin, cinnamon sticks"
                "suggested_surface": str,  # e.g., "dark wood or ornate metal tray"
                "suggested_mood": str,  # e.g., "warm, smoky, luxurious"
                "color_notes": str  # e.g., "bottle is dark amber glass with gold cap"
            }
        """
        try:
            from app.services.AIService import client as openai_client

            prompt = """What product is in this image? Return JSON with these fields:
{
  "category": "product category (perfume/skincare/food/fashion/electronics/jewellery/other)",
  "subcategory": "more specific type",
  "suggested_props": "2-4 styling props that would complement this product in a photo shoot",
  "suggested_surface": "ideal surface for product photography",
  "suggested_mood": "3-4 adjectives describing the mood",
  "color_notes": "describe the product's colors and materials"
}"""

            response = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": image_url}},
                            {"type": "text", "text": prompt}
                        ]
                    }],
                    max_tokens=200,
                    temperature=0.3
                )
            )

            import json
            result = json.loads(response.choices[0].message.content.strip())

            print(f"🔍 Product detection: {result.get('category')} - {result.get('subcategory')}")

            return result

        except Exception as e:
            print(f"⚠️ Product detection error: {str(e)}")
            # Return generic fallback
            return {
                "category": "product",
                "subcategory": "general",
                "suggested_props": "complementary items",
                "suggested_surface": "clean neutral surface",
                "suggested_mood": "professional, clean, minimal",
                "color_notes": "brand colors"
            }

    @staticmethod
    async def remove_background(image_url: str) -> Optional[str]:
        """
        Background removal (PRD Section 3: Step 4 - Background Removal).
        Extracts the product as a clean cutout on transparent background.

        NOTE: This uses GPT-Image-2 edit mode ONLY for background removal,
        NOT for the final graphic generation.

        Returns:
            URL of the product cutout with transparent background, or None if failed
        """
        try:
            from app.services.AIService import client as openai_client
            import requests
            import io

            # Download the image
            response = requests.get(image_url, timeout=10)
            response.raise_for_status()
            image_bytes = io.BytesIO(response.content)

            # Use GPT-Image-2 edit mode for background removal ONLY
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: openai_client.images.edit(
                    model="dall-e-2",  # edit mode only available in dall-e-2
                    image=image_bytes,
                    prompt="Remove the background completely. Keep only the product on a transparent background. Do not modify the product in any way — preserve exact colours, details, and proportions.",
                    n=1,
                    size="1024x1024"
                )
            )

            cutout_url = result.data[0].url
            print(f"✂️ Background removed: {cutout_url}")

            return cutout_url

        except Exception as e:
            print(f"⚠️ Background removal error: {str(e)}")
            # If background removal fails, return original image
            # The generation will still work, just without clean cutout
            return image_url

    @staticmethod
    def get_product_composition_guidelines(product_category: str) -> Dict[str, str]:
        """
        Product-specific composition guidelines (PRD Section 5.2).
        Returns styling rules based on product category.

        Returns:
            {
                "angle": str,  # Ideal camera angle
                "surface": str,  # Recommended surface
                "props": str,  # Typical props
                "background": str  # Background style
            }
        """
        guidelines = {
            "perfume": {
                "angle": "Front-facing, slight 15° angle to show label and depth",
                "surface": "Marble, stone, dark wood, or silk fabric",
                "props": "Fragrance ingredients: petals, vanilla pods, citrus slices, spices, oud chips",
                "background": "Solid colour, gradient, or atmospheric (smoke, bokeh)"
            },
            "fragrance": {
                "angle": "Front-facing, slight 15° angle to show label and depth",
                "surface": "Marble, stone, dark wood, or silk fabric",
                "props": "Fragrance ingredients: petals, vanilla pods, citrus slices, spices, oud chips",
                "background": "Solid colour, gradient, or atmospheric (smoke, bokeh)"
            },
            "skincare": {
                "angle": "Front-facing, upright, label readable",
                "surface": "Marble, glass shelf, bathroom surface",
                "props": "Raw ingredients: aloe, honey, citrus, herbs",
                "background": "Clean minimal or botanical"
            },
            "beauty": {
                "angle": "Front-facing, upright, label readable",
                "surface": "Marble, glass shelf, vanity surface",
                "props": "Raw ingredients: aloe, honey, citrus, botanicals",
                "background": "Clean minimal or botanical"
            },
            "food": {
                "angle": "45° overhead or front-facing for bottles",
                "surface": "Wood board, marble, rustic surface",
                "props": "Raw ingredients of the dish/drink",
                "background": "Warm, natural, kitchen-adjacent"
            },
            "beverage": {
                "angle": "45° overhead or front-facing",
                "surface": "Wood board, marble, bar surface",
                "props": "Ingredients, garnishes, ice",
                "background": "Warm, inviting, bar or kitchen setting"
            },
            "fashion": {
                "angle": "Flat-lay (overhead) or on-figure if full outfit",
                "surface": "Clean white, linen, wood plank",
                "props": "Complementary accessories: sunglasses, bag, shoes",
                "background": "Clean white/cream or lifestyle context"
            },
            "clothing": {
                "angle": "Flat-lay (overhead)",
                "surface": "Clean white or neutral fabric",
                "props": "Accessories that complement the garment",
                "background": "Clean minimal"
            },
            "electronics": {
                "angle": "Front-facing or 3/4 angle",
                "surface": "Clean surface, desk, or floating",
                "props": "Minimal: maybe a cable or accessory",
                "background": "Gradient, dark, or clean white"
            },
            "gadget": {
                "angle": "Front-facing or 3/4 angle",
                "surface": "Modern desk or tech surface",
                "props": "Related accessories only",
                "background": "Tech-themed gradient or dark"
            },
            "jewellery": {
                "angle": "Close-up, detail-forward",
                "surface": "Velvet, marble, mirror surface",
                "props": "Minimal: maybe a single flower or fabric swatch",
                "background": "Dark for gold/diamonds, light for silver/pearls"
            },
            "jewelry": {
                "angle": "Close-up, detail-forward",
                "surface": "Velvet, marble, mirror surface",
                "props": "Minimal: single flower or elegant fabric",
                "background": "Dark for gold/diamonds, light for silver/pearls"
            }
        }

        # Return product-specific guidelines or generic fallback
        return guidelines.get(product_category.lower(), {
            "angle": "Front-facing, label visible",
            "surface": "Clean neutral surface",
            "props": "Contextual items that relate to the product's use",
            "background": "Brand colour-matched gradient or solid"
        })

    @staticmethod
    async def generate_content_with_images(
        user_id: str,
        seed_content: str,
        platforms: List[str],
        include_images: bool = True,
        brand_context: Optional[Dict[str, Any]] = None,
        db=None,
        reference_image: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generate complete social media content with text and images
        
        Args:
            user_id: User requesting content
            seed_content: Original content to transform
            platforms: List of platforms to generate for
            include_images: Whether to generate images
            brand_context: Brand guidelines (colors, style, logo, etc.)
        """
        try:
            from .content_generation_service import ContentGenerationService
            
            # Generate text content first (pass db so drafts are saved before image update)
            text_result = await ContentGenerationService.generate_multi_platform_content(
                user_id=user_id,
                seed_content=seed_content,
                platforms=platforms,
                seed_type="text",
                brand_context=brand_context,
                db=db
            )
            
            if not text_result.get('status') or not include_images:
                return text_result
            
            # Generate images for each successful text draft
            drafts_with_images = []
            image_errors = []
            
            for draft in text_result['responseData']['drafts']:
                try:
                    # Generate image for this platform/content
                    image_result = await ImageContentService._generate_platform_image(
                        platform=draft['platform'],
                        content=draft['content'],
                        seed_content=seed_content,
                        brand_context=brand_context,
                        reference_image=reference_image,
                    )
                    
                    if image_result.get('status'):
                        raw_image_url = image_result['responseData']['image_url']
                        draft['image_specs'] = image_result['responseData']['specs']
                        draft['has_image'] = True

                        # Upload base64 image to Cloudinary for permanent CDN storage
                        stored_url = raw_image_url
                        if raw_image_url and raw_image_url.startswith("data:"):
                            print(f"🔄 Uploading image to Cloudinary for draft {draft['id']} ({draft['platform']})...")
                            try:
                                from app.utils.cloudinary_upload import upload_base64
                                stored_url = await upload_base64(raw_image_url, folder="uri-social/content-drafts")
                                print(f"☁️  ✅ CLOUDINARY UPLOAD SUCCESS!")
                                print(f"   📍 Draft ID: {draft['id']}")
                                print(f"   🌐 Platform: {draft['platform']}")
                                print(f"   🔗 URL: {stored_url}")
                            except Exception as _save_err:
                                print(f"⚠️  ❌ CLOUDINARY UPLOAD FAILED!")
                                print(f"   📍 Draft ID: {draft['id']}")
                                print(f"   🌐 Platform: {draft['platform']}")
                                print(f"   ❌ Error: {_save_err}")
                                print(f"   ⚠️  Keeping base64 data URL as fallback")

                        # Persist URL to DB
                        if db is not None:
                            result = await db["content_drafts"].update_one(
                                {"id": draft["id"]},
                                {"$set": {
                                    "image_url": stored_url,
                                    "image_specs": draft['image_specs'],
                                    "has_image": True,
                                }}
                            )
                            print(f"🖼️ Image saved to draft {draft['id']}: matched={result.matched_count}, modified={result.modified_count}")
                        draft['image_url'] = stored_url if not stored_url.startswith("data:") else None
                    else:
                        draft['has_image'] = False
                        image_errors.append({
                            "platform": draft['platform'],
                            "error": image_result.get('responseMessage', 'Image generation failed')
                        })

                    drafts_with_images.append(draft)
                    
                except Exception as e:
                    draft['has_image'] = False
                    drafts_with_images.append(draft)
                    image_errors.append({
                        "platform": draft['platform'],
                        "error": f"Image generation error: {str(e)}"
                    })
            
            # Update response with image information
            text_result['responseData']['drafts'] = drafts_with_images
            text_result['responseData']['images_generated'] = len([d for d in drafts_with_images if d.get('has_image')])
            text_result['responseData']['image_errors'] = image_errors
            
            return text_result
            
        except Exception as e:
            return UriResponse.error_response(f"Content with images generation failed: {str(e)}")
    
    @staticmethod
    async def regenerate_image_for_draft(
        draft_id: str,
        user_id: str,
        feedback: str,
        db,
    ) -> None:
        """
        Background task: regenerate a draft's image using user feedback.
        Clears image_url first (frontend shows shimmer), then generates a new
        image incorporating the feedback and persists it to the draft.
        """
        import re as _re, base64 as _b64, os as _os, uuid as _uuid
        from datetime import datetime

        try:
            draft = await db["content_drafts"].find_one(
                {"$or": [{"id": draft_id}, {"draft_id": draft_id}], "user_id": user_id}
            )
            if not draft:
                print(f"⚠️ regenerate_image: draft {draft_id} not found for user {user_id}")
                return

            platform = draft.get("platform", "instagram")
            content = draft.get("content", "")
            seed_content = draft.get("seed_content") or ""

            # Fall back to content_requests if seed_content wasn't stored on the draft
            if not seed_content:
                request_id = draft.get("request_id")
                if request_id:
                    req = await db["content_requests"].find_one({"id": request_id}, {"seed_content": 1})
                    seed_content = (req or {}).get("seed_content") or ""
            if not seed_content:
                seed_content = content  # last resort

            # Load brand profile so logo (and its position) are applied during regeneration
            from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService as _BPS
            _profile_doc = await db["brand_profiles"].find_one({"user_id": user_id})
            if _profile_doc:
                _profile_doc.pop("_id", None)
            regen_brand_context = _BPS.to_brand_context(_profile_doc) if _profile_doc else {}

            image_result = await ImageContentService._generate_platform_image(
                platform=platform,
                content=content,
                seed_content=seed_content,
                brand_context=regen_brand_context,
                feedback=feedback,
            )

            if not image_result.get("status"):
                print(f"❌ regenerate_image: generation failed for {draft_id}: {image_result.get('error')}")
                return

            raw_url = image_result["responseData"]["image_url"]
            specs = image_result["responseData"]["specs"]

            # Upload base64 to Cloudinary for permanent CDN storage
            stored_url = raw_url
            if raw_url and raw_url.startswith("data:"):
                print(f"🔄 Uploading REGENERATED image to Cloudinary for draft {draft_id}...")
                try:
                    from app.utils.cloudinary_upload import upload_base64
                    stored_url = await upload_base64(raw_url, folder="uri-social/content-drafts")
                    print(f"☁️  ✅ CLOUDINARY REGENERATION UPLOAD SUCCESS!")
                    print(f"   📍 Draft ID: {draft_id}")
                    print(f"   🔗 URL: {stored_url}")
                except Exception as _e:
                    print(f"⚠️  ❌ CLOUDINARY REGENERATION UPLOAD FAILED!")
                    print(f"   📍 Draft ID: {draft_id}")
                    print(f"   ❌ Error: {_e}")

            await db["content_drafts"].update_one(
                {"$or": [{"id": draft_id}, {"draft_id": draft_id}]},
                {"$set": {
                    "image_url": stored_url if not stored_url.startswith("data:") else None,
                    "image_specs": specs,
                    "has_image": True,
                    "updated_at": datetime.utcnow(),
                }},
            )
            print(f"✅ regenerate_image: draft {draft_id} image updated")

        except Exception as e:
            print(f"❌ regenerate_image error for {draft_id}: {e}")

    @staticmethod
    def _get_dynamic_motion_detail(industry: str) -> str:
        """
        Returns dynamic motion instructions based on product category (PRD Section 3).
        Every immersive image includes at least ONE element suggesting frozen motion.
        """
        motion_map = {
            "perfume_fragrance": "Smoke wisps, floating petals drifting upward. Gold dust particles catching light.",
            "beauty_wellness": "Gentle splash of liquid. Petals or ingredients drifting. Dewy droplets forming on surface. Light catching moisture.",
            "water_beverage": "Explosive water splash around bottle. Droplets frozen mid-air. Ice crystals, condensation, ripple patterns.",
            "juice_smoothie": "Fruit pieces exploding outward. Juice splashing in arcs. Scattered berries, citrus slices, leaves flying.",
            "dairy_milk": "Milk/cream splash erupting around product. Creamy swirls. Dripping cream, poured liquid, splatter patterns.",
            "food_beverage": "Ingredient explosion: crumbs, herbs, spices mid-air. Steam rising. Sauce drizzle, cheese pull, crunch particles.",
            "fashion_ecommerce": "Fabric in motion: flowing, catching wind, dynamic drape. Lens flare, urban particles, motion blur in background.",
            "shoes_accessories": "Ground particle kick-up. Lace or strap in motion. Water splash on wet surface. Dust in light beam.",
            "fintech_saas_tech": "Light trails, data particles, holographic glow effects. Reflections on glossy surfaces. Cool atmospheric haze.",
            "jewellery_watches": "Sparkle particles. Light caustics from gems. Velvet ripple. Metallic reflection patterns.",
            "home_candles": "Flame flicker. Wax melt. Smoke curl. Warm bokeh. Dust in sunbeam. Soft focus.",
        }
        return motion_map.get(industry, "Floating dust, light motes, atmospheric haze. Micro-splashes, surface texture, soft motion.")

    @staticmethod
    def _get_text_styling_detail(industry: str) -> str:
        """
        Returns text styling instructions based on product category (PRD Section 4.2).
        Text has material properties that match the product world.
        """
        text_style_map = {
            "perfume_fragrance": "Elegant serif or script in gold/cream/white. Subtle glow or shadow for readability.",
            "beauty_wellness": "Script or serif in warm palette. Slight glossy or dewy sheen effect.",
            "water_beverage": "Bold sans-serif. Water droplet texture or translucent quality.",
            "juice_smoothie": "Bold, chunky, playful. Gradient colours matching fruit palette.",
            "dairy_milk": "Flowing script in white with liquid/cream texture. Letters appear made of milk.",
            "food_beverage": "Bold, warm-toned. Slight texture matching food surface (crispy, glazed).",
            "fashion_ecommerce": "Clean condensed sans-serif. White or metallic against atmosphere.",
            "fintech_saas_tech": "Thin geometric sans-serif. Subtle neon glow or holographic shimmer.",
        }
        return text_style_map.get(industry, "Clean sans-serif. Subtle shadow for readability. No heavy effects.")

    @staticmethod
    async def _generate_platform_image(
        platform: str,
        content: str,
        seed_content: str,
        brand_context: Optional[Dict[str, Any]] = None,
        reference_image: Optional[str] = None,
        feedback: Optional[str] = None,
        image_type: str = "post_image",
        image_model: Optional[str] = None,
        slide_index: Optional[int] = None,
        total_slides: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Generate an AI image optimized for a specific platform.
        image_type: "post_image" (default), "story" (9:16), or any key in IMAGE_SPECS[platform].
        For carousel slides: slide_index and total_slides provide context for slide numbering.
        """
        try:
            # ALWAYS use GPT-Image-2 generation mode for final graphics (PRD Section 8)
            # Background removal uses edit mode, but final generation uses generation mode
            image_model = "openai/gpt-image-2"

            specs = ImageContentService._get_platform_image_specs(platform, image_type=image_type)

            bc = brand_context or {}
            style_fragment = bc.get("style_prompt_fragment", "")
            font_prompt = bc.get("font_style_prompt", "")
            region = bc.get("region", "")
            brand_colors = bc.get("brand_colors") or []

            # Look up the style description and composition mode from the library using the slug
            style_slug = bc.get("style_slug", "")
            style_desc = ""
            composition_mode = "immersive"  # default to immersive (PRD Section 1)
            style_type = None
            if style_slug:
                from app.agents.social_media_manager.services.style_library import get_style
                s = get_style(style_slug)
                if s:
                    style_desc = s.get("description", "")
                    composition_mode = s.get("composition_mode", "immersive")
                    style_type = s.get("style_type")  # Can be "art_piece" for 9:16 posters

            # ========== ART-PIECE POSTER DETECTION (9:16 Mobile Wallpaper Format) ==========
            # Check if this is an art-piece poster based on:
            # 1. Style type is "art_piece" OR
            # 2. Trigger words in seed content (wallpaper, poster, art piece, full-page promo)
            seed_lower = seed_content.lower()
            art_piece_trigger_words = [
                "wallpaper", "poster", "art piece", "art-piece", "artpiece",
                "full-page promo", "full page promo", "mobile wallpaper",
                "phone wallpaper", "vertical poster", "9:16 poster"
            ]
            is_art_piece = (
                style_type == "art_piece" or
                any(trigger in seed_lower for trigger in art_piece_trigger_words)
            )

            # Override specs for art-piece posters (9:16 format = 1024x1792)
            if is_art_piece:
                specs = {
                    "width": 1024,
                    "height": 1792,
                    "aspect_ratio": "9:16",
                }
                print(f"🎨 Art-Piece Poster Mode Activated: 9:16 format (1024x1792)")

            # ========== RESTRUCTURED PROMPT ASSEMBLY (PRD Section 3) ==========
            # CRITICAL: Brand rules MUST come FIRST - GPT-Image-2 weights prompt beginning more heavily

            brand_name = bc.get("brand_name", "")
            color_str = ", ".join(str(c) for c in brand_colors[:4]) if brand_colors else ""

            # SECTION 1: ABSOLUTE RULES (READ FIRST)
            absolute_rules = f"""=== ABSOLUTE RULES (READ FIRST) ===
This image is EXCLUSIVELY for the brand "{brand_name}".
You must follow every instruction below precisely.
Do NOT include any other brand names, logos, products, or brand-associated imagery.
Do NOT include any real-world company, product, or trademark other than "{brand_name}".
Do NOT draw from your training data to add elements not described in this prompt.
Every element in the image must come from the instructions below. Nothing else."""

            # SECTION 2: PROFESSIONAL OUTPUT RULES
            # These rules make AI graphics look professionally designed (not AI-generated)
            primary_color = brand_colors[0] if brand_colors else "#000000"
            secondary_color = brand_colors[1] if len(brand_colors) > 1 else "#FFFFFF"
            cta_text = bc.get("default_link", "Link in bio")

            # Brand name display logic (PRD Section 4)
            # Art-piece posters ALWAYS include brand logo + tagline + badges
            show_brand_name = is_art_piece or any([
                "add our name" in seed_lower,
                "add the name" in seed_lower,
                "add our logo" in seed_lower,
                "add the logo" in seed_lower,
                "include the logo" in seed_lower,
                "include our name" in seed_lower,
                "include brand name" in seed_lower,
                "put our name" in seed_lower,
                "put the logo" in seed_lower,
                "show our name" in seed_lower,
                "show the logo" in seed_lower,
                "with our logo" in seed_lower,
                "with the logo" in seed_lower,
                "with our name" in seed_lower,
                "brand name on" in seed_lower,
                "company name on" in seed_lower,
                "add branding" in seed_lower,
                "include branding" in seed_lower,
                # Content types that traditionally include brand name
                "event flyer" in seed_lower,
                "event poster" in seed_lower,
                "flyer" in seed_lower,
                "business card" in seed_lower,
                "banner" in seed_lower,
                "announcement" in seed_lower,
                "invitation" in seed_lower,
                "menu" in seed_lower,
            ])

            brand_name_directive = ""
            if is_art_piece:
                # Art-piece posters have special branding requirements
                tagline = bc.get("tagline", "")
                tagline_text = f'\nTagline: Display "{tagline}" below the logo in complementary font.' if tagline else ""
                brand_name_directive = f'''Display the brand name "{brand_name}" prominently at the top third or top centre of the poster. Logo size: large and clear.{tagline_text}
Feature badges: Add 2-3 feature badges (e.g., "Premium Quality", "Limited Edition", "Handcrafted") near the bottom or flanking the product in elegant frames.'''
            elif show_brand_name:
                brand_name_directive = f'Display the brand name "{brand_name}" prominently in the design. Spell it exactly as shown.'
            else:
                brand_name_directive = 'Do NOT display the brand name or logo anywhere. Brand identity is expressed through colours and visual treatment only.'

            # Add slide number context for carousel slides
            slide_context = ""
            if slide_index is not None and total_slides is not None:
                slide_num = slide_index + 1  # Convert 0-indexed to 1-indexed
                slide_context = f"""
=== CAROUSEL SLIDE CONTEXT ===
This is slide {slide_num} of {total_slides} in a carousel post.
- Add a small, subtle slide indicator "({slide_num}/{total_slides})" in the bottom-right corner
- Use very small text (20-25% of CTA text size), light grey or semi-transparent white
- Position it within safe margins, not overlapping other elements
- The slide indicator helps users track their progress through the carousel
- Maintain consistent visual style across all {total_slides} slides (same colors, fonts, layout approach)
"""

            professional_rules = f"""{slide_context}=== PROFESSIONAL OUTPUT RULES ===
Follow these rules precisely for every image. No exceptions.

1. BRAND NAME: {brand_name_directive}

2. CALL-TO-ACTION: Display the following CTA text at the bottom of the image
   in small clean sans-serif text: "{cta_text}". Style it subtle but legible,
   approximately 30% the size of the headline. Position: bottom-centre or
   bottom-right within safe zone. Do NOT style it as a button or banner.

3. FONTS: Maximum 2 font styles in the entire image. One bold/heavy weight
   for headlines. One regular weight for body and CTA. Same family or
   complementary pair. No decorative, script, or novelty fonts unless
   specified in the VISUAL STYLE section.

4. TEXT AREA: Text must occupy no more than 40% of total image area. The
   visual element (photo, illustration, gradient, colour) dominates at 60%+.

5. NO FILLER: Do NOT include generic phrases like "Elevate your experience",
   "Discover the difference", "Quality you can trust", "Where excellence
   meets innovation." Every word must be specific to the actual content
   described below.

6. NO HASHTAGS: Do NOT render any hashtags on the image. Hashtags go in
   the caption text only.

7. COLOUR LIMIT: Use only these colours: {primary_color}, {secondary_color},
   and one neutral (black #000000, white #FFFFFF, or grey #888888). No other
   colours unless specified in VISUAL STYLE.

8. NO TEXT EFFECTS: No drop shadows, outer glows, bevels, neon effects, or
   emboss on text. Flat clean text only. Exception: if text sits on a
   photograph, use a subtle semi-transparent dark overlay (rgba(0,0,0,0.4))
   behind the text for readability.

9. MARGINS: Maintain at least 5% margin on all four edges. No element touches
   the image border. Generous spacing between all text blocks and elements.
   At least 15-20% of the image should be empty space.

10. NO DECORATIONS: Do NOT add badges, stickers, watermarks, decorative
    borders, corner ornaments, ribbon banners, starburst shapes, or
    promotional labels unless explicitly described in the CONTENT section.

11. FACES: If human faces appear, render with natural skin texture, slight
    asymmetry, and realistic lighting. No overly smooth or symmetrical faces.
    Hands must be hidden or in natural positions. Default to non-face visuals
    when possible.

12. OVERALL: The image must look like it was created by a professional human
    graphic designer. Restraint over excess. Clean over busy. Intentional
    over random. Every element earns its place."""

            # SECTION 3: VISUAL STYLE
            _default_style = (
                "Clean, modern typographic social media post. Bold headline text is the dominant visual element. "
                "Solid colour or subtle gradient background using brand colours. "
                "Professional flat design — NO 3D renders, NO clipart, NO generic stock illustrations. "
                "Strong visual hierarchy: large headline, smaller subheading, clean layout with generous whitespace. "
                "Text overlays must be crisp and legible at a glance."
            )
            visual_style = f"=== VISUAL STYLE ===\n{style_fragment if style_fragment else _default_style}"

            # SECTION 4: BRAND IDENTITY
            brand_identity_parts = [f"=== BRAND IDENTITY ==="]
            brand_identity_parts.append(f"Brand Name: {brand_name}")
            if color_str:
                brand_identity_parts.append(f"Brand Colors: {color_str} (use ONLY these colors)")
            if bc.get("industry"):
                brand_identity_parts.append(f"Industry: {bc.get('industry')}")
            if bc.get("tagline"):
                brand_identity_parts.append(f"Tagline: {bc.get('tagline')}")
            brand_identity = "\n".join(brand_identity_parts)

            # SECTION 4: CONTENT
            content_section = f"=== CONTENT ===\n{seed_content.strip()}"

            # SECTION 5: FORMAT & REGION
            format_parts = ["=== FORMAT ==="]
            format_parts.append(f"Platform: {platform}")
            if region:
                format_parts.append(f"Market/Region: {region}. Use settings, aesthetics, and cultural references specific to this market.")
            if font_prompt:
                format_parts.append(f"Typography: {font_prompt}")
            else:
                format_parts.append("Typography: Clean, modern sans-serif typeface. Bold headline text with strong contrast, readable subheading, professional typographic hierarchy. Text must be clearly legible.")
            format_section = "\n".join(format_parts)

            # SECTION 6: DO NOT INCLUDE (PRD Section 3.1 - Critical for preventing hallucinations)
            do_not_include_items = [
                "No other brand names, logos, or trademarks",
                "No real-world product packaging (milk cartons, soda bottles, food containers, etc.)",
                "No celebrity faces or recognisable public figures",
                "No stock photography watermarks",
                "No elements from other brands' advertising campaigns",
            ]

            # Add seasonal/contextual exclusions based on seed content
            # (seed_lower already defined above in brand name logic)
            if "mother" in seed_lower:
                do_not_include_items.extend([
                    "No milk brands (Peak Milk, Dano, Cowbell, Three Crowns, etc.)",
                    "No dairy products or baby formula",
                    "No cooking oil or food product packaging",
                ])
            if "christmas" in seed_lower or "xmas" in seed_lower:
                do_not_include_items.extend([
                    "No Coca-Cola branding or red-and-white Santa imagery",
                    "No branded soft drinks or beverages",
                ])
            if "valentine" in seed_lower:
                do_not_include_items.append("No chocolate brand packaging (Cadbury, etc.)")

            do_not_include = "=== DO NOT INCLUDE ===\n" + "\n".join(f"- {item}" for item in do_not_include_items)

            # ASSEMBLE FINAL PROMPT (Order matters - rules at top!)
            parts = [
                absolute_rules,
                professional_rules,
                visual_style,
                brand_identity,
                content_section,
                format_section,
                do_not_include,
            ]
            image_prompt = "\n\n".join(p for p in parts if p)

            if not image_prompt:
                image_prompt = seed_content.strip()

            # ========== PRODUCT PRESERVATION PIPELINE (PRD: Product-Preservation-Pipeline) ==========
            # When reference_image provided: forensic analysis + preservation block
            # This is the KEY innovation that prevents product distortion
            product_preservation_block = ""
            cutout_url = reference_image  # Default to original if background removal fails

            if reference_image:
                try:
                    print(f"\n{'='*60}")
                    print(f"🔬 PRODUCT PRESERVATION PIPELINE ACTIVATED")
                    print(f"{'='*60}")

                    # Step 1: Background removal (get clean product cutout)
                    from app.utils.background_removal import remove_background
                    cutout_url = await remove_background(reference_image, method="auto")
                    print(f"✂️  Background removed: {cutout_url[:80]}...")

                    # Step 2: Forensic product analysis (the key innovation)
                    from app.agents.social_media_manager.services.product_analysis_service import ProductAnalysisService
                    product_spec = await ProductAnalysisService.analyze_product_forensically(cutout_url)

                    # Step 3: Build preservation block
                    product_preservation_block = ProductAnalysisService.build_preservation_block(product_spec)

                    print(f"✅ Product preservation block generated ({len(product_preservation_block)} chars)")
                    print(f"{'='*60}\n")

                except Exception as e:
                    print(f"⚠️ Product preservation pipeline error: {str(e)}")
                    print(f"   Falling back to standard reference image handling")
                    # Continue with original reference_image, no preservation block

            # Add composition block based on style's composition_mode (Immersive Composition System PRD)
            # When a reference image is provided, choose composition style based on the visual style
            if reference_image:
                # Get industry and dynamic details for immersive mode
                industry = bc.get("industry", "general_other")
                dynamic_motion = ImageContentService._get_dynamic_motion_detail(industry)
                text_styling = ImageContentService._get_text_styling_detail(industry)

                if composition_mode == "editorial":
                    # Editorial/Two-Zone mode for minimal/clean styles
                    composition_block = """
=== EDITORIAL COMPOSITION ===
Create a professional social media product graphic with TWO distinct zones:

PRODUCT ZONE (55% of frame, positioned left or right):
- The product from the reference image, placed slightly off-center
- The product must appear EXACTLY as it looks in the reference photo
- Same shape, same colours, same label, same proportions
- Professional background, surface, and optional styling props AROUND the product
- Background can be: solid colour, gradient, textured surface, or styled scene

TEXT ZONE (45% of frame, opposite side of product):
- Clean background (solid colour, gradient, or subtle texture)
- All text placed in this zone: headline, subtext, CTA
- Text must be in the NEGATIVE SPACE beside the product
- NO text overlapping or on top of the product
- 20px minimum gap between any text and the product

CRITICAL RULES:
- The product itself is SACRED - never distort, never regenerate, never modify
- Everything AROUND the product is AI-generated professional styling
- Text lives BESIDE the product in negative space, never on top of it
- The two zones must not overlap
- Product label/branding on the actual product must be clearly visible"""
                else:
                    # Immersive mode (default) - product exists INSIDE a 360° environment
                    composition_block = f"""
=== IMMERSIVE COMPOSITION ===
Create a professional social media product graphic where the product
exists INSIDE a three-dimensional environment.

PRODUCT:
- Gravitational centre of the image, slightly off-centre (40/60 split)
- SHARPEST element in the image. Everything else can be softer.
- Appears EXACTLY as in the reference photo. NOT regenerated.
- Can be slightly angled for dynamic energy.

ENVIRONMENT (wraps around product from ALL directions):
- BEHIND: Atmospheric depth - light falloff, haze, bokeh, context
- BELOW: Grounded - natural surface, liquid, ingredients
- SIDES: Context elements at varying distances for depth layers
- ABOVE: Atmospheric space where text lives. Sky, light, particles.
- IN FRONT: Subtle foreground blur at bottom/edges of frame

DYNAMIC MOTION:
{dynamic_motion}
- High-speed photography feel: sharp, detailed, frozen at 1/2000th

TEXT:
- Floats in natural pockets of atmospheric space
- Part of the scene, not pasted on top
- {text_styling}
- NEVER overlaps product label
- Max 3 elements: headline (5 words), subtext (optional), CTA
- CTA at the bottom, integrated into the scene

DEPTH AND ATMOSPHERE:
- THREE depth layers: soft foreground, sharp product, atmospheric bg
- ONE clear directional light source with highlights and shadows
- Rich tonal range: no pure black, no pure white
- Micro-details: particles, condensation, texture, reflections
- ONE unified colour temperature throughout

OVERALL:
- Frozen moment in a living world, not a composited product photo
- The viewer should FEEL something: desire, freshness, energy, warmth
- Should stop a scroll and make someone look closer"""

                image_prompt = image_prompt.rstrip() + "\n" + composition_block

            # ========== PREPEND PRESERVATION BLOCK (CRITICAL: Must come first) ==========
            # The preservation block must be at the BEGINNING so GPT-Image-2 weights it heavily
            if product_preservation_block:
                image_prompt = product_preservation_block + "\n\n" + image_prompt
                print(f"📌 Preservation block prepended to prompt (total: {len(image_prompt)} chars)")

            # ========== IMAGE GENERATION DEBUG (PRD Section 2) ==========
            from datetime import datetime
            print(f"\n{'='*60}")
            print(f"IMAGE GENERATION DEBUG")
            print(f"{'='*60}")
            print(f"Timestamp: {datetime.utcnow().isoformat()}")
            print(f"User ID: {bc.get('user_id', 'MISSING')}")
            print(f"Brand Name: {bc.get('brand_name', 'MISSING')}")
            print(f"Brand Colors: {brand_colors or 'MISSING'}")
            print(f"Region: {region or 'MISSING'}")
            print(f"Style Slug: {style_slug or 'MISSING'}")
            print(f"Style Fragment Length: {len(style_fragment) if style_fragment else 0}")
            print(f"Font Prompt Length: {len(font_prompt) if font_prompt else 0}")
            print(f"Seed Content: {seed_content[:100]}...")
            print(f"---")

            # VALIDATION: Check for undefined/null in prompt (PRD Section 2 Step 2)
            if "undefined" in image_prompt:
                print(f"❌ ERROR: Prompt contains 'undefined' - variable substitution failed")
                for i, line in enumerate(image_prompt.split('\n')):
                    if 'undefined' in line:
                        print(f"  Line {i}: {line}")
                raise ValueError("Prompt contains undefined values - aborting image generation")

            if "null" in image_prompt:
                print(f"❌ ERROR: Prompt contains 'null' - database field is null")
                raise ValueError("Prompt contains null values - aborting image generation")

            # VALIDATION: Check prompt length (PRD Section 2 Step 5)
            if len(image_prompt) < 200:
                print(f"⚠️  WARNING: Prompt too short ({len(image_prompt)} chars) - high hallucination risk")
                print(f"⚠️  Minimum recommended: 400 chars")

            print(f"Prompt Length: {len(image_prompt)} chars")
            print(f"\n{'━'*60}\n"
                f"📤 FINAL PROMPT → GPT-Image-2 [{platform.upper()}] "
                f"({'with style' if style_fragment else 'no style'}"
                f"{' + font' if font_prompt else ''})\n"
                f"{'━'*60}\n"
                f"{image_prompt}\n"
                f"{'━'*60}\n"
            )

            # Use cutout_url (background-removed) if preservation pipeline ran, otherwise original
            final_reference_image = cutout_url if (reference_image and cutout_url != reference_image) else reference_image

            image_response = await ImageContentService._call_dalle_api(
                prompt=image_prompt,
                size=f"{specs['width']}x{specs['height']}",
                reference_image=final_reference_image,
                image_model=image_model,
            )

            if image_response.get('success'):
                # Composite brand logo onto generated image for all models.
                logo_url = (brand_context or {}).get('logo_url')
                if logo_url:
                    import re as _re_logo
                    logo_position = (brand_context or {}).get('logo_position', 'bottom_right')
                    print(f"🖼️  OVERLAY DEBUG: logo_position={repr(logo_position)}, brand_context_keys={list((brand_context or {}).keys())}")
                    data_url = image_response['url']
                    _m = _re_logo.match(r"data:[^;]+;base64,(.+)", data_url, _re_logo.DOTALL)
                    if _m:
                        loop = asyncio.get_running_loop()
                        b64_final = await loop.run_in_executor(
                            None,
                            lambda: ImageContentService._overlay_logo(_m.group(1), logo_url, logo_position)
                        )
                        image_response['url'] = f"data:image/webp;base64,{b64_final}"

                return UriResponse.get_single_data_response("platform_image", {
                    "image_url": image_response['url'],
                    "platform": platform,
                    "specs": specs,
                    "prompt_used": image_prompt,
                    "generated_at": datetime.utcnow().isoformat()
                })
            else:
                return UriResponse.error_response(f"DALL-E generation failed: {image_response.get('error')}")

        except Exception as e:
            return UriResponse.error_response(f"Platform image generation failed: {str(e)}")

    @staticmethod
    async def _enhance_prompt_for_gpt_image2(
        seed_content: str,
        platform: str,
        specs: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        GPT-4o-mini thinking layer: expands the user's casual request into a
        detailed image spec before calling GPT-Image-2. Compensates for the
        API's lack of built-in reasoning (the gap vs ChatGPT's thinking mode).
        Falls back to raw seed_content if the call fails.
        """
        try:
            from app.services.AIService import client as _oai_client

            w = (specs or {}).get("width", 1024)
            h = (specs or {}).get("height", 1024)
            orientation = "landscape" if w > h else ("portrait" if h > w else "square")

            system_prompt = (
                "You are an expert art director and prompt engineer. "
                "Your job is to take a user's casual image request and expand it into a rich, "
                "detailed image generation prompt optimized for GPT-Image-2. "
                "Output a single paragraph (3-5 sentences) describing the scene, subjects, "
                "composition, lighting, color palette, mood, and visual style. "
                "Be specific and vivid. Avoid vague adjectives like 'beautiful' or 'amazing'. "
                "Output only the prompt text — no explanation, no preamble, no quotes."
            )

            user_message = (
                f"Platform: {platform} ({orientation}, {w}x{h})\n"
                f"User request: {seed_content}\n\n"
                "Write a detailed image generation prompt:"
            )

            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: _oai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message},
                    ],
                    max_tokens=300,
                    temperature=0.7,
                ),
            )
            enhanced = response.choices[0].message.content.strip()
            print(f"🧠 GPT-Image-2 prompt enhanced ({len(enhanced)} chars): {enhanced[:100]}…")
            return enhanced
        except Exception as _e:
            print(f"⚠️ GPT-Image-2 prompt enhancer failed: {_e} — using raw seed content")
            return seed_content.strip()

    @staticmethod
    async def _generate_image_brief(
        content: str,
        seed_content: str,
        platform: str,
        brand_context: Optional[Dict[str, Any]] = None,
        specs: Optional[Dict[str, Any]] = None,
        reference_image: Optional[str] = None,
        feedback: Optional[str] = None,
        style_fragment: str = "",
        font_prompt: str = "",
    ) -> Optional[str]:
        """
        Use GPT-5.4 to select the most appropriate image type for the content,
        then write a detailed flowing prompt for GPT-Image-2.
        style_fragment and font_prompt are injected as hard creative directives
        so they are reliably woven into the final image prompt.

        Image types the AI can choose from:
          PHOTO          — Authentic photorealistic documentary photograph
          POSTER         — Bold graphic design poster with brand colors
          STAT_CARD      — Clean typographic card featuring a key number/quote
          PRODUCT_SHOWCASE — Editorial product or service mockup
          INFOGRAPHIC    — Visual process, comparison, or data layout
          BRAND_ILLUSTRATION — Modern flat/semi-realistic illustrated scene
        """
        try:
            from app.services.AIService import client as ai_client

            aspect = specs.get('format', 'landscape') if specs else 'landscape'

            # ── Extract every available brand context field ───────────────────
            bc = brand_context or {}

            industry_name        = str(bc.get('industry') or 'business')
            brand_name           = bc.get('brand_name', '')
            tagline              = bc.get('tagline', '')
            business_description_raw = bc.get('business_description', '')
            voice_sample         = bc.get('voice_sample', '')
            brand_voice          = bc.get('brand_voice', '')
            target_audience      = bc.get('target_audience', '')
            audience_age_range   = bc.get('audience_age_range', '')
            primary_goal         = bc.get('primary_goal', '')
            region               = bc.get('region', '')
            brand_colors_str     = ', '.join(str(c) for c in (bc.get('brand_colors') or []))
            key_products_str     = ', '.join(str(p) for p in (bc.get('key_products_services') or [])[:5])
            cta_styles           = ', '.join(bc.get('cta_styles') or [])
            key_dates            = bc.get('key_dates', '')
            preferred_formats    = ', '.join(bc.get('preferred_formats') or [])
            website              = bc.get('website', '')
            guardrails_raw       = bc.get('guardrails') or {}
            if isinstance(guardrails_raw, dict):
                _g_parts = []
                if guardrails_raw.get('avoid_topics'):
                    _g_parts.append(f"avoid: {guardrails_raw['avoid_topics']}")
                if guardrails_raw.get('banned_words'):
                    _g_parts.append(f"banned words: {guardrails_raw['banned_words']}")
                if guardrails_raw.get('compliance_notes'):
                    _g_parts.append(guardrails_raw['compliance_notes'])
                guardrails_str = '; '.join(_g_parts)
            else:
                guardrails_str = '; '.join(str(g) for g in list(guardrails_raw)[:6]) if guardrails_raw else ''
            sample_template_urls = [u for u in (bc.get('sample_template_urls') or []) if u and isinstance(u, str)][:3]

            # Content pillars → use as content themes for image brief
            pillars = bc.get('content_pillars') or []
            themes_str = ', '.join(
                t.get('theme', '') if isinstance(t, dict) else str(t)
                for t in pillars[:4]
            ) if pillars else ''

            # industry_overview — synthesised from business description + products
            industry_overview = business_description_raw
            if key_products_str and not industry_overview:
                industry_overview = key_products_str

            business_description = industry_name
            if themes_str:
                business_description += f' (topics: {themes_str})'
            if industry_overview:
                business_description += f' — {industry_overview[:180]}'

            # Platform visual tendencies (guide the type selection)
            platform_notes = {
                "linkedin": (
                    "LinkedIn audiences respond well to: editorial photographs of real work moments, "
                    "bold stat cards with a striking number, infographics explaining a process or result, "
                    "or clean brand posters for announcements."
                ),
                "instagram": (
                    "Instagram audiences respond well to: warm lifestyle photographs, "
                    "aesthetic brand posters with strong color, product showcases, "
                    "illustrated scenes, or motivational quote cards."
                ),
                "facebook": (
                    "Facebook audiences respond well to: relatable community photographs, "
                    "bold announcement posters, stat cards celebrating milestones, "
                    "or illustrated explainer graphics."
                ),
                "twitter": (
                    "Twitter/X audiences respond well to: high-contrast photojournalism, "
                    "bold stat cards with one punchy number, or sharp brand posters."
                ),
            }
            platform_note = platform_notes.get(platform, platform_notes["instagram"])

            # ── Build brand context block for the image prompt ────────────────
            brand_lines = []
            # brand_name intentionally excluded — do NOT add business name text to images
            if tagline:
                brand_lines.append(f"Tagline: \"{tagline}\" — let this inform the aspirational feeling of the image.")
            if business_description_raw:
                brand_lines.append(f"Business: {business_description_raw}")
            if key_products_str:
                brand_lines.append(f"Key products/services: {key_products_str} — show the most relevant one visually.")
            if brand_colors_str:
                brand_lines.append(
                    f"Brand colors: {brand_colors_str} — these MUST appear prominently in the image. "
                    f"For graphic types (POSTER, STAT_CARD, INFOGRAPHIC, BRAND_ILLUSTRATION), use them as the dominant palette. "
                    f"For PHOTO or PRODUCT_SHOWCASE, incorporate them in clothing, props, or environmental accents."
                )
            if brand_voice:
                brand_lines.append(
                    f"Brand personality: {brand_voice} — the mood, energy, and composition of the image must reflect this."
                )
            if target_audience:
                brand_lines.append(
                    f"Target audience: {target_audience} — any people shown should match this demographic."
                )
            if audience_age_range:
                brand_lines.append(
                    f"Audience age range: {audience_age_range} — people and settings in the image should feel relatable to this age group."
                )
            if primary_goal:
                brand_lines.append(
                    f"Brand goal: {primary_goal} — the image should visually reinforce this aspiration."
                )
            if region:
                brand_lines.append(
                    f"Market region: {region} — use settings, aesthetics, and cultural cues specific to this region."
                )
            if preferred_formats:
                brand_lines.append(
                    f"Preferred content formats: {preferred_formats} — let this guide the visual style chosen."
                )
            if themes_str:
                brand_lines.append(
                    f"Content pillars/themes: {themes_str} — the image should visually anchor to the most relevant one."
                )
            if key_dates:
                brand_lines.append(
                    f"Upcoming key dates: {key_dates} — if relevant, let the image reflect a seasonal or event context."
                )
            if voice_sample:
                brand_lines.append(
                    f"Brand voice sample: \"{voice_sample[:200]}\" — let the tone and style of this writing inform the image's mood."
                )
            if cta_styles:
                brand_lines.append(
                    f"Call-to-action styles used by this brand: {cta_styles} — the image composition should naturally lead the eye toward action."
                )
            if website:
                brand_lines.append(
                    f"Website: {website} — for HEADLINE or FULL text level images, include this in small text as a URL/CTA element."
                )
            if guardrails_str:
                brand_lines.append(
                    f"Brand guardrails (must follow): {guardrails_str} — these are hard constraints the brand has set. Respect them in the image."
                )
            brand_block = (
                "\n\nBRAND CONTEXT:\n" + "\n".join(brand_lines)
                if brand_lines else ""
            )

            # Hard style + font directives — these are non-negotiable constraints that
            # must be woven into the FINAL_PROMPT, not overridden by creative choices.
            style_directive_block = ""
            if style_fragment:
                style_directive_block += (
                    f"\n\n🎨 MANDATORY VISUAL STYLE DIRECTIVE (non-negotiable — this overrides your default type choice):\n"
                    f"{style_fragment}\n"
                    f"Your FINAL_PROMPT MUST be written to produce an image that matches this exact visual style. "
                    f"Every word of your prompt should serve this direction."
                )
            if font_prompt:
                style_directive_block += (
                    f"\n\n✏️ MANDATORY TYPOGRAPHY DIRECTIVE (non-negotiable):\n"
                    f"{font_prompt}\n"
                    f"If the image includes any text or typographic elements, they must follow this direction exactly."
                )

            system_prompt = (
                "You are a world-class creative director and AI image prompt engineer at a top African brand agency. "
                "Your job is to commission visually stunning, commercially ready images for social media — "
                "the kind that appear in real campaigns by Flutterwave, Paystack, Moniepoint, and MTN. "
                "You brief GPT-Image-2, a state-of-the-art image generation model.\n\n"

                "GPT-Image-2 performs best with flowing, scene-rich natural-language prompts — "
                "NOT structured notes or labeled sections. Your final deliverable is a single master prompt "
                "that reads like a director's brief to a photographer and art director simultaneously.\n\n"

                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "STEP 1 — Pick the best image type:\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

                "  PHOTO              — Premium editorial photograph. Real people, real action.\n"
                "                       Best for: human stories, culture, community, behind-the-scenes.\n\n"
                "  POSTER             — Graphic design poster. Bold brand colors, clean layout.\n"
                "                       Best for: campaigns, launches, announcements, promotions.\n\n"
                "  STAT_CARD          — Typographic impact card. A single key number or quote is the hero.\n"
                "                       Best for: milestones, data, achievements.\n\n"
                "  PRODUCT_SHOWCASE   — Editorial product or service visual. Luxury magazine quality.\n"
                "                       Best for: product reveals, service spotlights.\n\n"
                "  BRAND_ILLUSTRATION — Modern flat or semi-realistic illustrated scene.\n"
                "                       Best for: abstract values, concepts, lifestyle.\n\n"

                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "STEP 2 — Decide text approach:\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

                "PHOTO / BRAND_ILLUSTRATION: NEVER include text overlays — the caption carries the message.\n\n"

                "POSTER:\n"
                "  NONE — Striking visual alone, no text. Most often the right choice.\n"
                "  BRAND_ONLY — Website URL only in tiny text (e.g. 'urisocial.com'). No brand name written out.\n"
                "  HEADLINE — One 4-6 word bold headline from the post + website URL in small text. No brand name written out.\n"
                "  FULL — Headline + subtext + website URL. Only for formal announcements. No brand name written out.\n\n"

                "STAT_CARD: Always show the key number/stat + short label. Website URL optional in small text.\n"
                "PRODUCT_SHOWCASE: Website URL only in small text, optional.\n\n"
                "CRITICAL: NEVER write the business name or brand name as text on the image. "
                "Logo overlays are handled separately in post-processing — do NOT add any logo or brand name text.\n\n"

                "SAFE ZONE — ABSOLUTE RULE: The image will be center-cropped to fit the target aspect ratio. "
                "The top 15% and bottom 15% of the canvas are the CROP DANGER ZONE — treat them as if they do not exist. "
                "ALL important content — faces, eyes, the main subject, key objects, text overlays, logos — "
                "must be fully contained within the center 70% of the canvas vertically. "
                "Leave the top 15% and bottom 15% as plain background, gradient, texture, or sky. "
                "This is not a suggestion — any subject or element that extends into these margins WILL be cut off. "
                "Compose the shot so everything meaningful is in the middle vertical band.\n\n"

                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "STEP 3 — Write the reasoning sections (internal):\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

                "TYPE: [chosen type]\n"
                "TEXT_LEVEL: [NONE | BRAND_ONLY | HEADLINE | FULL | N/A]\n"
                "PALETTE_NOTES: [Describe brand colors in words only — NO hex codes. "
                "e.g. 'deep magenta, ivory white, muted gold'. Explain where each appears.]\n"
                "SCENE_NOTES: [Describe the setting, subject, action, and composition in detail.]\n"
                "QUALITY_NOTES: [Camera, light, mood, finish standard for this image type.]\n\n"

                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "STEP 4 — Write the FINAL_PROMPT (the only part sent to the image model):\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

                "FINAL_PROMPT: [A single flowing paragraph of 200-260 words that Imagen 4 Ultra will "
                "render directly. Rules:\n"
                "• Open with the image type and format (e.g. 'Premium editorial photograph,' or 'Bold graphic design poster,')\n"
                "• COMPOSITION FIRST: explicitly state that the subject is centered vertically in the middle 70% of the frame, "
                "with open sky/background/gradient filling the top and bottom 15% — e.g. 'Subject centered in the mid-frame, "
                "upper and lower 15% of canvas left as open gradient background.'\n"
                "• Describe the subject with cinematic specificity: person's age range, skin tone, hair, exact clothing, "
                "expression, and precise action — never generic, always specific\n"
                "• Describe the setting with architectural and environmental detail — specific city district, "
                "time of day, light source and direction, material textures\n"
                "• For PHOTO: include camera model, lens, aperture, and colour grade\n"
                "• Brand colors described in words only (NO hex codes) — say 'deep magenta' not '#CD1B78'\n"
                "• For text-bearing types: specify the EXACT words (headline text and/or website URL only — "
                "NEVER the brand name), font style (bold condensed sans-serif / "
                "display serif), relative size, and placement in the lower half or centre of frame\n"
                "• Nigerian/West African cultural context always: Lagos or Abuja settings, warm dark-brown "
                "complexion, natural or protective hairstyles, culturally appropriate styling\n"
                "• End with quality standard: 'No watermarks, no logos, no stock-photo stiffness, "
                "no CGI render. Publishable in [relevant premium publication].'\n"
                "• No labels, no sections, no parenthetical notes — pure flowing prose only]"
            )

            feedback_block = (
                f"\n\n⚠️ USER FEEDBACK ON PREVIOUS IMAGE — YOU MUST INCORPORATE THIS:\n{feedback.strip()}\n"
                "This feedback overrides your default choices. Adjust the image type, scene, style, "
                "and composition to directly address these notes."
                if feedback else ""
            )

            ref_instruction = (
                f"USER'S INSTRUCTION FOR THE REFERENCE IMAGE: {seed_content[:400]}\n\n"
                if reference_image and seed_content else ""
            )

            user_prompt = (
                f"PLATFORM: {platform} ({aspect} format)\n"
                f"PLATFORM GUIDANCE: {platform_note}\n\n"
                f"{ref_instruction}"
                f"POST CONTENT TO VISUALIZE:\n{content[:700]}\n\n"
                f"Original business topic: {seed_content[:300]}\n\n"
                f"{brand_block}{style_directive_block}{feedback_block}\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "YOUR TASK:\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "1. Choose the image type that will make this post most compelling and scroll-stopping "
                "on this specific platform.\n"
                "2. If POSTER — be honest about TEXT_LEVEL. A bold visual with NONE often outperforms "
                "a cluttered poster with text. Only use HEADLINE or FULL for a specific offer or launch.\n"
                "3. In PALETTE_NOTES and SCENE_NOTES, use every piece of brand context — never leave "
                "anything generic. If the post topic is short, pull from business description, products, "
                "audience, and region to enrich the scene.\n"
                f"4. Brand colors ({brand_colors_str if brand_colors_str else 'from brand identity'}) "
                "must appear prominently — described in words only, no hex codes.\n"
                "5. In FINAL_PROMPT: write a single flowing paragraph of cinematic richness. "
                "This paragraph is fed DIRECTLY to GPT-Image-2 — it must be vivid, specific, "
                "and commercially ready. Every word counts. Describe things the camera would see, "
                "not abstract concepts. No labels, no sections — pure prose only. "
                + (
                    "The MANDATORY VISUAL STYLE DIRECTIVE above must be the foundation of your FINAL_PROMPT — "
                    "every scene, lighting, composition, and typography decision must serve that style. "
                    if style_fragment else ""
                )
            )

            logo_url = brand_context.get("logo_url") if brand_context else None

            # Pre-fetch external images as base64 data URLs so OpenAI vision doesn't
            # need to download from imgBB (which times out frequently).
            async def _fetch_as_data_url(url: str) -> Optional[str]:
                if not url:
                    return None
                if url.startswith("data:"):
                    return url  # already inline
                try:
                    import httpx as _httpx
                    import base64 as _b64
                    import mimetypes as _mt
                    async with _httpx.AsyncClient(timeout=15) as _c:
                        r = await _c.get(url)
                        r.raise_for_status()
                    content_type = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
                    data = _b64.b64encode(r.content).decode()
                    return f"data:{content_type};base64,{data}"
                except Exception as _e:
                    print(f"⚠️  Could not pre-fetch image for vision ({url[:60]}…): {_e}")
                    return None

            # Build user message — attach logo + sample templates + reference image as vision
            # so GPT-5.4 can extract brand identity and user-provided contextual details.
            has_vision = logo_url or sample_template_urls or reference_image
            if has_vision:
                vision_note_parts = []
                if reference_image:
                    vision_note_parts.append(
                        "A user-uploaded REFERENCE IMAGE is attached. "
                        "⚠️ CRITICAL RULES for the FINAL_PROMPT:\n"
                        "• This image will be used as the BASE for an image-editing model (gpt-image-1 edit endpoint).\n"
                        "• The product, garment, object, or item shown in the reference image MUST appear in the output "
                        "EXACTLY as it is — same design, same colours, same texture, same details. It must not be altered, reimagined, or replaced.\n"
                        "• You MAY expand the scene beyond the reference: add a person wearing/holding/using the product, "
                        "add a setting or background, add brand design overlays, add text — but ONLY if the user's prompt requests it.\n"
                        "• Read the 'Original business topic' field carefully — that is the user's explicit instruction for what to do with the image.\n"
                        "• FRAMING — ABSOLUTELY NO CROPPING: The entire subject must be fully visible in the frame. "
                        "If the image contains a person, their full body must be shown from head to toe — no cutting off at the waist, knees, or ankles. "
                        "If the image contains clothing or a garment, the entire garment must be visible — no cropping of hemlines, sleeves, collars, or any part of the outfit. "
                        "Frame the shot wide enough to show everything completely with comfortable breathing room around the subject. "
                        "Use phrases like 'full body shot', 'full-length view', 'entire outfit visible from head to toe', 'wide enough frame to show the complete garment' in your FINAL_PROMPT.\n"
                        "• Write the FINAL_PROMPT as a direct edit instruction to the image model. "
                        "Be specific: describe the EXACT product from the reference (its colours, shape, details) and what to add or place around it. "
                        "Example for a dress: 'Full-length shot — a Black woman with a natural afro wearing the exact navy blue wrap dress from the reference image — "
                        "same fabric pattern, same belt, same silhouette — full body visible from head to toe, standing in a sunlit Lagos boutique. The dress is unchanged, entire garment shown.' "
                        "Example for a product: 'The white ceramic coffee mug from the reference image, exact as shown, fully visible, held in the hands of a young professional "
                        "at a modern Lagos office desk. Do not alter the mug.'\n"
                        "• Never describe the reference as a 'scene to inspire' — treat it as the definitive source of truth for the product.\n"
                        "• Never use tight crops, close-ups, or portrait framing that would cut off any part of the clothing or subject."
                    )
                if logo_url:
                    vision_note_parts.append(
                        "A brand logo image is attached. Analyse its colors, shapes, and visual "
                        "style and let these directly inform the color palette and overall aesthetic."
                    )
                if sample_template_urls:
                    vision_note_parts.append(
                        f"{len(sample_template_urls)} brand design template(s) are attached. "
                        "Study their layout, typography style, color application, spacing, and visual hierarchy. "
                        "Your prompt should produce an image that feels like a natural extension of these templates — "
                        "same energy, same visual language, same brand identity."
                    )
                vision_note = "\n\n" + " ".join(vision_note_parts)

                user_message_content = [{"type": "text", "text": user_prompt + vision_note}]
                # Reference image goes first so it is the primary focus
                if reference_image:
                    ref_data = await _fetch_as_data_url(reference_image)
                    if ref_data:
                        user_message_content.append({"type": "image_url", "image_url": {"url": ref_data}})
                if logo_url:
                    logo_data = await _fetch_as_data_url(logo_url)
                    if logo_data:
                        user_message_content.append({"type": "image_url", "image_url": {"url": logo_data}})
                for tmpl_url in sample_template_urls:
                    tmpl_data = await _fetch_as_data_url(tmpl_url)
                    if tmpl_data:
                        user_message_content.append({"type": "image_url", "image_url": {"url": tmpl_data}})
                # If none of the images could be fetched, fall back to plain text
                if len(user_message_content) == 1:
                    user_message_content = user_prompt
            else:
                user_message_content = user_prompt

            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: ai_client.chat.completions.create(
                    model="gpt-5.4",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message_content}
                    ],
                    max_completion_tokens=1600,
                    temperature=0.85
                )
            )
            brief = response.choices[0].message.content.strip()

            # Strip hex color codes — image models render them as literal text in the image.
            import re as _re_hex
            brief_no_hex = _re_hex.sub(r'#[0-9A-Fa-f]{3,6}\b', '', brief)
            brief_no_hex = _re_hex.sub(r'  +', ' ', brief_no_hex).strip()

            # Extract only the FINAL_PROMPT section to send to the image model.
            # GPT reasons through the structure but only the flowing prose prompt
            # gets sent to Imagen — it performs dramatically better this way.
            final_prompt_match = _re_hex.search(
                r'FINAL_PROMPT:\s*(.*?)(?:\n\n[A-Z_]+:|$)',
                brief_no_hex,
                _re_hex.DOTALL
            )
            if final_prompt_match:
                brief_clean = final_prompt_match.group(1).strip()
            else:
                # Fallback: try a more lenient extraction (everything after FINAL_PROMPT:)
                if 'FINAL_PROMPT:' in brief_no_hex:
                    brief_clean = brief_no_hex.split('FINAL_PROMPT:', 1)[1].strip()
                else:
                    brief_clean = brief_no_hex

            chosen_type = 'UNKNOWN'
            type_match = _re_hex.search(r'TYPE:\s*(\w+)', brief_no_hex)
            if type_match:
                chosen_type = type_match.group(1).strip()

            # Diagnostic: show which brand fields were available for this generation
            field_status = {
                "brand_name": bool(brand_name),
                "tagline": bool(tagline),
                "business_desc": bool(business_description_raw),
                "products": bool(key_products_str),
                "colors": bool(brand_colors_str),
                "voice": bool(brand_voice),
                "voice_sample": bool(voice_sample),
                "audience": bool(target_audience),
                "age_range": bool(audience_age_range),
                "goal": bool(primary_goal),
                "region": bool(region),
                "pillars": bool(themes_str),
                "formats": bool(preferred_formats),
                "cta": bool(cta_styles),
                "key_dates": bool(key_dates),
                "website": bool(website),
                "guardrails": bool(guardrails_str),
                "logo": bool(logo_url),
                "templates": len(sample_template_urls),
            }
            filled = [k for k, v in field_status.items() if v and k != "templates"]
            missing = [k for k, v in field_status.items() if not v and k != "templates"]
            tmpl_count = field_status["templates"]

            vision_refs = []
            if reference_image:
                vision_refs.append("user reference image")
            if logo_url:
                vision_refs.append("logo")
            if tmpl_count:
                vision_refs.append(f"{tmpl_count} template(s)")
            vision_ref_note = f" | vision refs: {', '.join(vision_refs)}" if vision_refs else ""

            print(f"\n{'━'*60}")
            print(f"🎨 IMAGEN PROMPT — {platform.upper()} | type: {chosen_type}{vision_ref_note}")
            print(f"   ✅ fields used ({len(filled)}): {', '.join(filled)}")
            if missing:
                print(f"   ⚠️  fields missing ({len(missing)}): {', '.join(missing)}")
            print(f"   📝 prompt length: {len(brief_clean)} chars")
            print(f"{'━'*60}")
            print(brief_clean)
            print(f"{'━'*60}\n")
            return brief_clean

        except Exception as e:
            print(f"⚠️ Image brief generation failed, using static prompt: {e}")
            return None
    
    # Best-performing default image type per platform
    PLATFORM_DEFAULT_TYPE = {
        "instagram": "post_portrait",   # 4:5 — highest organic reach on Instagram
        "linkedin":  "post_image",      # 1.91:1 — LinkedIn standard
        "twitter":   "post_image",      # 16:9 — Twitter/X standard
        "facebook":  "post_portrait",    # 4:5 — matches Instagram for consistent cross-posting
    }

    @staticmethod
    def _get_platform_image_specs(platform: str, image_type: str = "post_image") -> Dict[str, Any]:
        """Get optimal image specifications for platform, using best-performing defaults."""
        platform_specs = ImageContentService.IMAGE_SPECS.get(platform, {})

        # Use platform-specific best default if caller didn't specify
        if image_type == "post_image":
            preferred = ImageContentService.PLATFORM_DEFAULT_TYPE.get(platform, "post_image")
        else:
            preferred = image_type

        if preferred in platform_specs:
            return platform_specs[preferred]
        elif "post_image" in platform_specs:
            return platform_specs["post_image"]
        elif platform_specs:
            return list(platform_specs.values())[0]
        else:
            return {"width": 1200, "height": 630, "format": "landscape"}
    
    @staticmethod
    def _create_image_prompt(
        content: str,
        seed_content: str,
        platform: str,
        brand_context: Optional[Dict[str, Any]] = None,
        specs: Dict[str, Any] = None
    ) -> str:
        """
        Fallback static prompt. Rotates between image types based on a hash of
        the content so the same post doesn't always produce the same style.
        """
        import hashlib
        industry = brand_context.get('industry', 'business') if brand_context else 'business'
        aspect = specs.get('format', 'landscape') if specs else 'landscape'

        # Extract brand fields (static fallback)
        import re as _re_hex_fb
        bc = brand_context or {}
        colors    = bc.get('brand_colors') or []
        color_list = ', '.join(str(c) for c in colors[:3]) if colors else ''
        # Strip hex codes — image models render them as literal text
        color_list = _re_hex_fb.sub(r'#[0-9A-Fa-f]{3,6}\b', '', color_list).strip().strip(',')
        audience   = bc.get('target_audience', '')
        region_fb  = bc.get('region', '')
        products   = bc.get('key_products_services') or []
        brand_name = bc.get('brand_name', '')
        brand_voice = bc.get('brand_voice', '')
        tagline_fb  = bc.get('tagline', '')
        primary_goal_fb = bc.get('primary_goal', '')
        logo_url   = bc.get('logo_url')

        color_note = (
            f"Brand colors ({color_list}) must appear in the dominant palette. "
            if color_list else ""
        )
        logo_note = (
            "The brand logo's visual identity (shapes, colors, style) should inform the overall aesthetic. "
            if logo_url else ""
        )
        audience_note   = f"Audience: {audience[:120]}. " if audience else ""
        region_note     = f"Regional setting: {region_fb}. " if region_fb else ""
        product_note    = f"Show {products[0]} prominently. " if products else ""
        tagline_note    = f'Aspirational feeling: "{tagline_fb}". ' if tagline_fb else ""
        goal_note       = f"Brand goal: {primary_goal_fb}. " if primary_goal_fb else ""
        voice_note      = f"Mood and energy should feel: {brand_voice}. " if brand_voice else ""

        # Pick image type by rotating deterministically on content hash
        content_hash = int(hashlib.md5(content[:100].encode()).hexdigest(), 16)
        image_types = ['photo', 'poster', 'stat_card', 'brand_illustration']
        image_type = image_types[content_hash % len(image_types)]

        scene = ImageContentService._extract_visual_concepts(content, seed_content)

        if image_type == 'poster':
            brand_ref = f"{brand_name}" if brand_name else industry
            return (
                f"COLOR_PALETTE: {color_list if color_list else 'deep navy, warm amber, white'} — "
                f"these colors are the dominant palette, filling backgrounds and accents. "
                f"BACKGROUND: Bold flat graphic poster for {brand_ref}. "
                f"Strong geometric shapes and color blocks in the brand palette fill the frame. "
                f"FOCAL_ELEMENT: A single powerful visual — a Nigerian professional in action, "
                f"or a stylised icon representing {industry} — placed in the upper two-thirds. "
                f"{product_note}"
                f"LAYOUT: {aspect} format, bold asymmetric layout, strong visual hierarchy, "
                f"clear negative space in the lower third. "
                f"{voice_note}"
                f"No watermarks, no logos, no text overlays. Professional quality, publishable brand asset."
            )

        if image_type == 'stat_card':
            brand_ref = f"{brand_name}" if brand_name else industry
            return (
                f"COLOR_PALETTE: {color_list if color_list else 'bold single brand color with white accents'} — "
                f"dominant background and accent colors. "
                f"BACKGROUND: Clean minimal flat design card for {brand_ref}. "
                f"{color_list if color_list else 'Deep brand color'} solid or subtle gradient background. "
                f"FOCAL_ELEMENT: Abstract geometric shapes or minimal icons representing {industry}. "
                f"ACCENT_ELEMENTS: Thin geometric lines or minimal icons in a lighter shade of "
                f"brand color, subtle texture or grid in background for depth. "
                f"QUALITY: Flat design only, pixel-perfect, publishable brand asset. "
                f"No watermarks, no logos, no text overlays, not photographic."
            )

        if image_type == 'brand_illustration':
            return (
                f"STYLE: Modern flat illustration with semi-realistic shading. Nigerian cultural context. "
                f"{color_note}{logo_note}"
                f"SCENE: {scene.split('.')[0]} — illustrated in a clean flat design style. "
                f"Lagos or Abuja environment, recognisable architectural details simplified into illustration. "
                f"COLOR_PALETTE: {color_list if color_list else 'warm brand colors with neutral backgrounds'}. "
                f"All colors drawn from the brand palette. "
                f"CHARACTERS: Confident Nigerian {industry} professional with dark skin tones, "
                f"natural hair, {industry}-appropriate attire. {audience_note}{region_note}"
                f"Warm authentic expression, caught mid-action. {tagline_note}{goal_note}{voice_note}"
                f"CONSTRAINTS: no readable text, no watermarks, no logos, "
                f"illustrated style only, not photographic."
            )

        # Default: PHOTO
        camera_light = {
            "linkedin": "Sony A7R V, 85mm f/1.4 at f/2.0, soft north-facing window light",
            "instagram": "Sony A7R V, 35mm f/1.8 at f/2.2, warm afternoon light through open terrace",
            "facebook": "Nikon Z9, 50mm f/1.4 at f/2.0, warm late-afternoon outdoor light",
            "twitter": "Canon EOS R5, 35mm f/2 at f/2.8, high-contrast overcast outdoor daylight",
        }.get(platform, "Sony A7R V, 50mm f/1.8, natural window light")

        composition = {
            "landscape": "wide shot, subject at left third, foreground element creating depth",
            "square": "centred composition, subject fills 60% of frame",
            "portrait": "vertical frame, subject in lower two-thirds, environment above",
            "story": "full-frame vertical, subject centred",
            "banner": "panoramic wide shot, sweeping left-to-right flow",
        }.get(aspect, "rule of thirds, foreground depth")

        colour = {
            "linkedin": "clean natural colour, lifted shadows, neutral white balance",
            "instagram": "warm natural tones, slightly muted highlights, authentic warmth",
            "facebook": "warm saturated natural colour, honest documentary look",
            "twitter": "high-contrast natural colour, photojournalism accuracy",
        }.get(platform, "warm natural tones, documentary finish")

        brand_color_note = (
            f"Incorporate brand colors ({color_list}) in clothing or environmental accents. "
            if color_list else ""
        )

        location = f"{region_fb} business district" if region_fb else "Lagos business district, Victoria Island or Lekki Phase 1"
        return (
            f"{color_note}"
            f"SCENE: {scene}, {location}. "
            f"SUBJECT: a confident Nigerian {industry} professional with warm dark-brown skin, "
            f"natural hair, actively engaged mid-action — candid documentary style, never posing. "
            f"Skin: natural texture, visible pores, subtle forehead sheen, no heavy retouching. "
            f"{audience_note}{product_note}{region_note}"
            f"CAMERA: {camera_light}. Composition: {composition}. "
            f"Subject tack sharp, background softly blurred (shallow depth of field). "
            f"COLOUR: {colour}. Lifted shadows, no heavy LUT or Instagram filter. "
            f"{brand_color_note}{logo_note}{tagline_note}{goal_note}{voice_note}"
            f"QUALITY: editorial-grade, publishable in a premium African business magazine. "
            f"CONSTRAINTS: no text overlays, no watermarks, no logos, "
            f"not stock-photo stiffness, not illustrated, not CGI render, not cinematic."
        )
    
    @staticmethod
    def _extract_visual_concepts(content: str, seed_content: str) -> str:
        """
        Map content keywords to concrete photographic scene descriptions.
        Returns up to two matched scenes joined naturally for richer prompts.
        """
        keyword_to_scene = {
            # Finance & banking
            'loan':        'entrepreneur leans forward signing a business loan agreement at a glass desk, '
                           'documents and a MacBook Pro spread open, natural window light catching the pen',
            'fintech':     'Nigerian professional in a crisp white shirt taps a payment confirmation '
                           'on a smartphone, Lagos Victoria Island skyline softly blurred behind',
            'banking':     'bank relationship manager and client review documents together at a sleek '
                           'marble desk, warm late-afternoon office light',
            'invest':      'investor and founder shake hands across a glass boardroom table, '
                           'city skyline and golden hour light streaming through floor-to-ceiling windows',
            'funding':     'startup founder presents growth charts on a large screen to seated investors '
                           'in a glass-walled Lekki conference room',
            'payment':     'close-up of hands exchanging a business card at a Lagos networking event, '
                           'shallow depth of field, warm ambient lighting',
            # Business operations
            'sme':         'small business owner stands proudly in front of a well-organised boutique '
                           'shopfront in Lagos Island, bright midday light on colourful merchandise',
            'business':    'two Nigerian professionals in tailored agbada and blazer discuss strategy '
                           'over espresso at a minimalist Ikoyi cafe, midday soft diffused light',
            'scale':       'diverse team celebrates around a monitor showing upward growth metrics, '
                           'modern open-plan Victoria Island office, afternoon window light',
            'growth':      'confident female executive reviews a laptop showing upward trend data '
                           'in a sunlit corner office, thoughtful expression, shallow depth of field',
            'product':     'marketing team reviews product mockups pinned to a large frosted glass wall, '
                           'creative studio space, cool overhead track lighting',
            'launch':      'team watches a live product launch countdown on multiple screens '
                           'in a darkened Lagos tech hub, faces lit by monitor glow, excited expressions',
            'team':        'diverse Nigerian team collaborates at a long oak conference table, '
                           'laptops open, animated discussion, warm afternoon side-light',
            'customer':    'smiling customer service agent in branded uniform assists a client '
                           'at a bright modern reception desk, open atrium background',
            # Tech & digital
            'digital':     'developer in a hoodie codes on dual ultrawide monitors in a sleek tech office, '
                           'ambient cyan LED bias lighting, shallow depth of field on the screen',
            'tech':        'young Nigerian engineer presents a prototype circuit board to colleagues '
                           'in a Yaba tech hub, overhead industrial lighting, candid moment',
            'app':         'product manager and designer review a mobile app wireframe on an iPad '
                           'at a standing desk, bright co-working space, cool diffused light',
            'social media':'content creator photographs a flat-lay arrangement on a marble surface, '
                           'ring light reflected in sunglasses, Lekki apartment with open terrace',
            # Marketing & brand
            'marketing':   'creative director reviews campaign mood board pinned to a white wall, '
                           'marker in hand, natural window light casting soft shadows',
            'brand':       'brand strategist and client discuss logo options spread on a clean desk, '
                           'branding studio environment, warm incandescent accent lights',
            'content':     'videographer films a talking-head interview in a well-lit Lagos studio, '
                           'soft box lighting, bokeh background of bookshelves',
            # Entrepreneurship & leadership
            'entrepreneur':'determined Nigerian entrepreneur stands at a floor-to-ceiling window '
                           'overlooking the Lagos skyline, arms crossed, confident gaze, golden hour light',
            'leader':      'executive addresses a small team in a modern glass office, '
                           'whiteboard with strategy diagrams, natural afternoon light from the side',
            'startup':     'startup founders brainstorm with sticky notes on a glass wall, '
                           'Yaba co-working space, overhead warm pendant lights',
            'success':     'Nigerian businesswoman receives applause after a conference presentation, '
                           'large screen with her slides visible, warm stage spotlights',
            # Location-specific
            'lagos':       'aerial-perspective street view of Victoria Island at golden hour, '
                           'modern glass towers reflecting warm amber light, light traffic below',
            'abuja':       'professionals walk across the gleaming plaza of a modern Maitama office complex, '
                           'harsh midday Nigerian sun casting sharp shadows',
        }

        text_lower = (content + " " + seed_content).lower()
        matched = []
        for keyword, scene in keyword_to_scene.items():
            if keyword in text_lower:
                matched.append(scene)
            if len(matched) >= 2:
                break

        if len(matched) == 2:
            return f"{matched[0]}. In the background, {matched[1].split(',')[0].lower()}"
        if matched:
            return matched[0]
        return (
            'Nigerian business professional in a fitted navy blazer reviews documents '
            'at a standing desk in a sunlit modern Lagos office, Victoria Island towers '
            'visible through floor-to-ceiling windows, warm afternoon side-light'
        )
    
    @staticmethod
    def _overlay_logo(b64: str, logo_url: str, position: str = "bottom_right") -> str:
        """
        Download the brand logo and composite it onto the generated image using Pillow.
        Logo is resized to ~14% of image width and placed at the specified corner.
        Falls back to the original image if anything fails.
        """
        import base64 as _b64
        import io
        import requests as _req
        from PIL import Image

        try:
            # Decode generated image
            img_bytes = _b64.b64decode(b64)
            base_img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
            bw, bh = base_img.size

            # Download logo
            resp = _req.get(logo_url, timeout=10)
            resp.raise_for_status()
            logo_img = Image.open(io.BytesIO(resp.content)).convert("RGBA")

            # Resize logo to 8% of image width, preserve aspect ratio
            target_w = max(40, int(bw * 0.08))
            lw, lh = logo_img.size
            scale = target_w / lw
            logo_img = logo_img.resize((target_w, int(lh * scale)), Image.LANCZOS)
            lw, lh = logo_img.size

            # Badge padding (inner: 5px each side, outer edge: 1.5% of width)
            badge_pad_inner = max(5, int(bw * 0.005))
            edge_pad = max(12, int(bw * 0.015))

            badge_w = lw + badge_pad_inner * 2
            badge_h = lh + badge_pad_inner * 2

            if position == "bottom_left":
                bx = edge_pad
                by = bh - badge_h - edge_pad
            elif position == "top_left":
                bx = edge_pad
                by = edge_pad
            elif position == "top_right":
                bx = bw - badge_w - edge_pad
                by = edge_pad
            elif position == "top_center":
                bx = (bw - badge_w) // 2
                by = edge_pad
            elif position == "bottom_center":
                bx = (bw - badge_w) // 2
                by = bh - badge_h - edge_pad
            elif position == "center":
                bx = (bw - badge_w) // 2
                by = (bh - badge_h) // 2
            else:  # bottom_right (default)
                bx = bw - badge_w - edge_pad
                by = bh - badge_h - edge_pad

            # Draw semi-transparent white rounded-rectangle badge behind logo
            badge = Image.new("RGBA", (badge_w, badge_h), (0, 0, 0, 0))
            try:
                from PIL import ImageDraw
                draw = ImageDraw.Draw(badge)
                radius = max(6, badge_h // 5)
                draw.rounded_rectangle(
                    [(0, 0), (badge_w - 1, badge_h - 1)],
                    radius=radius,
                    fill=(255, 255, 255, 210)  # white at 82% opacity
                )
            except Exception:
                # Fallback: plain white rectangle if rounded_rectangle unavailable
                badge = Image.new("RGBA", (badge_w, badge_h), (255, 255, 255, 210))

            base_img.paste(badge, (bx, by), badge)

            # Paste logo on top of badge
            logo_x = bx + badge_pad_inner
            logo_y = by + badge_pad_inner
            base_img.paste(logo_img, (logo_x, logo_y), logo_img)

            buf = io.BytesIO()
            base_img.convert("RGB").save(buf, format="WEBP", quality=97, method=6)
            result_b64 = _b64.b64encode(buf.getvalue()).decode()
            print(f"✅ Logo composited at {position} with badge ({lw}×{lh}px on {bw}×{bh}px image)")
            return result_b64

        except Exception as e:
            print(f"⚠️ Logo overlay failed: {e}, returning original image")
            return b64

    @staticmethod
    def _map_to_gemini_aspect(size: str) -> str:
        """Map platform dimensions to Nano Banana 2 (Imagen) supported aspect ratios."""
        try:
            width, height = map(int, size.split("x"))
            ratio = width / height
            if ratio >= 1.6:
                return "16:9"
            elif ratio >= 1.3:
                return "4:3"
            elif ratio <= 0.65:
                return "9:16"
            elif ratio <= 0.85:
                return "3:4"
            else:
                return "1:1"
        except (ValueError, AttributeError):
            return "1:1"

    @staticmethod
    def _crop_to_ratio(b64: str, target_w: int, target_h: int) -> str:
        """
        Center-crop a base64-encoded WebP image to the exact target aspect ratio.
        Returns the cropped image as a base64 string (WebP).
        Skips cropping if the ratio already matches within 2%.
        """
        import base64 as _b64
        import io
        from PIL import Image

        target_ratio = target_w / target_h

        img_bytes = _b64.b64decode(b64)
        img = Image.open(io.BytesIO(img_bytes))
        gen_w, gen_h = img.size
        gen_ratio = gen_w / gen_h

        # Already close enough — skip
        if abs(gen_ratio - target_ratio) / target_ratio < 0.02:
            return b64

        if target_ratio > gen_ratio:
            # Target is wider → crop top and bottom
            new_h = int(gen_w / target_ratio)
            top = (gen_h - new_h) // 2
            box = (0, top, gen_w, top + new_h)
        else:
            # Target is taller → crop left and right
            new_w = int(gen_h * target_ratio)
            left = (gen_w - new_w) // 2
            box = (left, 0, left + new_w, gen_h)

        cropped = img.crop(box)
        buf = io.BytesIO()
        cropped.save(buf, format="WEBP", quality=97, method=6)
        return _b64.b64encode(buf.getvalue()).decode()

    @staticmethod
    async def _call_dalle_api(
        prompt: str,
        size: str = "1024x1024",
        reference_image: Optional[str] = None,
        image_model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generate an image using Nano Banana 2 (Google Imagen via Gemini API).
        Falls back to gpt-image-1 if the Gemini key is not configured.

        When reference_image (base64 data URL or public URL) is provided, uses
        gpt-image-1's edit endpoint so the reference appears exactly as-is and
        only brand/design overlays are added — bypassing Imagen which would
        reimagine the scene from scratch.
        """
        import base64 as _b64
        import io

        try:
            from app.core.config import settings as _cfg

            # Parse requested dimensions for post-generation crop
            try:
                target_w, target_h = map(int, size.split("x"))
            except (ValueError, AttributeError):
                target_w, target_h = 1024, 1024

            # ── Image-edit path (reference image provided) ─────────────────────
            # Skip Imagen (text-to-image only) and use gpt-image-1 edit endpoint,
            # which takes the reference as a base and applies the prompt as overlays.
            # GPT-Image-2 handles its own reference image path below.
            if reference_image and (image_model or "") not in ("openai/gpt-image-2", "fal-ai/openai/gpt-image-2"):
                try:
                    from app.services.AIService import client as _ai_client
                    from PIL import Image as _PILImage

                    # Decode reference image to raw PNG bytes (gpt-image-1 edit requires PNG)
                    if reference_image.startswith("data:"):
                        import re as _re_ref
                        _m = _re_ref.match(r"data:[^;]+;base64,(.+)", reference_image, _re_ref.DOTALL)
                        raw_bytes = _b64.b64decode(_m.group(1)) if _m else None
                    else:
                        import httpx as _httpx
                        async with _httpx.AsyncClient(timeout=20) as _c:
                            r = await _c.get(reference_image)
                            raw_bytes = r.content if r.status_code == 200 else None

                    if not raw_bytes:
                        raise ValueError("Could not load reference image bytes")

                    # Convert to RGBA PNG (required by the edit endpoint)
                    img = _PILImage.open(io.BytesIO(raw_bytes)).convert("RGBA")

                    # Resize to match requested output dimensions
                    if target_w > target_h:
                        edit_size = "1536x1024"
                    elif target_h > target_w:
                        edit_size = "1024x1536"
                    else:
                        edit_size = "1024x1024"
                    tw, th = map(int, edit_size.split("x"))
                    img = img.resize((tw, th), _PILImage.LANCZOS)

                    png_buf = io.BytesIO()
                    img.save(png_buf, format="PNG")
                    png_buf.seek(0)

                    loop = asyncio.get_running_loop()
                    response = await loop.run_in_executor(
                        None,
                        lambda: _ai_client.images.edit(
                            model="gpt-image-1",
                            image=("reference.png", png_buf, "image/png"),
                            prompt=prompt,
                            n=1,
                            size=edit_size,
                        )
                    )

                    b64 = response.data[0].b64_json
                    b64 = ImageContentService._crop_to_ratio(b64, target_w, target_h)

                    # Convert to WebP
                    out_img = _PILImage.open(io.BytesIO(_b64.b64decode(b64))).convert("RGB")
                    webp_buf = io.BytesIO()
                    out_img.save(webp_buf, format="WEBP", quality=97, method=6)
                    b64_webp = _b64.b64encode(webp_buf.getvalue()).decode()

                    print(f"🎨 gpt-image-1 edit generated (reference image preserved, {edit_size})")
                    return {
                        "success": True,
                        "url": f"data:image/webp;base64,{b64_webp}",
                        "model": "gpt-image-1-edit",
                    }
                except Exception as _edit_err:
                    print(f"⚠️ gpt-image-1 edit failed: {_edit_err} — falling back to standard generation")
                    # Fall through to standard generation below

            # ── GPT-Image-2 direct OpenAI path ────────────────────────────────
            if (image_model or "") in ("openai/gpt-image-2", "fal-ai/openai/gpt-image-2"):
                try:
                    from app.services.AIService import client as _oai_client
                    import base64 as _b64
                    from PIL import Image as _PILImage
                    import io as _io

                    if target_w > target_h:
                        _gpt2_size = "1536x1024"
                    elif target_h > target_w:
                        _gpt2_size = "1024x1536"
                    else:
                        _gpt2_size = "1024x1024"

                    if reference_image:
                        # Decode reference image to PNG bytes for the edit endpoint
                        if reference_image.startswith("data:"):
                            import re as _re_ref2
                            _m2 = _re_ref2.match(r"data:[^;]+;base64,(.+)", reference_image, _re_ref2.DOTALL)
                            _ref_bytes = _b64.b64decode(_m2.group(1)) if _m2 else None
                        else:
                            import httpx as _httpx2
                            async with _httpx2.AsyncClient(timeout=20) as _c2:
                                _r2 = await _c2.get(reference_image)
                                _ref_bytes = _r2.content if _r2.status_code == 200 else None

                        if not _ref_bytes:
                            raise ValueError("Could not load reference image bytes for GPT-Image-2 edit")

                        _ref_img = _PILImage.open(_io.BytesIO(_ref_bytes)).convert("RGBA")
                        _tw2, _th2 = map(int, _gpt2_size.split("x"))
                        _ref_img = _ref_img.resize((_tw2, _th2), _PILImage.LANCZOS)
                        _ref_png_buf = _io.BytesIO()
                        _ref_img.save(_ref_png_buf, format="PNG")
                        _ref_png_buf.seek(0)

                        print(f"🎨 GPT-Image-2 edit with reference ({_gpt2_size})…")
                        loop = asyncio.get_running_loop()
                        _gpt2_resp = await loop.run_in_executor(
                            None,
                            lambda: _oai_client.images.edit(
                                model="gpt-image-2",
                                image=("reference.png", _ref_png_buf, "image/png"),
                                prompt=prompt,
                                n=1,
                                size=_gpt2_size,
                            ),
                        )
                        _mode = "gpt-image-2-edit"
                    else:
                        print(f"🎨 GPT-Image-2 direct OpenAI ({_gpt2_size})…")
                        loop = asyncio.get_running_loop()
                        _gpt2_resp = await loop.run_in_executor(
                            None,
                            lambda: _oai_client.images.generate(
                                model="gpt-image-2",
                                prompt=prompt,
                                n=1,
                                size=_gpt2_size,
                                quality="high",
                                output_format="webp",
                            ),
                        )
                        _mode = "gpt-image-2"

                    _gpt2_b64 = _gpt2_resp.data[0].b64_json

                    _gpt2_img = _PILImage.open(_io.BytesIO(_b64.b64decode(_gpt2_b64))).convert("RGB")
                    _gpt2_buf = _io.BytesIO()
                    _gpt2_img.save(_gpt2_buf, format="WEBP", quality=97, method=6)
                    _gpt2_b64 = _b64.b64encode(_gpt2_buf.getvalue()).decode()

                    print(f"✅ GPT-Image-2 ready ({_gpt2_size}, {_mode})")
                    return {
                        "success": True,
                        "url": f"data:image/webp;base64,{_gpt2_b64}",
                        "model": _mode,
                    }
                except Exception as _gpt2_err:
                    print(f"⚠️ GPT-Image-2 failed: {_gpt2_err} — falling back to Imagen/GPT")

            # ── fal.ai path (model explicitly chosen from frontend) ────────────
            _fal_model = image_model or ""
            if _fal_model.startswith("fal-ai/") and _cfg.FAL_API_KEY:
                try:
                    import os as _os
                    import httpx as _httpx
                    import fal_client as _fal

                    _os.environ.setdefault("FAL_KEY", _cfg.FAL_API_KEY)

                    # Map pixel dimensions to fal.ai image_size strings
                    def _fal_size(w: int, h: int) -> str:
                        r = w / h
                        if r >= 1.6:   return "landscape_16_9"
                        if r >= 1.2:   return "landscape_4_3"
                        if r <= 0.65:  return "portrait_16_9"
                        if r <= 0.85:  return "portrait_4_3"
                        return "square_hd"

                    _fal_image_size = _fal_size(target_w, target_h)

                    print(f"🎨 fal.ai [{_fal_model}] generating ({_fal_image_size})…")

                    _fal_args = {
                        "prompt": prompt,
                        "image_size": _fal_image_size,
                        "num_images": 1,
                        "output_format": "jpeg",
                        "num_inference_steps": 28,
                        "guidance_scale": 3.5,
                    }

                    loop = asyncio.get_running_loop()
                    _fal_result = await loop.run_in_executor(
                        None,
                        lambda: _fal.run(_fal_model, arguments=_fal_args),
                    )

                    _fal_images = _fal_result.get("images") or []
                    if not _fal_images:
                        raise ValueError(f"fal.ai returned no images: {_fal_result}")

                    _fal_url = _fal_images[0].get("url") or ""
                    if not _fal_url:
                        raise ValueError("fal.ai image url is empty")

                    # Download and convert to WebP base64
                    async with _httpx.AsyncClient(timeout=60) as _hc:
                        _dl = await _hc.get(_fal_url)
                        _dl.raise_for_status()
                    import io as _io
                    from PIL import Image as _PILImage
                    _fal_img = _PILImage.open(_io.BytesIO(_dl.content)).convert("RGB")
                    _fal_buf = _io.BytesIO()
                    _fal_img.save(_fal_buf, format="WEBP", quality=97, method=6)
                    import base64 as _b64
                    _fal_b64 = _b64.b64encode(_fal_buf.getvalue()).decode()
                    _fal_b64 = ImageContentService._crop_to_ratio(_fal_b64, target_w, target_h)

                    print(f"✅ fal.ai [{_fal_model}] image ready")
                    return {
                        "success": True,
                        "url": f"data:image/webp;base64,{_fal_b64}",
                        "model": _fal_model,
                    }
                except Exception as _fal_err:
                    print(f"⚠️ fal.ai [{_fal_model}] failed: {_fal_err} — falling back to Imagen/GPT")

            if _cfg.GOOGLE_GEMINI_API_KEY:
                # ── Nano Banana 2 via Google GenAI SDK ────────────────────────
                try:
                    from google import genai as _genai
                    from google.genai import types as _gtypes
                    import base64 as _b64

                    aspect_ratio = ImageContentService._map_to_gemini_aspect(size)

                    client_g = _genai.Client(api_key=_cfg.GOOGLE_GEMINI_API_KEY)

                    loop = asyncio.get_running_loop()
                    response = await loop.run_in_executor(
                        None,
                        lambda: client_g.models.generate_images(
                            model="imagen-4.0-ultra-generate-001",
                            prompt=prompt,
                            config=_gtypes.GenerateImagesConfig(
                                number_of_images=1,
                                aspect_ratio=aspect_ratio,
                                safety_filter_level="block_low_and_above",
                                person_generation="allow_adult",
                            ),
                        )
                    )

                    if not response.generated_images:
                        raise ValueError("Nano Banana 2 returned no images (blocked/filtered)")

                    generated = response.generated_images[0]
                    b64 = _b64.b64encode(generated.image.image_bytes).decode()

                    # Crop to exact target ratio
                    b64 = ImageContentService._crop_to_ratio(b64, target_w, target_h)

                    # Nano Banana 2 returns PNG — convert to WebP for consistency
                    import io
                    from PIL import Image as _PILImage
                    img = _PILImage.open(io.BytesIO(_b64.b64decode(b64)))
                    buf = io.BytesIO()
                    img.convert("RGB").save(buf, format="WEBP", quality=97, method=6)
                    b64 = _b64.b64encode(buf.getvalue()).decode()

                    print(f"🎨 Nano Banana 2 image generated ({aspect_ratio})")
                    data_url = f"data:image/webp;base64,{b64}"
                    return {
                        "success": True,
                        "url": data_url,
                        "model": "nano-banana-2"
                    }
                except Exception as _nb_err:
                    print(f"⚠️ Nano Banana 2 failed: {_nb_err} — falling back to gpt-image-1")

            # ── Fallback: gpt-image-1 ──────────────────────────────────────
            from app.services.AIService import client

            if target_w > target_h:
                image_size = "1536x1024"
            elif target_h > target_w:
                image_size = "1024x1536"
            else:
                image_size = "1024x1024"

            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.images.generate(
                    model="gpt-image-1.5",
                    prompt=prompt,
                    n=1,
                    size=image_size,
                    quality="high",
                    output_format="webp",
                )
            )
            import base64 as _b64
            b64 = response.data[0].b64_json
            b64 = ImageContentService._crop_to_ratio(b64, target_w, target_h)
            data_url = f"data:image/webp;base64,{b64}"
            print(f"🎨 gpt-image-1 image generated ({image_size})")
            return {
                "success": True,
                "url": data_url,
                "model": "gpt-image-1.5"
            }

        except Exception as e:
            print(f"❌ Image generation failed: {e}")
            return {"success": False, "error": str(e)}
    
    @staticmethod
    async def generate_brand_consistent_images(
        user_id: str,
        content_requests: List[Dict[str, Any]],
        brand_guidelines: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Generate multiple images that maintain brand consistency
        
        Args:
            user_id: User ID
            content_requests: List of content/platform combinations
            brand_guidelines: Brand colors, style, industry, etc.
        """
        try:
            generated_images = []
            errors = []
            
            for request in content_requests:
                try:
                    result = await ImageContentService._generate_platform_image(
                        platform=request['platform'],
                        content=request['content'],
                        seed_content=request.get('seed_content', ''),
                        brand_context=brand_guidelines
                    )
                    
                    if result.get('status'):
                        generated_images.append({
                            "platform": request['platform'],
                            "image_data": result['responseData']
                        })
                    else:
                        errors.append({
                            "platform": request['platform'],
                            "error": result.get('responseMessage')
                        })
                        
                except Exception as e:
                    errors.append({
                        "platform": request.get('platform', 'unknown'),
                        "error": str(e)
                    })
            
            return UriResponse.get_single_data_response("brand_consistent_images", {
                "user_id": user_id,
                "generated_images": generated_images,
                "errors": errors,
                "total_generated": len(generated_images),
                "brand_guidelines_applied": brand_guidelines,
                "generated_at": datetime.utcnow().isoformat()
            })
            
        except Exception as e:
            return UriResponse.error_response(f"Brand consistent image generation failed: {str(e)}")


# Usage Examples:
"""
# Generate content with images
result = await ImageContentService.generate_content_with_images(
    user_id="user_123",
    seed_content="Our new loan product is helping Lagos businesses",
    platforms=["linkedin", "instagram"],
    include_images=True,
    brand_context={
        "colors": ["#1f4e79", "#ffffff", "#f8b500"],
        "style": "modern, professional",
        "industry": "fintech",
        "logo_url": "https://example.com/logo.png"
    }
)

# Generate brand-consistent images for multiple posts
brand_images = await ImageContentService.generate_brand_consistent_images(
    user_id="user_123",
    content_requests=[
        {"platform": "linkedin", "content": "Business growth content..."},
        {"platform": "instagram", "content": "Behind the scenes content..."}
    ],
    brand_guidelines={
        "colors": ["#1f4e79", "#f8b500"],
        "style": "professional, Nigerian business",
        "industry": "financial services"
    }
)
"""
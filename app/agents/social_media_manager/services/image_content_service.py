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
    async def generate_content_with_images(
        user_id: str,
        seed_content: str,
        platforms: List[str],
        include_images: bool = True,
        brand_context: Optional[Dict[str, Any]] = None,
        db=None
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
                        brand_context=brand_context
                    )
                    
                    if image_result.get('status'):
                        raw_image_url = image_result['responseData']['image_url']
                        draft['image_specs'] = image_result['responseData']['specs']
                        draft['has_image'] = True

                        # Upload base64 image to imgBB so we always store a
                        # public URL (avoids the internal-URL proxy problem).
                        stored_url = raw_image_url
                        if raw_image_url and raw_image_url.startswith("data:"):
                            try:
                                import base64 as _b64, re as _re, httpx as _httpx
                                from app.core.config import settings as _cfg
                                _match = _re.match(r"data:[^;]+;base64,(.+)", raw_image_url, _re.DOTALL)
                                if _match and _cfg.IMGBB_API_KEY:
                                    async with _httpx.AsyncClient(timeout=30) as _c:
                                        _r = await _c.post(
                                            "https://api.imgbb.com/1/upload",
                                            data={"key": _cfg.IMGBB_API_KEY, "image": _match.group(1)},
                                        )
                                        _rj = _r.json()
                                    if _rj.get("success"):
                                        stored_url = _rj["data"]["url"]
                                        print(f"☁️  Image uploaded to imgBB: {stored_url}")
                                    else:
                                        print(f"⚠️  imgBB upload failed: {_rj.get('error')}, keeping base64")
                            except Exception as _imgbb_err:
                                print(f"⚠️  imgBB upload error: {_imgbb_err}, keeping base64")

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
    async def _generate_platform_image(
        platform: str,
        content: str,
        seed_content: str,
        brand_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Generate an AI image optimized for a specific platform
        """
        try:
            # Get platform image specifications
            specs = ImageContentService._get_platform_image_specs(platform)

            # Try GPT-4.1 meta-prompting first — picks image type and generates brief
            image_prompt = await ImageContentService._generate_image_brief(
                content=content,
                seed_content=seed_content,
                platform=platform,
                brand_context=brand_context,
                specs=specs
            )

            # Fall back to static prompt if GPT-4.1 fails
            if not image_prompt:
                image_prompt = ImageContentService._create_image_prompt(
                    content=content,
                    seed_content=seed_content,
                    platform=platform,
                    brand_context=brand_context,
                    specs=specs
                )

            image_response = await ImageContentService._call_dalle_api(
                prompt=image_prompt,
                size=f"{specs['width']}x{specs['height']}"
            )

            if image_response.get('success'):
                # Composite brand logo onto generated image if available
                logo_url = (brand_context or {}).get('logo_url')
                if logo_url:
                    import re as _re_logo
                    data_url = image_response['url']
                    _m = _re_logo.match(r"data:[^;]+;base64,(.+)", data_url, _re_logo.DOTALL)
                    if _m:
                        loop = asyncio.get_running_loop()
                        b64_final = await loop.run_in_executor(
                            None,
                            lambda: ImageContentService._overlay_logo(_m.group(1), logo_url)
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
    async def _generate_image_brief(
        content: str,
        seed_content: str,
        platform: str,
        brand_context: Optional[Dict[str, Any]] = None,
        specs: Optional[Dict[str, Any]] = None
    ) -> Optional[str]:
        """
        Use GPT-4.1 to select the most appropriate image type for the content,
        then write a detailed prompt for gpt-image-1.5.

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
            primary_goal         = bc.get('primary_goal', '')
            region               = bc.get('region', '')
            brand_colors_str     = ', '.join(str(c) for c in (bc.get('brand_colors') or []))
            key_products_str     = ', '.join(str(p) for p in (bc.get('key_products_services') or [])[:5])
            cta_styles           = ', '.join(bc.get('cta_styles') or [])
            key_dates            = bc.get('key_dates', '')
            preferred_formats    = ', '.join(bc.get('preferred_formats') or [])

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
            if brand_name:
                brand_lines.append(f"Brand: {brand_name}")
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
            brand_block = (
                "\n\nBRAND CONTEXT:\n" + "\n".join(brand_lines)
                if brand_lines else ""
            )

            system_prompt = (
                "You are a creative director writing image prompts for gpt-image-1.5.\n\n"

                "STEP 1 — Choose the best image type for the content and platform from this list:\n\n"

                "  PHOTO\n"
                "    Authentic photorealistic documentary photograph. Real people, real moments, no posing.\n"
                "    Best for: human stories, testimonials, behind-the-scenes, community updates.\n"
                "    Prompt format: SCENE / SUBJECT / DETAILS / CONSTRAINTS\n\n"

                "  POSTER\n"
                "    Bold graphic design poster with rendered headline text. Brand colors dominant. "
                "Large legible headline extracted from post content rendered as typography in the image. "
                "Brand name rendered smaller. Strong single focal visual behind or around the text.\n"
                "    Best for: product launches, announcements, campaigns, sales.\n"
                "    Prompt format: BACKGROUND / FOCAL_ELEMENT / COLOR_PALETTE / LAYOUT / TEXT_CONTENT\n\n"

                "  STAT_CARD\n"
                "    Clean typographic card with the key number, percentage, or short quote from the post "
                "rendered as oversized bold text in the centre. Brand colors as background. "
                "Brand name or label in smaller text below. Minimal, bold, scannable.\n"
                "    Best for: milestones, achievements, data-driven content.\n"
                "    Prompt format: BACKGROUND / TEXT_CONTENT / ACCENT_ELEMENTS / COLOR_PALETTE\n\n"

                "  PRODUCT_SHOWCASE\n"
                "    Editorial product or service mockup. Subject isolated or in minimal setting, "
                "clean and aspirational.\n"
                "    Best for: product features, service highlights, app screenshots.\n"
                "    Prompt format: SUBJECT / BACKGROUND / LIGHTING / DETAILS / CONSTRAINTS\n\n"

                "  INFOGRAPHIC\n"
                "    Clean visual layout representing a process, comparison, or set of steps. "
                "Icons, arrows, sections — no actual readable text, just the visual structure and style.\n"
                "    Best for: how-it-works, comparisons, step-by-step content.\n"
                "    Prompt format: LAYOUT / VISUAL_ELEMENTS / COLOR_PALETTE / STYLE / CONSTRAINTS\n\n"

                "  BRAND_ILLUSTRATION\n"
                "    Modern flat or semi-realistic illustrated scene in brand colors. "
                "Characters or abstract environments. Nigerian cultural context.\n"
                "    Best for: abstract concepts, values, explainer content, lifestyle.\n"
                "    Prompt format: STYLE / SCENE / COLOR_PALETTE / CHARACTERS / CONSTRAINTS\n\n"

                "STEP 2 — Write the prompt for the chosen type.\n\n"

                "RULES THAT APPLY TO ALL TYPES:\n"
                "• No watermarks. The brand logo will be composited separately — do NOT render it.\n"
                "• Nigerian cultural context — Lagos/Abuja settings, West African aesthetics, "
                "authentic dark skin tones for any people shown\n"
                "• Brand colors must appear visibly in the image\n"
                "• The image must directly relate to the specific content provided\n\n"

                "TEXT RENDERING RULES BY TYPE:\n"
                "• PHOTO, BRAND_ILLUSTRATION: NO text overlays. Keep image purely visual.\n"
                "• POSTER: MUST render the brand name and a punchy 4-7 word headline extracted from "
                "the post content as large bold legible typography directly in the image. "
                "Place headline in the upper or lower third. Brand name smaller below/above it.\n"
                "• STAT_CARD: MUST render the single most important number, percentage, or "
                "short quote from the post as oversized bold text in the centre of the card. "
                "Brand name or label in smaller text below it.\n"
                "• INFOGRAPHIC: Include short section labels and icon labels as readable text. "
                "Keep individual text elements to 1-3 words each.\n"
                "• PRODUCT_SHOWCASE: May include the product name or one short benefit headline.\n\n"

                "ADDITIONAL RULES FOR PHOTO TYPE:\n"
                "• Subjects must be DOING something specific — not posing\n"
                "• Include: natural skin texture, visible pores, slight forehead shine, real hair\n"
                "• Use camera language (85mm f/1.4, soft window light) — not 'stunning', 'HDR', '8K'\n"
                "• Never: cinematic, dramatic, film stock names, CGI, render\n\n"

                "OUTPUT FORMAT:\n"
                "TYPE: [chosen type]\n"
                "[prompt in the format specified for that type]\n\n"
                "For POSTER/STAT_CARD/INFOGRAPHIC include a TEXT: line listing the exact words to render.\n"
                "Output ONLY the TYPE line, optional TEXT line, then the prompt. No other commentary."
            )

            user_prompt = (
                f"PLATFORM: {platform} ({aspect} format)\n"
                f"BUSINESS TYPE: {business_description}\n"
                f"PLATFORM GUIDANCE: {platform_note}"
                f"{brand_block}\n\n"
                f"POST CONTENT TO VISUALIZE:\n{content[:700]}\n\n"
                f"Original business topic: {seed_content[:300]}\n\n"
                "Choose the image type that will make this post most compelling on this platform. "
                "Apply the brand colors prominently. Write the full image prompt.\n\n"
                "IMPORTANT — if you choose POSTER, STAT_CARD, or INFOGRAPHIC: extract a 4-8 word "
                "headline (or the single most impactful number/quote) from the post content above "
                f"and the brand name '{brand_name}' to render as actual text in the image. "
                "Include a TEXT: line in your output listing the exact words to render."
            )

            logo_url = brand_context.get("logo_url") if brand_context else None

            # Build user message — attach logo as a vision image if available so
            # GPT-4.1 can extract exact brand colors and visual style from it.
            if logo_url:
                user_message_content = [
                    {"type": "text", "text": (
                        user_prompt +
                        "\n\nA brand logo image is attached. Analyse its colors, shapes, and visual "
                        "style and let these directly inform the image prompt you write. "
                        "Reflect the logo's visual identity in the color palette and overall aesthetic."
                    )},
                    {"type": "image_url", "image_url": {"url": logo_url}},
                ]
            else:
                user_message_content = user_prompt

            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: ai_client.chat.completions.create(
                    model="gpt-4.1",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_message_content}
                    ],
                    max_tokens=700,
                    temperature=0.7
                )
            )
            brief = response.choices[0].message.content.strip()
            chosen_type = brief.split('\n')[0].replace('TYPE:', '').strip() if brief.startswith('TYPE:') else 'UNKNOWN'
            logo_note = " (logo reference used)" if logo_url else ""
            print(f"🎨 Image brief generated — type: {chosen_type} ({len(brief)} chars){logo_note}")
            return brief

        except Exception as e:
            print(f"⚠️ Image brief generation failed, using static prompt: {e}")
            return None
    
    @staticmethod
    def _get_platform_image_specs(platform: str, image_type: str = "post_image") -> Dict[str, Any]:
        """Get optimal image specifications for platform"""
        platform_specs = ImageContentService.IMAGE_SPECS.get(platform, {})
        
        # Default to post_image, or first available spec
        if image_type in platform_specs:
            return platform_specs[image_type]
        elif platform_specs:
            return list(platform_specs.values())[0]
        else:
            # Default fallback
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
        bc = brand_context or {}
        colors    = bc.get('brand_colors') or []
        color_list = ', '.join(str(c) for c in colors[:3]) if colors else ''
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
            # Extract a short headline from seed content
            words = seed_content.split()
            headline_words = words[:6] if len(words) >= 6 else words
            headline = ' '.join(headline_words).rstrip('.,!?')
            return (
                f"BACKGROUND: Bold flat graphic poster for {brand_ref}. "
                f"{color_note}"
                f"Strong geometric shapes and color blocks in the brand palette fill the frame. "
                f"FOCAL_ELEMENT: A single powerful visual — a Nigerian professional in action, "
                f"or a stylised icon representing {industry} — placed in the upper two-thirds. "
                f"{product_note}"
                f"COLOR_PALETTE: {color_list if color_list else 'deep navy, warm amber, white'}. "
                f"LAYOUT: {aspect} format, bold asymmetric layout, strong visual hierarchy. "
                f"TEXT_CONTENT: Render the headline '{headline}' as large bold white typography "
                f"in the lower third of the image. "
                f"{'Render brand name \"' + brand_name + '\" in smaller text below the headline. ' if brand_name else ''}"
                f"{voice_note}"
                f"No watermarks, no logos."
            )

        if image_type == 'stat_card':
            brand_ref = f"{brand_name}" if brand_name else industry
            # Try to pull a number from content, fallback to generic
            import re as _re_fb
            nums = _re_fb.findall(r'\b\d+[%+x]?\b', seed_content)
            key_stat = nums[0] if nums else "1"
            stat_label = seed_content[:40].rstrip('.,!?') if seed_content else industry
            return (
                f"BACKGROUND: Clean minimal flat design card, "
                f"{color_list if color_list else 'deep brand color'} background. "
                f"TEXT_CONTENT: Render '{key_stat}' as an oversized bold centred number in white "
                f"or high-contrast colour. Below it render the label '{stat_label}' in smaller text. "
                f"{'Below the label render brand name \"' + brand_name + '\" in small caps. ' if brand_name else ''}"
                f"ACCENT_ELEMENTS: Thin geometric lines or small icons in a lighter shade of the "
                f"brand color, subtle grid pattern in background. "
                f"{color_note}"
                f"COLOR_PALETTE: {color_list if color_list else 'bold single brand color with white accents'}. "
                f"Flat design only, not photographic. No watermarks, no logos."
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
            f"SCENE: {scene}, {location}. "
            f"SUBJECT: a confident Nigerian {industry} professional with warm dark-brown skin, "
            f"natural hair, actively engaged in the task — candid documentary, not posing. "
            f"Natural skin texture, visible pores, slight forehead shine. "
            f"{audience_note}{product_note}{region_note}"
            f"DETAILS: {camera_light}, {composition}, shallow depth of field. "
            f"Colour: {colour}. {brand_color_note}{logo_note}{tagline_note}{goal_note}{voice_note}"
            f"CONSTRAINTS: no text overlays, no watermarks, no logos, "
            f"not stock-photo aesthetic, not illustrated, not cinematic."
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

            # Resize logo to 14% of image width, preserve aspect ratio
            target_w = max(60, int(bw * 0.14))
            lw, lh = logo_img.size
            scale = target_w / lw
            logo_img = logo_img.resize((target_w, int(lh * scale)), Image.LANCZOS)
            lw, lh = logo_img.size

            # Edge padding (~2.5% of image width)
            pad = max(20, int(bw * 0.025))

            if position == "bottom_left":
                x, y = pad, bh - lh - pad
            else:  # bottom_right (default)
                x, y = bw - lw - pad, bh - lh - pad

            # Composite logo onto base image
            base_img.paste(logo_img, (x, y), logo_img)

            buf = io.BytesIO()
            base_img.convert("RGB").save(buf, format="WEBP", quality=95)
            result_b64 = _b64.b64encode(buf.getvalue()).decode()
            print(f"✅ Logo composited at {position} ({lw}×{lh}px on {bw}×{bh}px image)")
            return result_b64

        except Exception as e:
            print(f"⚠️ Logo overlay failed: {e}, returning original image")
            return b64

    @staticmethod
    def _map_to_dalle_size(size: str) -> str:
        """Map platform dimensions to gpt-image-1 supported sizes (1024x1024, 1536x1024, 1024x1536)"""
        try:
            width, height = map(int, size.split("x"))
            if width > height:
                return "1536x1024"
            elif height > width:
                return "1024x1536"
            else:
                return "1024x1024"
        except (ValueError, AttributeError):
            return "1024x1024"

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
        cropped.save(buf, format="WEBP", quality=95)
        return _b64.b64encode(buf.getvalue()).decode()

    @staticmethod
    async def _call_dalle_api(prompt: str, size: str = "1024x1024") -> Dict[str, Any]:
        """
        Call OpenAI gpt-image-1.5 API for image generation.
        Generates at the nearest supported DALL-E size, then center-crops to the
        exact target ratio so platform specs (e.g. Instagram 4:5) are met.
        Returns a base64 data URL since gpt-image-1.5 only supports b64_json output.
        """
        try:
            from app.services.AIService import client

            # Parse requested dimensions for post-generation crop
            try:
                target_w, target_h = map(int, size.split("x"))
            except (ValueError, AttributeError):
                target_w, target_h = 1024, 1024

            image_size = ImageContentService._map_to_dalle_size(size)

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
                    moderation="low",
                )
            )

            b64 = response.data[0].b64_json

            # Crop to exact target ratio (< 5 ms, negligible vs. DALL-E latency)
            b64 = ImageContentService._crop_to_ratio(b64, target_w, target_h)

            data_url = f"data:image/webp;base64,{b64}"
            return {
                "success": True,
                "url": data_url
            }

        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
    
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
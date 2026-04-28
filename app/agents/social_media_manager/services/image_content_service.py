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

                        # Save base64 image to local static storage (served directly)
                        stored_url = raw_image_url
                        if raw_image_url and raw_image_url.startswith("data:"):
                            try:
                                import base64 as _b64, re as _re, os as _os, uuid as _uuid
                                _match = _re.match(r"data:[^;]+;base64,(.+)", raw_image_url, _re.DOTALL)
                                if _match:
                                    _filename = f"{_uuid.uuid4().hex}.webp"
                                    _static_dir = "/app/static/images"
                                    _os.makedirs(_static_dir, exist_ok=True)
                                    _img_bytes = _b64.b64decode(_match.group(1))
                                    with open(f"{_static_dir}/{_filename}", "wb") as _f:
                                        _f.write(_img_bytes)
                                    stored_url = f"/static/images/{_filename}"
                                    print(f"💾 Image saved locally: {stored_url}")
                            except Exception as _save_err:
                                print(f"⚠️  Local image save error: {_save_err}, keeping base64")

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

            image_result = await ImageContentService._generate_platform_image(
                platform=platform,
                content=content,
                seed_content=seed_content,
                feedback=feedback,
            )

            if not image_result.get("status"):
                print(f"❌ regenerate_image: generation failed for {draft_id}: {image_result.get('error')}")
                return

            raw_url = image_result["responseData"]["image_url"]
            specs = image_result["responseData"]["specs"]

            # Save base64 to local static storage (served directly by backend)
            stored_url = raw_url
            if raw_url and raw_url.startswith("data:"):
                try:
                    _match = _re.match(r"data:[^;]+;base64,(.+)", raw_url, _re.DOTALL)
                    if _match:
                        _filename = f"{_uuid.uuid4().hex}.webp"
                        _static_dir = "/app/static/images"
                        _os.makedirs(_static_dir, exist_ok=True)
                        _img_bytes = _b64.b64decode(_match.group(1))
                        with open(f"{_static_dir}/{_filename}", "wb") as _f:
                            _f.write(_img_bytes)
                        stored_url = f"/static/images/{_filename}"
                        print(f"💾 Regenerated image saved locally: {stored_url}")
                except Exception as _e:
                    print(f"⚠️ Local image save error during regeneration: {_e}")

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
    async def _generate_platform_image(
        platform: str,
        content: str,
        seed_content: str,
        brand_context: Optional[Dict[str, Any]] = None,
        reference_image: Optional[str] = None,
        feedback: Optional[str] = None,
        image_type: str = "post_image",
        image_model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generate an AI image optimized for a specific platform.
        image_type: "post_image" (default), "story" (9:16), or any key in IMAGE_SPECS[platform].
        """
        try:
            # GPT-Image-2 is the only image model — ignore whatever was passed in.
            image_model = "openai/gpt-image-2"

            # Get platform image specifications
            specs = ImageContentService._get_platform_image_specs(platform, image_type=image_type)

            # Assemble prompt: style directive (if set) + brand colors + user seed content.
            # The style fragment is injected by _generate_image_bg from the brand's
            # selected visual style, rotating through their 3 picks each campaign.
            bc = brand_context or {}
            style_fragment = bc.get("style_prompt_fragment", "")
            font_prompt = bc.get("font_style_prompt", "")
            brand_colors = [c for c in (bc.get("brand_colors") or []) if c]
            base_prompt = seed_content.strip()

            color_block = ""
            if brand_colors:
                color_block = (
                    f"\n\n=== BRAND COLORS ===\n"
                    f"Dominant palette: {', '.join(brand_colors[:3])}. "
                    f"These colors must appear prominently — use them for backgrounds, accents, graphic elements, "
                    f"clothing, or environmental details depending on the image type."
                )

            font_block = f"\n\n=== TYPOGRAPHY ===\n{font_prompt}" if font_prompt else ""

            if style_fragment:
                image_prompt = f"=== VISUAL STYLE ===\n{style_fragment}\n\n=== CONTENT ===\n{base_prompt}{color_block}{font_block}"
            else:
                image_prompt = base_prompt + color_block + font_block

            # When a reference image is provided, always append a hard no-crop directive
            # directly to the prompt so the image model cannot ignore it.
            if reference_image:
                image_prompt = (
                    image_prompt.rstrip()
                    + " Full body shown completely from head to toe. Entire garment/product fully visible in frame — "
                    "no cropping of any part of the clothing, subject, or object. Wide enough framing to show everything."
                )

            image_response = await ImageContentService._call_dalle_api(
                prompt=image_prompt,
                size=f"{specs['width']}x{specs['height']}",
                reference_image=reference_image,
                image_model=image_model,
            )

            if image_response.get('success'):
                # Composite brand logo onto generated image for all models.
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

            system_prompt = (
                "You are a world-class creative director and AI image prompt engineer at a top African brand agency. "
                "Your job is to commission visually stunning, commercially ready images for social media — "
                "the kind that appear in real campaigns by Flutterwave, Paystack, Moniepoint, and MTN. "
                "You brief Nano Banana 2 (Google Imagen 4 Ultra), a state-of-the-art photorealistic image model.\n\n"

                "Nano Banana 2 performs best with flowing, scene-rich natural-language prompts — "
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
                f"{brand_block}{feedback_block}\n\n"
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
                "This paragraph is fed DIRECTLY to Imagen 4 Ultra — it must be vivid, specific, "
                "and commercially ready. Every word counts. Describe things the camera would see, "
                "not abstract concepts. No labels, no sections — pure prose only."
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
            # Extract a short headline from seed content
            words = seed_content.split()
            headline_words = words[:6] if len(words) >= 6 else words
            headline = ' '.join(headline_words).rstrip('.,!?')
            brand_name_line = f'Render brand name "{brand_name}" in smaller text below the headline. ' if brand_name else ''
            return (
                f"COLOR_PALETTE: {color_list if color_list else 'deep navy, warm amber, white'} — "
                f"these colors are the dominant palette, filling backgrounds and accents. "
                f"BACKGROUND: Bold flat graphic poster for {brand_ref}. "
                f"Strong geometric shapes and color blocks in the brand palette fill the frame. "
                f"FOCAL_ELEMENT: A single powerful visual — a Nigerian professional in action, "
                f"or a stylised icon representing {industry} — placed in the upper two-thirds. "
                f"{product_note}"
                f"LAYOUT: {aspect} format, bold asymmetric layout, strong visual hierarchy, "
                f"clear negative space for text. "
                f"TYPOGRAPHY: Render the headline '{headline}' as large bold clean sans-serif white "
                f"typography in the lower third. Maximum legibility, high contrast against background. "
                f"{brand_name_line}"
                f"{voice_note}"
                f"No watermarks, no logos. Professional quality, publishable brand asset."
            )

        if image_type == 'stat_card':
            brand_ref = f"{brand_name}" if brand_name else industry
            # Try to pull a number from content, fallback to generic
            import re as _re_fb
            nums = _re_fb.findall(r'\b\d+[%+x]?\b', seed_content)
            key_stat = nums[0] if nums else "1"
            stat_label = seed_content[:40].rstrip('.,!?') if seed_content else industry
            stat_brand_line = f'Below the label render brand name "{brand_name}" in small caps. ' if brand_name else ''
            return (
                f"COLOR_PALETTE: {color_list if color_list else 'bold single brand color with white accents'} — "
                f"dominant background and accent colors. "
                f"BACKGROUND: Clean minimal flat design card. "
                f"{color_list if color_list else 'Deep brand color'} solid or subtle gradient background. "
                f"TYPOGRAPHY: Render '{key_stat}' as a massive bold centred number/stat in white "
                f"or maximum-contrast color — it must dominate the card visually. "
                f"Below it render '{stat_label}' in clean smaller sans-serif text. "
                f"{stat_brand_line}"
                f"ACCENT_ELEMENTS: Thin geometric lines or minimal icons in a lighter shade of "
                f"brand color, subtle texture or grid in background for depth. "
                f"QUALITY: Flat design only, pixel-perfect, publishable brand asset. "
                f"No watermarks, no logos, not photographic."
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

            # Resize logo to 7% of image width, preserve aspect ratio
            target_w = max(40, int(bw * 0.07))
            lw, lh = logo_img.size
            scale = target_w / lw
            logo_img = logo_img.resize((target_w, int(lh * scale)), Image.LANCZOS)
            lw, lh = logo_img.size

            # Badge padding (inner: 8px each side, outer edge: 2.5% of width)
            badge_pad_inner = max(8, int(bw * 0.008))
            edge_pad = max(20, int(bw * 0.025))

            badge_w = lw + badge_pad_inner * 2
            badge_h = lh + badge_pad_inner * 2

            if position == "bottom_left":
                bx = edge_pad
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
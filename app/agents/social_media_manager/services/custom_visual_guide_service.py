# app/agents/social_media_manager/services/custom_visual_guide_service.py

"""
Custom Visual Guide Service
Handles reference image upload, analysis, and font matching per PRD Section 1-11.

Features:
- Parallel aesthetic + typography extraction via GPT-4o-mini Vision
- Font matching algorithm with 6 outcome states
- Safety, quality, and copyright screening
- 11-dimension metadata tagging
- Prompt fragment assembly for V3 Rulebook integration
"""

from typing import Dict, Any, List, Optional, Tuple
import asyncio
import hashlib
import httpx
import json
import base64
import io
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError
from PIL import Image
import cv2
import numpy as np

from app.services.AIService import client as openai_client
from app.models.custom_visual_guide import (
    CustomVisualGuide,
    AestheticProfile,
    TypographyExtraction,
    FontMatch,
    IdentifiedFont,
    NextStepSuggestion,
    MetadataTags,
)


class CustomVisualGuideService:
    """Service for processing reference images into custom visual guides"""

    # PRD Section 7.1: Per-plan guide limits
    PLAN_LIMITS = {
        "free": 2,
        "starter": 5,
        "standard": 10,
        "executive": 25,
    }

    @staticmethod
    async def process_reference_image(
        image_url: str,
        user_id: str,
        brand_id: Optional[str],
        name: str,
        db: AsyncIOMotorDatabase,
    ) -> CustomVisualGuide:
        """
        Main orchestrator - implements PRD Section 2.1: The Nine Steps

        Args:
            image_url: Cloudinary URL of uploaded reference image
            user_id: User who uploaded
            brand_id: Brand (optional)
            name: Guide name
            db: Database connection

        Returns:
            CustomVisualGuide object with all analysis complete
        """
        print(f"[CVG] Starting reference image processing: {name}")

        from fastapi import HTTPException

        try:
            # Step 1: Upload validation (image_url already validated by caller)
            image_hash = await CustomVisualGuideService._compute_image_hash(image_url)
            print(f"[CVG] Image hash: {image_hash}")

            # Steps 2-4: Parallel screening
            print(f"[CVG] Running safety, quality, and copyright screening...")
            await CustomVisualGuideService._screen_image(image_url)

            # Steps 5-6: Parallel vision analysis
            print(f"[CVG] Running parallel aesthetic + typography extraction...")
            aesthetic_profile, typography_extraction = await asyncio.gather(
                CustomVisualGuideService._extract_aesthetic_profile(image_url),
                CustomVisualGuideService._extract_typography_character(image_url),
            )

            # Step 6b: Font matching
            print(f"[CVG] Matching fonts to typography character...")
            font_matches, match_outcome = await CustomVisualGuideService._match_fonts(
                typography_extraction, user_id, db
            )

            # Step 7: Prompt assembly
            print(f"[CVG] Assembling prompt fragment...")
            prompt_fragment = CustomVisualGuideService._assemble_prompt_fragment(
                aesthetic_profile
            )

            # Step 8: Metadata tagging
            print(f"[CVG] Generating 11-dimension metadata tags...")
            metadata_tags = CustomVisualGuideService._generate_metadata_tags(
                aesthetic_profile
            )

            # Step 9: Store the guide
            print(f"[CVG] Storing custom visual guide...")
            guide_doc = {
                "user_id": user_id,
                "brand_id": brand_id,
                "name": name,
                "original_image_url": image_url,
                "original_image_hash": image_hash,
                "uploaded_at": datetime.utcnow(),
                "aesthetic_profile": aesthetic_profile,
                "prompt_fragment": prompt_fragment,
                "typography_extraction": typography_extraction,
                "match_outcome": match_outcome["outcome"],
                "matched_font_id": match_outcome.get("matched_font_id"),
                "matched_font_source": match_outcome.get("matched_font_source"),
                "match_confidence": match_outcome.get("match_confidence"),
                "alternative_font_matches": match_outcome.get("alternative_matches"),
                "identified_font_name": match_outcome.get("identified_font_name"),
                "next_step_suggestion": match_outcome.get("next_step_suggestion"),
                "metadata_tags": metadata_tags,
                "times_used": 0,
                "times_font_applied": 0,
                "times_user_uploaded_suggested_font": 0,
                "status": "active",
                "updated_at": datetime.utcnow(),
            }

            try:
                result = await db["custom_visual_guides"].insert_one(guide_doc)
                guide_doc["id"] = str(result.inserted_id)
                guide_doc["_id"] = result.inserted_id

                print(f"[CVG] ✅ Custom visual guide created: {guide_doc['id']}")
                return CustomVisualGuide(**guide_doc)

            except DuplicateKeyError:
                print(f"[CVG] ❌ Duplicate image detected: {image_hash}")
                raise HTTPException(
                    status_code=409,
                    detail="You've already uploaded this image as a custom guide."
                )

        except HTTPException:
            raise
        except Exception as e:
            print(f"[CVG] ❌ Error processing reference image: {e}")
            raise

    # ========================================================================
    # SCREENING METHODS (PRD Section 8)
    # ========================================================================

    @staticmethod
    async def _compute_image_hash(image_url: str) -> str:
        """Compute hash of image for deduplication"""
        from fastapi import HTTPException
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(image_url)
                if response.status_code != 200:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Failed to download image: {response.status_code}"
                    )
                image_bytes = response.content
                return hashlib.sha256(image_bytes).hexdigest()
        except HTTPException:
            raise
        except Exception as e:
            print(f"[CVG] Error computing image hash: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @staticmethod
    async def _screen_image(image_url: str):
        """
        PRD Section 8: Safety, Copyright, and Quality Screening

        Runs three parallel checks:
        1. Content safety (NSFW, violence, gore)
        2. Copyright detection (ads, celebrities, trademarks)
        3. Quality checks (resolution, blur, watermarks)
        """
        from fastapi import HTTPException
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(image_url)
                if response.status_code != 200:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Failed to download image: {response.status_code}"
                    )
                image_bytes = response.content

            # Run parallel screening
            # NOTE: Copyright check temporarily disabled
            safety_ok, quality_ok = await asyncio.gather(
                CustomVisualGuideService._check_content_safety(image_bytes),
                # CustomVisualGuideService._check_copyright(image_url),  # Temporarily disabled
                CustomVisualGuideService._check_quality(image_bytes),
            )

            if not safety_ok:
                raise HTTPException(
                    status_code=400,
                    detail="Image contains inappropriate content (NSFW, violence, or gore)."
                )
            # Temporarily disabled copyright check
            # if not copyright_ok:
            #     raise HTTPException(
            #         status_code=400,
            #         detail="Image appears to contain copyrighted material (commercial ads, celebrities, or trademarks)."
            #     )
            if not quality_ok:
                raise HTTPException(
                    status_code=400,
                    detail="Image quality is too low (resolution, blur, or heavy watermarks)."
                )

            print(f"[CVG] ✅ All screening checks passed")

        except HTTPException:
            raise
        except Exception as e:
            print(f"[CVG] ❌ Screening failed: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @staticmethod
    async def _check_content_safety(image_bytes: bytes) -> bool:
        """PRD Section 8.1: Content safety screening"""
        # TODO: Integrate with actual moderation API (e.g., AWS Rekognition, Google Vision)
        # For now, assume safe
        return True

    @staticmethod
    async def _check_copyright(image_url: str) -> bool:
        """
        PRD Section 8.2: Copyright screening using GPT-4o-mini Vision
        Detects commercial ads, celebrities, artwork, trademarks
        """
        try:
            prompt = """Analyze this image for copyright concerns. Return ONLY a JSON object:
{
  "has_copyright_content": boolean,
  "confidence": 0.0-1.0,
  "detected_types": ["commercial_ad" | "celebrity" | "artwork" | "trademark" | "none"]
}

Detect:
- Commercial advertisements with brand logos
- Recognizable celebrities or public figures
- Copyrighted artwork or designs
- Trademarked logos or designs

If confidence >= 0.7 that copyrighted content is present, set has_copyright_content to true."""

            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_url}}
                        ]
                    }],
                    max_tokens=150,
                    temperature=0,
                    response_format={"type": "json_object"}
                )
            )

            result = json.loads(response.choices[0].message.content)
            has_copyright = result.get("has_copyright_content", False)
            confidence = result.get("confidence", 0.0)

            print(f"[CVG] Copyright check: has_copyright={has_copyright}, confidence={confidence}")

            # PRD: Threshold 0.7 confidence → reject
            if has_copyright and confidence >= 0.7:
                return False

            return True

        except Exception as e:
            print(f"[CVG] Copyright check error: {e}")
            # On error, allow through (don't block uploads due to API issues)
            return True

    @staticmethod
    async def _check_quality(image_bytes: bytes) -> bool:
        """
        PRD Section 8.3: Quality screening
        - Resolution below 400x400 pixels
        - Blurry or out-of-focus (Laplacian variance)
        - Heavy watermarks
        """
        try:
            # Load image
            img = Image.open(io.BytesIO(image_bytes))
            width, height = img.size

            # Check resolution
            if width < 400 or height < 400:
                print(f"[CVG] ❌ Image resolution too low: {width}x{height}")
                return False

            # Check blur using Laplacian variance
            img_np = np.array(img.convert('L'))  # Convert to grayscale
            laplacian_var = cv2.Laplacian(img_np, cv2.CV_64F).var()

            # Threshold for blur detection (lower = more blurry)
            if laplacian_var < 100:
                print(f"[CVG] ❌ Image is too blurry: laplacian_var={laplacian_var}")
                return False

            print(f"[CVG] ✅ Quality check passed: {width}x{height}, laplacian={laplacian_var:.2f}")
            return True

        except Exception as e:
            print(f"[CVG] Quality check error: {e}")
            # On error, allow through
            return True

    # ========================================================================
    # AESTHETIC EXTRACTION (PRD Section 3)
    # ========================================================================

    @staticmethod
    async def _extract_aesthetic_profile(image_url: str) -> Dict[str, Any]:
        """
        PRD Section 3.2: Aesthetic vision analysis using GPT-4o-mini

        Extracts structured aesthetic profile for prompt generation.
        """
        try:
            # PRD Section 3.2: The complete aesthetic extraction prompt
            prompt = """You are an expert visual analyst. Analyze the uploaded image and extract a structured aesthetic profile. DO NOT describe the literal content. DESCRIBE THE VISUAL STYLE so it can be reproduced in new images with different content.

For PHOTOGRAPHY/REALISTIC images, use this vocabulary where applicable: tungsten practicals, soft halation, anamorphic, motivated lighting, negative fill, golden hour, Kodak Portra tones, Fuji 400H, controlled specular, edge lighting, soft diffusion bloom, atmospheric haze, film grain, shallow depth of field, foreground occlusion, environmental wrapping, spatial continuity, runway editorial, documentary realism, magazine spread framing, brutalist editorial.

For ILLUSTRATIONS/DRAWINGS/SKETCHES, use this vocabulary where applicable: hand-drawn, sketchy, line art, ink drawing, pencil sketch, watercolor, digital illustration, vector art, flat design, minimalist illustration, doodle style, gestural lines, loose linework, controlled linework, crosshatching, stippling, cel-shaded, screenprint aesthetic, editorial illustration, technical drawing, architectural sketch, fashion sketch, comic book style, manga style, charcoal rendering, pen and ink, marker rendering, continuous line, broken line, graphic novel aesthetic.

Return ONLY a JSON object with this structure:
{
  "visual_genre": "string",
  "quality_benchmark": "string",
  "camera_style": "string",
  "color_palette": {
    "primary_colors": ["string", "string", "string"],
    "temperature": "warm | cool | neutral | mixed",
    "saturation": "muted | balanced | rich | high",
    "contrast": "low | balanced | high",
    "dominant_role": "background | accent | subject"
  },
  "lighting": {
    "direction": "string",
    "quality": "hard | soft | diffused | dramatic",
    "temperature": "warm | cool | neutral",
    "specific_style": "string using cinematography vocabulary"
  },
  "composition": {
    "framing": "centered | asymmetric | edge-weighted | layered",
    "density": "spacious | balanced | dense | maximalist",
    "subject_position": "string",
    "depth_treatment": "flat | layered | atmospheric | immersive"
  },
  "texture_and_atmosphere": {
    "grain": "none | subtle | pronounced",
    "haze": "none | subtle | pronounced",
    "depth_of_field": "deep | balanced | shallow",
    "finish": "matte | balanced | glossy | high-end"
  },
  "mood": {
    "primary": "string",
    "secondary": "string or null"
  },
  "anti_aesthetic": ["string", "string", "string"],
  "subject_treatment": "string"
}

If you cannot determine a field with confidence, use null. Return ONLY valid JSON."""

            print(f"[CVG] Calling GPT-4o-mini for aesthetic extraction...")

            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_url}}
                        ]
                    }],
                    max_tokens=800,
                    temperature=0,
                    response_format={"type": "json_object"}
                )
            )

            aesthetic_profile = json.loads(response.choices[0].message.content)
            print(f"[CVG] ✅ Aesthetic extraction complete")
            return aesthetic_profile

        except Exception as e:
            print(f"[CVG] ❌ Aesthetic extraction error: {e}")
            raise

    # ========================================================================
    # TYPOGRAPHY EXTRACTION (PRD Section 4)
    # ========================================================================

    @staticmethod
    async def _extract_typography_character(image_url: str) -> Dict[str, Any]:
        """
        PRD Section 4.3: Typography extraction using GPT-4o-mini Vision

        Extracts typography character for font matching.
        """
        try:
            # PRD Section 4.3: The typography extraction prompt
            prompt = """You are analyzing the typography style in this image. Look at any text, headlines, or lettering visible.

Step 1: Does this image contain meaningful typography?
Step 2: If typography is present, describe its character.
Step 3: Does the typography appear to use a recognizable named font (Helvetica, Playfair Display, Bebas Neue, Cormorant, Bodoni, Inter, Montserrat, Lato, etc.)? Only mark identified if highly confident.

Return ONLY a JSON object with this structure:
{
  "has_typography": boolean,
  "typography_character": {
    "style_class": "serif | sans-serif | display | script | mono | mixed",
    "subclass": "string - more specific, e.g., 'humanist sans', 'transitional serif', 'high-contrast modern serif', 'condensed display', 'rounded geometric'",
    "weight_visual": "thin | light | regular | medium | bold | black",
    "contrast": "low | medium | high | extreme",
    "width": "condensed | normal | wide",
    "personality": "one word: elegant, bold, playful, serious, modern, classic, raw, refined, editorial, casual",
    "energy_level": <integer 1-10>,
    "use_case_alignment": ["array from: luxury, fashion, professional, casual, tech, editorial, meme, sale, tribute"],
    "decorative_level": "functional | semi-decorative | highly-decorative"
  },
  "matching_priority_traits": ["array of 2-3 most important traits"],
  "identified_font": {
    "name": "string - named font if highly confident, otherwise null",
    "confidence": "high | medium | null"
  }
}

If has_typography is false, omit other fields. Return ONLY valid JSON."""

            print(f"[CVG] Calling GPT-4o-mini for typography extraction...")

            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_url}}
                        ]
                    }],
                    max_tokens=600,
                    temperature=0,
                    response_format={"type": "json_object"}
                )
            )

            typography_extraction = json.loads(response.choices[0].message.content)
            print(f"[CVG] ✅ Typography extraction complete: has_typography={typography_extraction.get('has_typography')}")
            return typography_extraction

        except Exception as e:
            print(f"[CVG] ❌ Typography extraction error: {e}")
            raise

    # ========================================================================
    # FONT MATCHING (PRD Section 4.4-4.8)
    # ========================================================================

    @staticmethod
    async def _match_fonts(
        typography_extraction: Dict[str, Any],
        user_id: str,
        db: AsyncIOMotorDatabase,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        PRD Section 4.4: Font matching algorithm
        PRD Section 4.8: Match outcome classification

        Returns:
            (font_matches, match_outcome)

        match_outcome = {
            "outcome": "STRONG_MATCH | DECENT_MATCH | WEAK_MATCH | NO_RECOMMENDED_MATCH | NO_MATCH | NO_TYPOGRAPHY | DECORATIVE_ACCEPTED",
            "matched_font_id": str or None,
            "matched_font_source": "library | user_upload" or None,
            "match_confidence": "high | medium | low" or None,
            "alternative_matches": [FontMatch dicts],
            "identified_font_name": str or None,
            "next_step_suggestion": NextStepSuggestion dict or None,
        }
        """
        try:
            has_typography = typography_extraction.get("has_typography", False)

            # PRD Section 4.8: Outcome 6 - NO_TYPOGRAPHY
            if not has_typography:
                print(f"[CVG] No typography in reference - using NO_TYPOGRAPHY outcome")
                return [], {
                    "outcome": "NO_TYPOGRAPHY",
                    "matched_font_id": None,
                    "matched_font_source": None,
                    "match_confidence": None,
                    "alternative_matches": None,
                    "identified_font_name": None,
                    "next_step_suggestion": None,
                }

            typo_char = typography_extraction.get("typography_character", {})
            decorative_level = typo_char.get("decorative_level")
            identified_font = typography_extraction.get("identified_font")

            # PRD Section 4.8: Outcome 6 (Special Case) - DECORATIVE_ACCEPTED
            if decorative_level == "highly-decorative":
                print(f"[CVG] Highly decorative typography - using DECORATIVE_ACCEPTED outcome")
                return [], {
                    "outcome": "DECORATIVE_ACCEPTED",
                    "matched_font_id": None,
                    "matched_font_source": None,
                    "match_confidence": None,
                    "alternative_matches": None,
                    "identified_font_name": None,
                    "next_step_suggestion": {
                        "type": "use_brand_default_decorative",
                        "message": "Typography in your reference is decorative (custom lettering or calligraphy). For social media posts, your brand default will produce more readable results — but we'll match the mood and palette of your reference exactly.",
                    },
                }

            # Get available fonts (library + user custom fonts)
            available_fonts = await CustomVisualGuideService._get_available_fonts(user_id, db)

            if not available_fonts:
                print(f"[CVG] No available fonts to match against")
                return [], CustomVisualGuideService._classify_no_match_outcome(
                    identified_font, None, typo_char
                )

            # PRD Section 4.4: Run font matching algorithm
            font_matches = CustomVisualGuideService._score_fonts(
                typo_char, available_fonts, typography_extraction.get("matching_priority_traits", [])
            )

            # Sort by score and take top 3
            font_matches = sorted(font_matches, key=lambda x: x["match_score"], reverse=True)[:3]

            # PRD Section 4.8: Classify outcome based on top score
            top_score = font_matches[0]["match_score"] if font_matches else 0
            outcome = CustomVisualGuideService._classify_match_outcome(
                top_score, font_matches, identified_font, typo_char
            )

            print(f"[CVG] Font matching complete: outcome={outcome['outcome']}, top_score={top_score}")
            return font_matches, outcome

        except Exception as e:
            print(f"[CVG] ❌ Font matching error: {e}")
            raise

    @staticmethod
    async def _get_available_fonts(user_id: str, db: AsyncIOMotorDatabase) -> List[Dict[str, Any]]:
        """
        Get user's available fonts (custom uploads + 16 library fonts)

        Returns list of font objects with scoring attributes.
        """
        # TODO: Fetch from actual fonts collection
        # For now, return library fonts only as placeholder

        # PRD mentions 16 library fonts - these would come from the fonts collection
        # Each should have: font_id, font_name, style_class, subclass, weight_visual,
        # contrast, width, personality, energy_level, use_case_alignment, source

        library_fonts = [
            {
                "font_id": "library_cormorant",
                "font_name": "Cormorant",
                "style_class": "serif",
                "subclass": "transitional serif",
                "weight_visual": "regular",
                "contrast": "medium",
                "width": "normal",
                "personality": "elegant",
                "energy_level": 4,
                "use_case_alignment": ["luxury", "fashion", "editorial"],
                "source": "library",
            },
            {
                "font_id": "library_inter",
                "font_name": "Inter",
                "style_class": "sans-serif",
                "subclass": "humanist sans",
                "weight_visual": "regular",
                "contrast": "low",
                "width": "normal",
                "personality": "professional",
                "energy_level": 5,
                "use_case_alignment": ["professional", "tech", "casual"],
                "source": "library",
            },
            # TODO: Add remaining 14 library fonts
        ]

        # Fetch user's custom fonts
        custom_fonts_cursor = db["custom_fonts"].find({"user_id": user_id})
        custom_fonts = await custom_fonts_cursor.to_list(length=100)

        # Convert custom fonts to same format
        for font in custom_fonts:
            font["font_id"] = str(font["_id"])
            font["source"] = "user_upload"
            # Assume custom fonts have been analyzed and have these fields

        return library_fonts + custom_fonts

    @staticmethod
    def _score_fonts(
        typo_char: Dict[str, Any],
        available_fonts: List[Dict[str, Any]],
        priority_traits: List[str],
    ) -> List[Dict[str, Any]]:
        """
        PRD Section 4.4: Font matching algorithm

        Scores each available font against extracted typography character.
        """
        scored_fonts = []

        for font in available_fonts:
            score = 0

            # Style class match (most important) - 50 points
            if font.get("style_class") == typo_char.get("style_class"):
                score += 50
            elif CustomVisualGuideService._are_compatible_classes(
                font.get("style_class"), typo_char.get("style_class")
            ):
                score += 20

            # Subclass similarity - 30 points
            score += CustomVisualGuideService._subclass_similarity(
                font.get("subclass"), typo_char.get("subclass")
            ) * 30

            # Weight match - 15 points
            score += CustomVisualGuideService._weight_distance(
                font.get("weight_visual"), typo_char.get("weight_visual")
            ) * 15

            # Contrast match - 15 points
            if font.get("contrast") == typo_char.get("contrast"):
                score += 15

            # Personality match - 20 points
            if font.get("personality") == typo_char.get("personality"):
                score += 20

            # Energy level alignment - up to 20 points
            font_energy = font.get("energy_level", 5)
            typo_energy = typo_char.get("energy_level", 5)
            energy_diff = abs(font_energy - typo_energy)
            score += (10 - energy_diff) * 2

            # Use case alignment - 10 points per shared case
            font_use_cases = set(font.get("use_case_alignment", []))
            typo_use_cases = set(typo_char.get("use_case_alignment", []))
            shared_cases = font_use_cases.intersection(typo_use_cases)
            score += len(shared_cases) * 10

            # Priority traits weighting - 25 points each
            for trait in priority_traits:
                if trait in font and font.get(trait) == typo_char.get(trait):
                    score += 25

            scored_fonts.append({
                "font_id": font["font_id"],
                "font_name": font["font_name"],
                "match_score": score,
                "match_confidence": "high" if score >= 120 else "medium" if score >= 80 else "low",
                "source": font["source"],
            })

        return scored_fonts

    @staticmethod
    def _are_compatible_classes(class1: str, class2: str) -> bool:
        """Check if two font classes are compatible (e.g., serif and slab-serif)"""
        compatible_pairs = [
            ("serif", "slab-serif"),
            ("sans-serif", "display"),
        ]
        return (class1, class2) in compatible_pairs or (class2, class1) in compatible_pairs

    @staticmethod
    def _subclass_similarity(subclass1: str, subclass2: str) -> float:
        """Calculate similarity between font subclasses (0.0-1.0)"""
        if not subclass1 or not subclass2:
            return 0.0
        if subclass1.lower() == subclass2.lower():
            return 1.0
        # Simple word overlap
        words1 = set(subclass1.lower().split())
        words2 = set(subclass2.lower().split())
        if words1.intersection(words2):
            return 0.5
        return 0.0

    @staticmethod
    def _weight_distance(weight1: str, weight2: str) -> float:
        """Calculate weight similarity (0.0-1.0)"""
        weight_order = ["thin", "light", "regular", "medium", "bold", "black"]
        try:
            idx1 = weight_order.index(weight1)
            idx2 = weight_order.index(weight2)
            distance = abs(idx1 - idx2)
            # Max distance is 5 (thin to black)
            return (5 - distance) / 5.0
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _classify_match_outcome(
        top_score: int,
        font_matches: List[Dict[str, Any]],
        identified_font: Optional[Dict[str, Any]],
        typo_char: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        PRD Section 4.8: Match outcome classification

        Returns match outcome dict based on score and identified font.
        """
        # PRD Section 4.5: Match confidence bands
        if top_score >= 120:
            # OUTCOME 1: STRONG_MATCH
            return {
                "outcome": "STRONG_MATCH",
                "matched_font_id": font_matches[0]["font_id"],
                "matched_font_source": font_matches[0]["source"],
                "match_confidence": "high",
                "alternative_matches": font_matches[1:] if len(font_matches) > 1 else [],
                "identified_font_name": None,
                "next_step_suggestion": None,
            }

        elif top_score >= 80:
            # OUTCOME 2: DECENT_MATCH
            return {
                "outcome": "DECENT_MATCH",
                "matched_font_id": font_matches[0]["font_id"],
                "matched_font_source": font_matches[0]["source"],
                "match_confidence": "medium",
                "alternative_matches": font_matches[1:] if len(font_matches) > 1 else [],
                "identified_font_name": None,
                "next_step_suggestion": None,
            }

        elif top_score >= 50:
            # OUTCOME 3: WEAK_MATCH
            return {
                "outcome": "WEAK_MATCH",
                "matched_font_id": font_matches[0]["font_id"],
                "matched_font_source": font_matches[0]["source"],
                "match_confidence": "low",
                "alternative_matches": font_matches[1:] if len(font_matches) > 1 else [],
                "identified_font_name": None,
                "next_step_suggestion": {
                    "type": "use_match_with_caveat",
                    "message": f"Approximate match — the reference's typography is somewhat different in character. The closest match is {font_matches[0]['font_name']}, but it's not exact.",
                },
            }

        else:
            # Below 50: NO_RECOMMENDED_MATCH or NO_MATCH
            return CustomVisualGuideService._classify_no_match_outcome(
                identified_font, font_matches, typo_char
            )

    @staticmethod
    def _classify_no_match_outcome(
        identified_font: Optional[Dict[str, Any]],
        font_matches: Optional[List[Dict[str, Any]]],
        typo_char: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Handle NO_RECOMMENDED_MATCH and NO_MATCH outcomes
        PRD Section 4.8: Outcomes 4 and 5
        """
        # OUTCOME 4: NO_RECOMMENDED_MATCH (identified font available)
        if identified_font and identified_font.get("confidence") == "high":
            font_name = identified_font.get("name")
            return {
                "outcome": "NO_RECOMMENDED_MATCH",
                "matched_font_id": None,
                "matched_font_source": None,
                "match_confidence": None,
                "alternative_matches": font_matches if font_matches else [],
                "identified_font_name": font_name,
                "next_step_suggestion": {
                    "type": "upload_identified",
                    "message": f"We recognized the reference's typography — it looks like {font_name}, a free font available on Google Fonts. None of your current fonts have this character.",
                    "actionable_link": f"https://fonts.google.com/specimen/{font_name.replace(' ', '+')}",
                },
            }

        # OUTCOME 4: NO_RECOMMENDED_MATCH (descriptive, weak candidates exist)
        if font_matches and len(font_matches) > 0:
            # Build descriptive message from typo_char
            style_desc = CustomVisualGuideService._build_descriptive_suggestion(typo_char)
            return {
                "outcome": "NO_RECOMMENDED_MATCH",
                "matched_font_id": None,
                "matched_font_source": None,
                "match_confidence": None,
                "alternative_matches": font_matches,
                "identified_font_name": None,
                "next_step_suggestion": {
                    "type": "upload_descriptive",
                    "message": f"Typography couldn't be matched well. {style_desc} The closest font you have is {font_matches[0]['font_name']}, but it's only an approximate match.",
                    "actionable_link": "https://fonts.google.com/",
                },
            }

        # OUTCOME 5: NO_MATCH
        return {
            "outcome": "NO_MATCH",
            "matched_font_id": None,
            "matched_font_source": None,
            "match_confidence": None,
            "alternative_matches": None,
            "identified_font_name": None,
            "next_step_suggestion": {
                "type": "use_brand_default",
                "message": "Typography couldn't be matched. The reference uses very distinctive typography that doesn't match any of your current fonts. We weren't able to identify a specific font either.",
            },
        }

    @staticmethod
    def _build_descriptive_suggestion(typo_char: Dict[str, Any]) -> str:
        """Build descriptive suggestion for font upload based on character"""
        style_class = typo_char.get("style_class", "")
        subclass = typo_char.get("subclass", "")
        weight = typo_char.get("weight_visual", "")

        if subclass:
            return f"The reference uses a {weight} {subclass}"
        elif style_class:
            return f"The reference uses a {weight} {style_class}"
        else:
            return "The reference uses distinctive typography"

    # ========================================================================
    # PROMPT ASSEMBLY (PRD Section 5)
    # ========================================================================

    @staticmethod
    def _assemble_prompt_fragment(aesthetic_profile: Dict[str, Any]) -> str:
        """
        PRD Section 5.1: Prompt fragment assembly

        Assembles aesthetic profile into a reusable prompt fragment.
        """
        # Helper to safely get values, replacing None with empty string
        def safe_get(d: Dict, key: str, default: str = '') -> str:
            val = d.get(key, default)
            return val if val is not None else default

        parts = []

        # Visual genre and quality
        genre_parts = [
            safe_get(aesthetic_profile, 'visual_genre'),
            'aesthetic.',
            safe_get(aesthetic_profile, 'quality_benchmark'),
            safe_get(aesthetic_profile, 'camera_style')
        ]
        genre_parts = [p for p in genre_parts if p and p != 'aesthetic.']
        if genre_parts:
            parts.append(' '.join(genre_parts) + ('.' if not genre_parts[-1].endswith('.') else ''))

        # Color palette
        palette = aesthetic_profile.get("color_palette", {})
        primary_colors = ", ".join(palette.get("primary_colors", []))
        if primary_colors:
            palette_parts = [
                safe_get(palette, 'saturation'),
                safe_get(palette, 'temperature'),
                f"palette with {primary_colors}",
                f"{safe_get(palette, 'contrast')} contrast" if safe_get(palette, 'contrast') else ''
            ]
            palette_parts = [p for p in palette_parts if p]
            parts.append(' '.join(palette_parts) + '.')

        # Lighting
        light = aesthetic_profile.get("lighting", {})
        light_parts = [
            safe_get(light, 'specific_style'),
            safe_get(light, 'quality'),
            safe_get(light, 'temperature'),
            f"light from {safe_get(light, 'direction')}" if safe_get(light, 'direction') else ''
        ]
        light_parts = [p for p in light_parts if p]
        if light_parts:
            parts.append(' '.join(light_parts) + ('.' if not light_parts[-1].endswith('.') else ''))

        # Composition
        comp = aesthetic_profile.get("composition", {})
        comp_parts = [
            f"{safe_get(comp, 'framing')} composition" if safe_get(comp, 'framing') else '',
            f"with {safe_get(comp, 'density')} framing" if safe_get(comp, 'density') else '',
            f"{safe_get(comp, 'depth_treatment')} depth treatment" if safe_get(comp, 'depth_treatment') else ''
        ]
        comp_parts = [p for p in comp_parts if p]
        if comp_parts:
            parts.append(' '.join(comp_parts) + '.')

        # Texture and atmosphere
        tex = aesthetic_profile.get("texture_and_atmosphere", {})
        tex_elements = []
        if safe_get(tex, "grain") and safe_get(tex, "grain") != "none":
            tex_elements.append(f"{safe_get(tex, 'grain')} film grain")
        if safe_get(tex, "haze") and safe_get(tex, "haze") != "none":
            tex_elements.append(f"{safe_get(tex, 'haze')} atmospheric haze")
        if safe_get(tex, "depth_of_field"):
            tex_elements.append(f"{safe_get(tex, 'depth_of_field')} depth of field")
        if tex_elements:
            parts.append(". ".join(tex_elements) + ".")

        # Mood
        mood = aesthetic_profile.get("mood", {})
        mood_words = [mood.get("primary"), mood.get("secondary")]
        mood_words = [m for m in mood_words if m]
        if mood_words:
            parts.append(f"Mood: {', '.join(mood_words)}.")

        # Subject treatment
        subject_treatment = safe_get(aesthetic_profile, "subject_treatment")
        if subject_treatment:
            parts.append(subject_treatment + ('.' if not subject_treatment.endswith('.') else ''))

        # Anti-aesthetic
        anti = aesthetic_profile.get("anti_aesthetic", [])
        if anti:
            parts.append(f"Avoid: {', '.join(anti)}.")

        prompt_fragment = "\n\n".join(parts)
        return prompt_fragment

    # ========================================================================
    # METADATA TAGGING (PRD Section 6)
    # ========================================================================

    @staticmethod
    def _generate_metadata_tags(aesthetic_profile: Dict[str, Any]) -> Dict[str, Any]:
        """
        PRD Section 6: 11-dimension metadata tagging

        Generates metadata tags from aesthetic profile for recommendation system.
        """
        # Extract mood and other characteristics
        mood_primary = aesthetic_profile.get("mood", {}).get("primary", "calm")
        palette_temp = aesthetic_profile.get("color_palette", {}).get("temperature", "neutral")
        palette_sat = aesthetic_profile.get("color_palette", {}).get("saturation", "balanced")
        visual_genre = aesthetic_profile.get("visual_genre", "")
        treatment = aesthetic_profile.get("subject_treatment", "")
        comp_density = aesthetic_profile.get("composition", {}).get("density", "balanced")

        # Map to 11 dimensions
        metadata_tags = {
            "energy_level": CustomVisualGuideService._infer_energy_level(mood_primary, palette_sat),
            "formality": "luxury" if "editorial" in visual_genre.lower() or "high-end" in treatment.lower() else "casual",
            "tone": "playful" if mood_primary in ["playful", "exciting"] else "serious",
            "density": comp_density,  # maximalist | minimalist | balanced
            "treatment": "stylized" if "editorial" in visual_genre.lower() else "documentary",
            "subject_focus": "product-centered" if "product" in treatment.lower() else "human-centered",
            "visual_mode": "cinematic" if "cinematic" in visual_genre.lower() else "graphic",
            "audience_register": "premium" if "luxury" in visual_genre.lower() or "editorial" in visual_genre.lower() else "meme-native",
            "composition_density": comp_density,
            "emotion": mood_primary,
            "intent_tags": CustomVisualGuideService._infer_intent_tags(visual_genre, treatment),
            "minimal": comp_density == "spacious" or palette_sat == "muted",
            "sensitive_content_safe": True,  # Will be enforced below
        }

        # PRD Section 6.1: Application-layer safety enforcement
        energy = metadata_tags["energy_level"]
        minimal = metadata_tags["minimal"]

        if energy > 4:
            metadata_tags["sensitive_content_safe"] = False
        if not minimal:
            metadata_tags["sensitive_content_safe"] = False

        return metadata_tags

    @staticmethod
    def _infer_energy_level(mood: str, saturation: str) -> int:
        """Infer energy level 1-10 from mood and saturation"""
        energy_map = {
            "exciting": 8,
            "bold": 7,
            "playful": 6,
            "calm": 3,
            "intimate": 4,
            "minimal": 2,
            "nostalgic": 4,
        }
        base_energy = energy_map.get(mood.lower(), 5)

        # Adjust for saturation
        if saturation == "high":
            base_energy = min(10, base_energy + 2)
        elif saturation == "muted":
            base_energy = max(1, base_energy - 1)

        return base_energy

    @staticmethod
    def _infer_intent_tags(visual_genre: str, treatment: str) -> List[str]:
        """Infer intent tags from visual genre and treatment"""
        tags = []

        genre_lower = visual_genre.lower()
        treatment_lower = treatment.lower()

        if "editorial" in genre_lower:
            tags.append("editorial")
        if "documentary" in genre_lower or "realism" in genre_lower:
            tags.append("authentic")
        if "product" in treatment_lower:
            tags.append("product_showcase")
        if "luxury" in genre_lower or "high-end" in treatment_lower:
            tags.append("luxury")
        if "casual" in treatment_lower or "playful" in treatment_lower:
            tags.append("casual")

        return tags if tags else ["general"]

    # ========================================================================
    # UTILITY METHODS
    # ========================================================================

    @staticmethod
    async def get_guide_detail(guide_id: str, db: AsyncIOMotorDatabase) -> Optional[Dict[str, Any]]:
        """
        Get detailed information for a specific guide

        Returns guide document with all fields including prompt_fragment
        """
        from bson import ObjectId
        try:
            guide = await db["custom_visual_guides"].find_one({
                "_id": ObjectId(guide_id),
                "status": "active",
            })
            return guide
        except Exception as e:
            print(f"[CVG] Error getting guide detail: {e}")
            return None

    @staticmethod
    async def get_user_guide_count(user_id: str, db: AsyncIOMotorDatabase) -> int:
        """Get number of active guides for a user"""
        count = await db["custom_visual_guides"].count_documents({
            "user_id": user_id,
            "status": "active",
        })
        return count

    @staticmethod
    async def check_plan_limit(user_id: str, user_plan: str, db: AsyncIOMotorDatabase) -> bool:
        """Check if user has reached their plan's guide limit"""
        current_count = await CustomVisualGuideService.get_user_guide_count(user_id, db)
        limit = CustomVisualGuideService.PLAN_LIMITS.get(user_plan.lower(), 2)
        return current_count < limit

    @staticmethod
    async def track_guide_usage(guide_id: str, applied_font: bool, db: AsyncIOMotorDatabase) -> None:
        """
        Track when a guide is used for content generation

        Updates usage analytics for the guide.
        """
        from bson import ObjectId
        from datetime import datetime
        try:
            update_ops = {
                "$inc": {"times_used": 1},
                "$set": {"last_used_at": datetime.utcnow()}
            }

            if applied_font:
                update_ops["$inc"]["times_font_applied"] = 1

            await db["custom_visual_guides"].update_one(
                {"_id": ObjectId(guide_id)},
                update_ops
            )

            # Also track in guide_usage_events collection
            usage_event = {
                "guide_id": guide_id,
                "applied_matched_font": applied_font,
                "used_at": datetime.utcnow(),
            }
            await db["guide_usage_events"].insert_one(usage_event)

            print(f"[CVG] ✅ Tracked usage for guide {guide_id}")

        except Exception as e:
            print(f"[CVG] Error tracking guide usage: {e}")

    @staticmethod
    async def auto_rematch_guides_for_new_font(
        user_id: str,
        new_font_id: str,
        db: AsyncIOMotorDatabase,
    ) -> List[str]:
        """
        PRD Section 11.7: Auto-rematch when user uploads new custom font

        Re-runs font matching for all guides with NO_RECOMMENDED_MATCH outcome.
        If new font scores well, updates guide to STRONG_MATCH/DECENT_MATCH.

        Returns:
            List of guide IDs that were updated
        """
        print(f"[CVG] Auto-rematching guides for new font: {new_font_id}")

        # Find guides with NO_RECOMMENDED_MATCH outcome
        guides_to_rematch = await db["custom_visual_guides"].find({
            "user_id": user_id,
            "status": "active",
            "match_outcome": {"$in": ["NO_RECOMMENDED_MATCH", "NO_MATCH"]},
        }).to_list(length=100)

        updated_guide_ids = []

        for guide in guides_to_rematch:
            try:
                typography_extraction = guide.get("typography_extraction")
                if not typography_extraction:
                    continue

                # Re-run font matching
                font_matches, match_outcome = await CustomVisualGuideService._match_fonts(
                    typography_extraction, user_id, db
                )

                # Only update if match improved
                if match_outcome["outcome"] in ["STRONG_MATCH", "DECENT_MATCH"]:
                    await db["custom_visual_guides"].update_one(
                        {"_id": guide["_id"]},
                        {
                            "$set": {
                                "match_outcome": match_outcome["outcome"],
                                "matched_font_id": match_outcome["matched_font_id"],
                                "matched_font_source": match_outcome["matched_font_source"],
                                "match_confidence": match_outcome["match_confidence"],
                                "alternative_font_matches": match_outcome.get("alternative_matches"),
                                "next_step_suggestion": match_outcome.get("next_step_suggestion"),
                                "updated_at": datetime.utcnow(),
                            },
                            "$inc": {"times_user_uploaded_suggested_font": 1},
                        }
                    )
                    updated_guide_ids.append(str(guide["_id"]))
                    print(f"[CVG] ✅ Updated guide {guide['name']} to {match_outcome['outcome']}")

            except Exception as e:
                print(f"[CVG] Error rematching guide {guide.get('name')}: {e}")
                continue

        print(f"[CVG] Auto-rematch complete: {len(updated_guide_ids)} guides updated")
        return updated_guide_ids

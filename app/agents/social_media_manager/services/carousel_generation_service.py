"""
Carousel (and story caption) generation service.

Generates structured carousel slide content using GPT-4.1.
Each carousel has a caption (used as the overall post text) and N slides,
each with a headline and body copy.

For story posts, generates a short punchy caption (max 125 chars).
"""

import json
import re
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

from app.core.config import settings

_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


class CarouselGenerationService:

    @staticmethod
    def analyze_content_type(seed_content: str) -> Dict[str, Any]:
        """
        Analyze seed content to determine optimal carousel structure.

        Detects:
        - List-based content ("5 mistakes", "10 tips") → extract number
        - Short content (< 15 words) → use minimum slides
        - Story/narrative content → standard slide count

        Returns:
            {
                "type": "list" | "story" | "short",
                "optimal_slides": int,
                "numbered": bool,
                "detected_count": int | None
            }
        """
        seed_lower = seed_content.lower().strip()
        word_count = len(seed_content.split())

        # Detect list-based content with numbers
        # Match patterns like "5 mistakes", "10 tips", "3 ways", etc.
        list_patterns = [
            r'\b(\d+)\s+(ways?|tips?|mistakes?|reasons?|steps?|secrets?|strategies|tactics|methods?|ideas?|hacks?)\b',
            r'\b(top|best)\s+(\d+)\b',
        ]

        for pattern in list_patterns:
            match = re.search(pattern, seed_lower)
            if match:
                # Extract the number
                if match.group(0).startswith(('top', 'best')):
                    count = int(match.group(2))
                else:
                    count = int(match.group(1))

                # Optimal slides = Hook + Count + CTA
                return {
                    "type": "list",
                    "optimal_slides": min(count + 2, 10),  # Cap at 10 slides max
                    "numbered": True,
                    "detected_count": count
                }

        # Short content - use minimum slides
        if word_count < 15:
            return {
                "type": "short",
                "optimal_slides": 3,
                "numbered": False,
                "detected_count": None
            }

        # Default: story/narrative format
        # Medium content (15-50 words) → 5 slides
        # Long content (50+ words) → 7 slides
        if word_count < 50:
            optimal = 5
        else:
            optimal = 7

        return {
            "type": "story",
            "optimal_slides": optimal,
            "numbered": False,
            "detected_count": None
        }

    @staticmethod
    async def generate(
        seed_content: str,
        platform: str,
        brand_context: Optional[Dict[str, Any]] = None,
        num_slides: int = 3,
    ) -> Dict[str, Any]:
        """
        Generate carousel content for a single platform.

        Returns:
            {
                "caption": str,
                "slides": [{"slide_number": int, "headline": str, "body": str}, ...],
                "content_analysis": {...}
            }
        """
        bc = brand_context or {}
        brand_name = bc.get("brand_name", "")
        brand_voice = bc.get("brand_voice", "")
        industry = bc.get("industry", "")
        target_audience = bc.get("target_audience", "")

        # Analyze content to determine optimal slide count
        content_analysis = CarouselGenerationService.analyze_content_type(seed_content)

        # Override num_slides with intelligent detection (unless explicitly forced)
        # If user explicitly requested a count, respect it. Otherwise use detected optimal.
        if num_slides == 3:  # Default value, use intelligent detection
            num_slides = content_analysis["optimal_slides"]
        else:
            # User specified custom count, but cap it
            num_slides = max(2, min(10, num_slides))

        print(f"📊 Carousel analysis: type={content_analysis['type']}, optimal_slides={content_analysis['optimal_slides']}, using={num_slides}")

        brand_block = ""
        if brand_name:
            brand_block += f"Brand name: {brand_name}\n"
        if industry:
            brand_block += f"Industry: {industry}\n"
        if brand_voice:
            brand_block += f"Brand voice: {brand_voice}\n"
        if target_audience:
            brand_block += f"Target audience: {target_audience}\n"

        # Build content-type-specific structure instructions
        if content_analysis["type"] == "list" and content_analysis["numbered"]:
            structure_note = f"""
STRICT STRUCTURE FOR {num_slides} SLIDES:
- Slide 1: Hook (attention-grabbing headline that creates curiosity)
- Slides 2-{num_slides - 1}: Value slides (one clear point per slide, use numbered format: 1., 2., 3., etc.)
- Slide {num_slides}: CTA (call to action with clear next step)

This is a LIST-BASED carousel with {content_analysis['detected_count']} points.
Each value slide should deliver ONE complete idea."""
        else:
            structure_note = f"""
STRICT STRUCTURE FOR {num_slides} SLIDES:
- Slide 1: Hook (attention-grabbing headline that makes people want to swipe)
- Slides 2-{num_slides - 1}: Value slides (each slide builds on the previous, delivering clear value)
- Slide {num_slides}: CTA (strong call to action with next step)

This is a {content_analysis['type'].upper()} carousel. Build a cohesive narrative."""

        system_prompt = (
            "You are an expert social media copywriter specialising in carousel posts "
            f"for {platform}. Your job is to create engaging, scroll-stopping carousel content.\n\n"
            f"{structure_note}\n\n"
            "Rules:\n"
            "- Each headline: ≤8 words, punchy and bold\n"
            "- Each body: ≤25 words, clear and scannable\n"
            "- Overall caption: engaging hook + relevant hashtags, suitable for the platform\n"
            "- The carousel must tell a complete, cohesive story from slide 1 to slide N\n"
            "- Each slide must build on the previous slide\n"
            "- Return ONLY valid JSON — no markdown, no extra text\n\n"
            "JSON schema:\n"
            '{"caption": "string", "slides": [{"headline": "string", "body": "string"}]}'
        )

        user_prompt = (
            f"{brand_block}\n"
            f"Topic / seed content:\n{seed_content}\n\n"
            f"Generate exactly {num_slides} slides following the structure above."
        ).strip()

        try:
            response = await _client.chat.completions.create(
                model="gpt-4.1",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.75,
                max_tokens=800,
                response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content or "{}"
            data = json.loads(raw)

            caption = data.get("caption", "")
            slides_raw = data.get("slides", [])

            # Normalise slides and add slide_number
            slides: List[Dict[str, str]] = []
            for idx, s in enumerate(slides_raw[:num_slides]):
                slides.append({
                    "slide_number": idx + 1,  # 1-indexed for display
                    "headline": str(s.get("headline", "")).strip(),
                    "body": str(s.get("body", "")).strip(),
                })

            # Pad if GPT returned fewer slides than requested
            while len(slides) < num_slides:
                slides.append({
                    "slide_number": len(slides) + 1,
                    "headline": "Key Insight",
                    "body": "Stay tuned for more details."
                })

            return {
                "caption": caption,
                "slides": slides,
                "content_analysis": content_analysis
            }

        except Exception as e:
            print(f"⚠️ CarouselGenerationService.generate error: {e}")
            # Graceful fallback
            slides = [
                {
                    "slide_number": i + 1,
                    "headline": f"Slide {i + 1}",
                    "body": seed_content[:50]
                }
                for i in range(num_slides)
            ]
            return {
                "caption": seed_content[:200],
                "slides": slides,
                "content_analysis": content_analysis
            }

    @staticmethod
    async def generate_story_caption(
        seed_content: str,
        platform: str,
        brand_context: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Generate a very short story caption (max 125 chars).
        Punchy, emoji-friendly, no hashtags.
        """
        bc = brand_context or {}
        brand_voice = bc.get("brand_voice", "")

        system_prompt = (
            "You are a social media copywriter. Generate a very short, punchy caption "
            f"for a {platform} Story post. Max 125 characters. Use 1-2 emojis. "
            "No hashtags. Direct, impactful tone."
            + (f" Brand voice: {brand_voice}." if brand_voice else "")
            + " Return ONLY the caption text, nothing else."
        )

        try:
            response = await _client.chat.completions.create(
                model="gpt-4.1",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Topic: {seed_content}"},
                ],
                temperature=0.8,
                max_tokens=60,
            )
            caption = (response.choices[0].message.content or "").strip()
            return caption[:125]
        except Exception as e:
            print(f"⚠️ CarouselGenerationService.generate_story_caption error: {e}")
            return seed_content[:125]

    @staticmethod
    async def generate_multi_platform(
        user_id: str,
        seed_content: str,
        platforms: List[str],
        brand_context: Optional[Dict[str, Any]] = None,
        num_slides: int = 3,
        db=None,
    ) -> Dict[str, Any]:
        """
        Generate carousel content for multiple platforms and persist drafts to DB.
        Returns the same shape as ContentGenerationService.generate_multi_platform_content.
        """
        from datetime import datetime
        import uuid

        # One request_id shared across all platform drafts in this generation
        request_id = str(uuid.uuid4())

        drafts = []
        for platform in platforms:
            carousel_data = await CarouselGenerationService.generate(
                seed_content=seed_content,
                platform=platform,
                brand_context=brand_context,
                num_slides=num_slides,
            )

            draft_id = str(uuid.uuid4())
            slides_with_specs = []
            for slide in carousel_data["slides"]:
                slides_with_specs.append({
                    "slide_number": slide.get("slide_number", len(slides_with_specs) + 1),
                    "headline": slide["headline"],
                    "body": slide["body"],
                    "image_url": None,
                    "image_specs": {"width": 1080, "height": 1080},
                    "image_retry_count": 0,  # Track retries per slide
                    "image_failed": False,  # Track if image generation failed
                })

            draft_doc = {
                "id": draft_id,
                "request_id": request_id,
                "user_id": user_id,
                "platform": platform,
                "content": carousel_data["caption"],
                "post_type": "carousel",
                "slides": slides_with_specs,
                "hashtags": [],
                "status": "draft",
                "approval_status": "pending",
                "has_image": False,
                "image_retry_count": 0,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }

            if db is not None:
                try:
                    await db["content_drafts"].insert_one(draft_doc)
                except Exception as db_err:
                    print(f"⚠️ CarouselGenerationService DB insert error: {db_err}")

            # Serialise for the response (remove _id, convert datetime)
            draft_response = {k: v for k, v in draft_doc.items() if k != "_id"}
            draft_response["created_at"] = draft_doc["created_at"].isoformat()
            draft_response["updated_at"] = draft_doc["updated_at"].isoformat()
            drafts.append(draft_response)

        return {
            "status": True,
            "responseMessage": f"Generated {len(drafts)} carousel draft(s)",
            "responseData": {"drafts": drafts, "request_id": request_id},
        }

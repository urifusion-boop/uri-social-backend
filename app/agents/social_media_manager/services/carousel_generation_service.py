"""
Carousel (and story caption) generation service.

Generates structured carousel slide content using GPT-4.1.
Each carousel has a caption (used as the overall post text) and N slides,
each with a headline and body copy.

For story posts, generates a short punchy caption (max 125 chars).
"""

import json
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

from app.core.config import settings

_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


class CarouselGenerationService:

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
                "slides": [{"headline": str, "body": str}, ...]
            }
        """
        bc = brand_context or {}
        brand_name = bc.get("brand_name", "")
        brand_voice = bc.get("brand_voice", "")
        industry = bc.get("industry", "")
        target_audience = bc.get("target_audience", "")
        num_slides = max(2, min(5, num_slides))

        brand_block = ""
        if brand_name:
            brand_block += f"Brand name: {brand_name}\n"
        if industry:
            brand_block += f"Industry: {industry}\n"
        if brand_voice:
            brand_block += f"Brand voice: {brand_voice}\n"
        if target_audience:
            brand_block += f"Target audience: {target_audience}\n"

        system_prompt = (
            "You are an expert social media copywriter specialising in carousel posts "
            f"for {platform}. Your job is to create engaging, scroll-stopping carousel content.\n\n"
            "Carousel structure:\n"
            "- Slide 1 (Hook): attention-grabbing question or surprising stat that makes people swipe\n"
            "- Middle slides (Value): practical tips, facts, or numbered steps that deliver real value\n"
            "- Last slide (CTA): clear call-to-action that tells the audience what to do next\n\n"
            "Rules:\n"
            "- Each headline: ≤8 words, punchy and bold\n"
            "- Each body: ≤25 words, clear and scannable\n"
            "- Overall caption: engaging hook + relevant hashtags, suitable for the platform\n"
            "- Return ONLY valid JSON — no markdown, no extra text\n\n"
            "JSON schema:\n"
            '{"caption": "string", "slides": [{"headline": "string", "body": "string"}]}'
        )

        user_prompt = (
            f"{brand_block}\n"
            f"Topic / seed content:\n{seed_content}\n\n"
            f"Generate a {num_slides}-slide carousel post."
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

            # Normalise slides
            slides: List[Dict[str, str]] = []
            for s in slides_raw[:num_slides]:
                slides.append({
                    "headline": str(s.get("headline", "")).strip(),
                    "body": str(s.get("body", "")).strip(),
                })

            # Pad if GPT returned fewer slides than requested
            while len(slides) < num_slides:
                slides.append({"headline": "Key Insight", "body": "Stay tuned for more details."})

            return {"caption": caption, "slides": slides}

        except Exception as e:
            print(f"⚠️ CarouselGenerationService.generate error: {e}")
            # Graceful fallback
            slides = [
                {"headline": f"Slide {i + 1}", "body": seed_content[:50]}
                for i in range(num_slides)
            ]
            return {"caption": seed_content[:200], "slides": slides}

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
                    "headline": slide["headline"],
                    "body": slide["body"],
                    "image_url": None,
                    "image_specs": {"width": 1080, "height": 1080},
                })

            draft_doc = {
                "id": draft_id,
                "user_id": user_id,
                "platform": platform,
                "content": carousel_data["caption"],
                "post_type": "carousel",
                "slides": slides_with_specs,
                "hashtags": [],
                "status": "draft",
                "approval_status": "pending",
                "has_image": False,
                "image_retry_count": 0,  # PRD 4.3: Track image retry count
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
            "responseData": {"drafts": drafts},
        }

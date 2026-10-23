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
        force_num_slides: bool = False,
    ) -> Dict[str, Any]:
        """
        Generate carousel content for a single platform.

        force_num_slides: skip the content-based slide-count detection and use
        num_slides exactly (still capped 2-10). Calendar V2 sets this — its
        pipeline already decided the slide count deterministically and the
        draft must not silently expand a 3-slide idea into 7.

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
        if force_num_slides:
            # Caller decided the count deliberately — respect it exactly.
            num_slides = max(2, min(10, num_slides))
        elif num_slides == 3:  # Default value, use intelligent detection
            num_slides = content_analysis["optimal_slides"]
        else:
            # User specified custom count, but cap it
            num_slides = max(2, min(10, num_slides))

        print(f"📊 Carousel analysis: type={content_analysis['type']}, optimal_slides={content_analysis['optimal_slides']}, using={num_slides}")

        # Same full Brand Playbook instructions single posts get (voice, audience,
        # customer psychology, guardrails, words to avoid, CTAs, languages...) —
        # carousels used to get only name/industry/voice/audience.
        from .content_generation_service import ContentGenerationService
        brand_block = ContentGenerationService._build_brand_block(bc, platform=platform)
        if not brand_block:
            for label, value in (("Brand name", brand_name), ("Industry", industry),
                                 ("Brand voice", brand_voice), ("Target audience", target_audience)):
                if value:
                    brand_block += f"{label}: {value}\n"

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

        # The brand's own hashtag limit wins over the default 3-6.
        from . import brand_brief
        _limit = brand_brief.hashtag_limit(bc)
        hashtag_rule = "3-6" if _limit is None or _limit >= 6 else (f"up to {_limit}" if _limit > 0 else "no")

        system_prompt = (
            "You are an expert social media copywriter specialising in carousel posts "
            f"for {platform}. Your job is to create engaging, scroll-stopping carousel content.\n\n"
            f"{structure_note}\n\n"
            "Rules:\n"
            "- Each headline: ≤8 words, punchy and bold\n"
            "- Each body: ≤25 words, clear and scannable\n"
            "- Overall caption: engaging hook, suitable for the platform\n"
            f"- End the caption with {hashtag_rule} relevant, specific hashtags (not generic ones like #social "
            "or #post) — always include hashtags, this is not optional\n"
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

        # Everything the writer was given — a statistic or source not in here was invented.
        sources = f"{seed_content}\n{json.dumps(bc, default=str, ensure_ascii=False)}"

        async def _write(correction: str = "") -> Dict[str, Any]:
            response = await _client.chat.completions.create(
                model="gpt-4.1",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt + correction},
                ],
                temperature=0.75,
                # Scale with slide count: a fixed 800 can cut the JSON off mid-way
                # on 8-10 slide carousels, which then fails json.loads.
                max_tokens=max(800, 200 + 150 * num_slides),
                response_format={"type": "json_object"},
            )
            return json.loads(response.choices[0].message.content or "{}")

        def _issues(d: Dict[str, Any]) -> List[Dict[str, str]]:
            """Playbook rules broken anywhere in the carousel. Banned words and
            invented facts count on every slide; language and the call-to-action
            are judged on the caption, where they belong."""
            caption_text = re.sub(r"#\w+", "", str(d.get("caption", "")))
            slide_text = "\n".join(f"{x.get('headline', '')}. {x.get('body', '')}"
                                   for x in (d.get("slides") or []) if isinstance(x, dict))
            found = brand_brief.audit(caption_text, bc, sources=sources)
            seen = {(i["code"], i["detail"]) for i in found}
            for i in brand_brief.audit(slide_text, bc, sources=sources):
                if i["code"] in ("banned_word", "unverified_number", "unverified_source") \
                        and (i["code"], i["detail"]) not in seen:
                    found.append(i)
            # brand_brief.audit's "banned_word" check only ever reads the
            # BRAND's own custom words_to_avoid (empty by default) — the
            # universal generic-AI-slop list (CaptionValidatorService.
            # BANNED_WORDS, which feed/story posts already check) was never
            # consulted here. Confirmed live: a real carousel slide headline
            # came back "...To Seamless Content" despite "seamless" being
            # banned there since before this fix — carousels simply never
            # checked that list at all. Checked on the same combined text
            # single posts use (CaptionValidatorService._for_validation
            # isn't platform-specific enough to need here; a plain strip of
            # hashtags, already done above for caption_text, is enough).
            from .caption_validator_service import CaptionValidatorService
            for text, origin in ((caption_text, "caption"), (slide_text, "slides")):
                style = CaptionValidatorService.validate_caption(text)
                for issue in style["issues"]:
                    if not issue.startswith("banned_word:"):
                        continue
                    word = issue.split(": ", 1)[1].strip("'")
                    key = ("banned_word", word)
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append({"code": "banned_word", "detail": word,
                                 "fix": f'Remove "{word}" from the {origin} in every form (plural, -ed, -ing) '
                                        "and say it another way."})
            return found

        try:
            data = await _write()
            # Rewrite while the carousel still breaks the brand's rules, as
            # captions get — up to 2 attempts, stopping the moment it's clean.
            # A single retry could still fail to comply (confirmed live: a
            # rewrite asked to remove "effortless" shipped "effortlessly"
            # instead) and the old code just kept whichever draft merely had
            # *fewer* issues, not necessarily zero. More attempts raise the
            # odds a real rewrite lands clean; brand_brief.enforce() below is
            # the actual guarantee regardless of how this loop ends.
            issues = _issues(data)
            attempts = 0
            while issues and attempts < 2:
                attempts += 1
                print(f"📊 Carousel breaks Playbook rules (attempt {attempts}): "
                      f"{[i['code'] + ':' + i['detail'] for i in issues]} — rewriting")
                try:
                    retry = await _write(
                        "\n\nCORRECTION: your last attempt broke this brand's rules. Write the carousel again "
                        "with these fixes:\n" + "\n".join(f"- {i['fix']}" for i in issues)
                    )
                    retry_issues = _issues(retry)
                    if retry.get("slides") and len(retry_issues) < len(issues):
                        data, issues = retry, retry_issues
                    else:
                        break  # no improvement — stop spending retries, fall back to enforce()
                except Exception as retry_error:
                    print(f"⚠️ Carousel rewrite failed, keeping the last good version: {retry_error}")
                    break

            caption_raw = data.get("caption", "")
            slides_raw = data.get("slides", [])

            # Same convention as feed/story posts: hashtags live in their own
            # field, not mixed into the displayed caption text. DraftCard only
            # renders the hashtag pills from this separate list, so without
            # this extraction step carousels silently showed no hashtags at
            # all even when the model did include them in the caption string.
            from .content_generation_service import ContentGenerationService
            caption, hashtags = ContentGenerationService._extract_and_clean_hashtags(caption_raw, platform)
            # Guaranteed without a model: no emojis if the brand said none, the
            # brand's hashtag limit, and its call-to-action closing the caption.
            caption = brand_brief.enforce(caption, bc)
            hashtags = brand_brief.limit_hashtags(hashtags, bc)

            # Normalise slides and add slide_number
            slides: List[Dict[str, str]] = []
            for idx, s in enumerate(slides_raw[:num_slides]):
                slides.append({
                    "slide_number": idx + 1,  # 1-indexed for display
                    "headline": brand_brief.enforce(str(s.get("headline", "")).strip(), bc, append_cta=False),
                    "body": brand_brief.enforce(str(s.get("body", "")).strip(), bc, append_cta=False),
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
                "hashtags": hashtags,
                "slides": slides,
                "content_analysis": content_analysis,
                # Playbook rules still broken after the rewrite (normally empty).
                "playbook_issues": [f"{i['code']}: {i['detail']}" for i in issues
                                    if i["code"] != "missing_cta"],
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
                "hashtags": [],
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
        force_num_slides: bool = False,
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
                force_num_slides=force_num_slides,
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
                "hashtags": carousel_data.get("hashtags", []),
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

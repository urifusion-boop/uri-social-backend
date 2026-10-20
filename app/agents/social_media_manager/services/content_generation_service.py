# app/agents/social_media_manager/services/content_generation_service.py

import asyncio
import json
import re
from typing import List, Dict, Optional, Any, Tuple
from . import brand_brief

# Platform formats (e.g. LinkedIn's "end with an engaging question") were winning
# over the brand's own call to action — confirmed on dev. The CTA closes the post.
_CTA_LAST_LINE = (
    "The call-to-action is the FINAL line of the post. If the platform format also asks for a "
    "closing question, put the question just before it — never instead of it."
)
from datetime import datetime
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.AIService import AIService
from app.domain.responses.uri_response import UriResponse
# Caption Voice System imports
from app.agents.social_media_manager.services.caption_validator_service import CaptionValidatorService
from app.agents.social_media_manager.services.caption_voice_system import (
    BANNED_PATTERNS_RULES,
    build_voice_profile_instructions,
    get_platform_formatting_rules,
)


class ContentGenerationService:
    """
    AI-powered content generation service that creates platform-native content
    
    This service integrates with existing URI infrastructure:
    - Uses existing AIService for content generation
    - Follows URI response patterns
    - Integrates with URI database and user management
    - Optimized for Nigerian business context
    
    Features:
    - Platform-specific prompts (LinkedIn B2B, Twitter threads, Facebook community)
    - Concurrent generation for multiple platforms
    - Hashtag extraction and content optimization
    - Regeneration with feedback
    - Nigerian business context integration
    """
    
    # Platform-specific prompts optimized for Nigerian business engagement
    PLATFORM_PROMPTS = {
        "linkedin": """
You are a seasoned Nigerian business leader writing for LinkedIn. Your audience consists of CEOs, entrepreneurs, and professionals in Nigeria and across Africa.

CONTENT TO TRANSFORM: {seed_content}

REQUIREMENTS:
- Start with a strong, attention-grabbing hook (first line must make people stop scrolling)
- Professional but conversational tone (approachable authority)
- Use lots of white space for readability (single line paragraphs)
- Include 2-3 specific insights, stats, or examples relevant to Nigerian/African business
- End with an engaging question to drive comments
- 150-300 words maximum
- Focus on business impact and lessons learned
- Use Nigerian context when relevant (Lagos, Abuja, SMEs, Naira, local market dynamics)

FORMAT:
- Short, punchy opening line
- 2-3 paragraphs with single-line spacing  
- Bullet points if listing benefits/insights
- Thought-provoking question at the end

TONE: Think Aliko Dangote sharing business wisdom, or Tony Elumelu discussing entrepreneurship.

Do NOT include hashtags in the main content - they will be added separately.

Write as if you're sharing hard-won business wisdom with fellow African entrepreneurs.
        """,
        
        "twitter": """
You are creating a viral Twitter thread optimized for Nigerian business and tech audiences. Think Jason Njoku, Tope Awotona, or Iyinoluwa Aboyeji sharing insights.

CONTENT TO TRANSFORM: {seed_content}

REQUIREMENTS:
- Thread format: 3-5 tweets maximum
- First tweet MUST be a contrarian or surprising hook
- Each tweet max 250 characters (leave room for thread numbering)
- No cringe emojis or excessive punctuation
- Optimize for retweets and engagement
- Each tweet should work standalone but flow as a sequence
- Use thread numbering: (1/4), (2/4), etc.
- Focus on insights, not fluff
- Reference Nigerian context where relevant (Fintech growth, tech hubs, SME challenges)

TONE: Punchy, confident, slightly contrarian. Think startup founder dropping knowledge bombs about African tech/business.

FORMAT: Return as a JSON array of tweets like:
[
  "Tweet 1 content here (1/4)",
  "Tweet 2 content here (2/4)", 
  "Tweet 3 content here (3/4)",
  "Tweet 4 content here (4/4)"
]

Make people think "I never looked at it this way" and want to retweet.
        """,
        
        "facebook": """
You are writing a Facebook post for a Nigerian business page. The post must feel natural and human — NOT like a template.

CONTENT TO TRANSFORM: {seed_content}

CRITICAL RULES:
- NEVER start with "Hey", "Hi", or any greeting to the audience
- NEVER use filler phrases like "I'm excited to share", "I hope this finds you well", "I'm beyond excited", "fellow entrepreneurs"
- NO excessive emojis — maximum 2, and only if they genuinely add meaning
- NO toxic positivity or hype language ("game-changer", "amazing", "vibrant community", "let's rise together")
- DO NOT follow a rigid 3-part template — vary the structure based on what the content actually calls for

WHAT MAKES A GREAT FACEBOOK POST:
- Opens with the most interesting or surprising fact, statement, or question from the content
- Gets to the point immediately — no warm-up
- Reads like something a real person would actually post, not a marketing department
- 80-150 words
- One clear call-to-action or question at the end (not multiple)
- May use Nigerian business context naturally where it fits (not forced)

TONE: Direct, credible, human. Think of a business owner sharing a genuine update or observation — not a brand account performing enthusiasm.

Write one post. No alternatives, no meta-commentary.
        """,
        
        "instagram": """
You are creating Instagram content for a Nigerian business account targeting entrepreneurs and SMEs across Africa.

CONTENT TO TRANSFORM: {seed_content}

REQUIREMENTS:
- Visual storytelling focus (assume there will be accompanying images)
- Casual but inspiring tone
- 100-150 words (Instagram users scan quickly)
- Use line breaks for visual appeal
- Include call-to-action for engagement
- Motivational but authentic (no toxic positivity)
- Reference visual elements when relevant
- Nigerian business context where appropriate

FORMAT:
- Attention-grabbing opening
- Short paragraphs with line breaks
- Inspiring but practical message
- Clear call-to-action

TONE: Inspirational Nigerian founder sharing the journey. Think visual storyteller meets business mentor.

Write as if you're sharing behind-the-scenes insights from building a successful Nigerian business.
        """,

        "x": """
You are creating a viral X (Twitter) thread optimized for Nigerian business and tech audiences. Think Jason Njoku, Tope Awotona, or Iyinoluwa Aboyeji sharing insights.

CONTENT TO TRANSFORM: {seed_content}

REQUIREMENTS:
- Thread format: 3-5 tweets maximum
- First tweet MUST be a contrarian or surprising hook
- Each tweet max 250 characters (leave room for thread numbering)
- No cringe emojis or excessive punctuation
- Optimize for retweets and engagement
- Each tweet should work standalone but flow as a sequence
- Use thread numbering: (1/4), (2/4), etc.
- Focus on insights, not fluff
- Reference Nigerian context where relevant (Fintech growth, tech hubs, SME challenges)

TONE: Punchy, confident, slightly contrarian. Think startup founder dropping knowledge bombs about African tech/business.

FORMAT: Return as a JSON array of tweets like:
[
  "Tweet 1 content here (1/4)",
  "Tweet 2 content here (2/4)",
  "Tweet 3 content here (3/4)",
  "Tweet 4 content here (4/4)"
]

Make people think "I never looked at it this way" and want to retweet.
        """,

        "linkedin": """
You are a seasoned Nigerian business leader writing for LinkedIn. Your audience consists of CEOs, entrepreneurs, and professionals in Nigeria and across Africa.

CONTENT TO TRANSFORM: {seed_content}

REQUIREMENTS:
- Start with a strong, attention-grabbing hook (first line must make people stop scrolling)
- Professional but conversational tone (approachable authority)
- Use lots of white space for readability (single line paragraphs)
- Include 2-3 specific insights, stats, or examples relevant to Nigerian/African business
- End with an engaging question to drive comments
- 150-300 words maximum
- Focus on business impact and lessons learned
- Use Nigerian context when relevant (Lagos, Abuja, SMEs, Naira, local market dynamics)

FORMAT:
- Short, punchy opening line
- 2-3 paragraphs with single-line spacing
- Bullet points if listing benefits/insights
- Thought-provoking question at the end

TONE: Think Aliko Dangote sharing business wisdom, or Tony Elumelu discussing entrepreneurship.

Do NOT include hashtags in the main content - they will be added separately.

Write as if you're sharing hard-won business wisdom with fellow African entrepreneurs.
        """,

        "tiktok": """
You are writing a TikTok caption for a Nigerian business account's photo/graphic post. The caption supports the image — it doesn't replace it.

CONTENT TO TRANSFORM: {seed_content}

REQUIREMENTS:
- Very short — 1-3 lines max, TikTok truncates after ~1 line
- Ultra-casual, meme-friendly, Gen Z language patterns
- One punchy hook or POV-style line, not a paragraph
- No corporate/marketing tone — read like a real person's caption, not an ad
- Nigerian business context where it genuinely fits (Lagos, SMEs, Naira) — never forced

TONE: Think a Nigerian founder posting on their own TikTok, not a brand account performing enthusiasm.

Do NOT include hashtags in the main content - they will be added separately.

Write one caption. No alternatives, no meta-commentary.
        """
    }
    
    @staticmethod
    def _brand_banned_words(brand_context: Optional[Dict[str, Any]]) -> List[str]:
        """Every word the brand has said never to use (see brand_brief.banned_words)."""
        return brand_brief.banned_words(brand_context)

    @staticmethod
    def build_caption_prompt(seed_content: str, platform: str, brand_context: Optional[Dict[str, Any]]) -> str:
        """The complete prompt a caption is written from. One place, so real
        posts and "Preview Jane's voice" can never drift apart. Order:
          1. Banned patterns (universal)
          2. Formatting rules (universal + platform)
          3. Voice profile (brand)
          4. Brand instructions (the Playbook)
          5. The platform's own prompt, with the topic
        """
        brand_context = brand_brief.for_platform(brand_context, platform)
        brand_block = ContentGenerationService._build_brand_block(brand_context, platform=platform)
        platform_prompt = ContentGenerationService.PLATFORM_PROMPTS[platform].format(seed_content=seed_content)
        voice_instructions = ""
        if brand_context and brand_context.get("voice_profile"):
            voice_profile = dict(brand_context["voice_profile"])
            # The voice profile's defaults ("1-2 emojis", "1-3 hashtags") used to
            # contradict a Playbook that said none, in the same prompt.
            if brand_brief.emoji_mode(brand_context) == "no":
                voice_profile["emoji_usage"] = "none"
            if brand_brief.hashtag_limit(brand_context) == 0:
                voice_profile["hashtag_style"] = "none"
            voice_instructions = build_voice_profile_instructions(
                voice_profile,
                brand_context.get("voice_sample_analysis"),
                platform,
                language_rule=brand_brief.languages_rule(brand_context),
            )
        # Without this the model wrote "if you're still typing invoices by hand in 2023" (seen on dev).
        today = f"Today's date is {datetime.utcnow():%d %B %Y}. Never refer to an earlier year as the current one."
        parts = [
            BANNED_PATTERNS_RULES,
            get_platform_formatting_rules(platform),
            voice_instructions,
            brand_block,
            today,
            platform_prompt,
        ]
        return "\n".join(p for p in parts if p)

    @staticmethod
    def _build_brand_block(brand_context: Optional[Dict[str, Any]], platform: str = "") -> str:
        """
        Build directive brand instructions from every onboarding field.
        Injected at the top of every generation prompt.
        """
        if not brand_context:
            return ""

        # Hard rules, voice and audience come from the shared Brand Brief so
        # every generator states them the same way, in the same priority order.
        parts = [
            "BRAND INSTRUCTIONS — you are writing on behalf of this specific brand. Apply every rule below.",
            brand_brief.PRIORITY_ORDER,
            brand_brief.TRUTH_RULE,
        ]
        rules = brand_brief.hard_rules(brand_context, captions=True, platform=platform)
        if rules:
            parts.append("HARD RULES (never break these):")
            parts.extend(f"- {r}" for r in rules)

        # ── Core identity ─────────────────────────────────────────────────────
        if brand_context.get("brand_name"):
            name = brand_context["brand_name"]
            parts.append(
                f'- Brand name is "{name}". Mention it naturally at least once '
                f'(e.g. "At {name}..." or "{name} helps..." — never force it).'
            )

        if brand_context.get("tagline"):
            parts.append(
                f'- Brand tagline: "{brand_context["tagline"]}". '
                f'Weave it naturally into the post or use it as a closing line.'
            )

        if brand_context.get("industry"):
            parts.append(
                f'- Industry: {brand_context["industry"]}. '
                f'Use vocabulary and references that credible voices in this industry use.'
            )

        if brand_context.get("business_description"):
            parts.append(
                f'- What the business does: {brand_context["business_description"]}. '
                f'Keep the content grounded in what the brand actually offers.'
            )

        if brand_context.get("key_products_services"):
            services = brand_context["key_products_services"]
            if isinstance(services, list) and services:
                parts.append(
                    f'- Key products/services: {", ".join(services[:6])}. '
                    f'Reference the most relevant one naturally — do not list them all mechanically.'
                )

        if brand_context.get("website"):
            parts.append(
                f'- Brand website: {brand_context["website"]}. '
                f'You may reference it naturally in a CTA if it fits the content.'
            )

        if brand_context.get("unique_selling_proposition"):
            parts.append(
                f'- What makes this brand different: {brand_context["unique_selling_proposition"]}. '
                f'Let this differentiation show through naturally, especially in hooks or CTAs.'
            )

        if brand_context.get("price_range"):
            parts.append(
                f'- Price positioning: {brand_context["price_range"]}. '
                f'Match the tone and vocabulary to this price tier (e.g. don\'t use budget '
                f'language for a premium brand, or exclusive/luxury language for a budget one).'
            )

        if brand_context.get("business_stage"):
            # Same wording as calendar_rules.py's _STAGE_GUIDANCE — a
            # single source of truth so a single post and a calendar week never
            # disagree on what "new"/"growing"/etc. actually means for tone.
            from .calendar_rules import _STAGE_GUIDANCE
            guidance = _STAGE_GUIDANCE.get(brand_context["business_stage"])
            if guidance:
                parts.append(f'- Business stage: this is {guidance}.')

        priorities = brand_context.get("business_priorities")
        if isinstance(priorities, list) and priorities:
            parts.append(
                f'- Current business priorities: {", ".join(priorities[:4])}. '
                f'Lean into these where the post naturally supports them.'
            )

        # ── Voice & tone ──────────────────────────────────────────────────────
        # Use platform-specific tone if available and same_tone_everywhere is False
        platform_tones = brand_context.get("platform_tones") or {}
        same_tone = brand_context.get("same_tone_everywhere", True)
        platform_tone = platform_tones.get(platform) if (platform and not same_tone) else None

        active_voice = platform_tone or brand_context.get("brand_voice", "")
        if active_voice:
            parts.append(
                f'- Brand voice/tone: {active_voice}. '
                f'Every sentence must sound like this brand — this overrides any default tone.'
            )

        if brand_context.get("voice_sample"):
            parts.append(
                f'- Real example of this brand\'s writing: "{brand_context["voice_sample"][:400]}". '
                f'Mirror the sentence structure, vocabulary, and energy of this sample exactly.'
            )

        # ── Audience ─────────────────────────────────────────────────────────
        if brand_context.get("target_audience"):
            parts.append(
                f'- Target audience: {brand_context["target_audience"]}. '
                f'Write as if speaking directly to them — use their language and reference their world.'
            )

        if brand_context.get("ideal_customer_profile"):
            parts.append(
                f'- Ideal Customer Profile: {brand_context["ideal_customer_profile"]}. '
                f'Tailor all content specifically to resonate with this exact persona — their interests, pain points, and aspirations.'
            )

        # Target Customer Detail — a short combined line in aggregate (occupation/
        # location/income), plus pain points and "why they choose us" separately,
        # since those two are directly actionable for a single post's hook/angle.
        cust_bits = []
        if brand_context.get("customer_gender"):
            cust_bits.append(brand_context["customer_gender"])
        if brand_context.get("customer_occupation"):
            cust_bits.append(brand_context["customer_occupation"])
        if brand_context.get("customer_location"):
            cust_bits.append(f'in {brand_context["customer_location"]}')
        if brand_context.get("customer_income_level"):
            cust_bits.append(f'{brand_context["customer_income_level"]} income')
        if cust_bits:
            parts.append(
                f'- Customer profile: {", ".join(cust_bits)}. '
                f'Reflect this in the references and examples you use.'
            )

        pain_points = brand_context.get("customer_pain_points")
        if isinstance(pain_points, list) and pain_points:
            parts.append(
                f'- Customer pain points: {", ".join(pain_points[:4])}. '
                f'Speak to at least one of these where relevant.'
            )

        interests = brand_context.get("customer_interests")
        if isinstance(interests, list) and interests:
            parts.append(
                f'- Customer interests: {", ".join(interests[:4])}. '
                f'Draw references or analogies from these where it fits naturally.'
            )

        needs = brand_context.get("customer_needs")
        if isinstance(needs, list) and needs:
            parts.append(
                f'- Customer needs: {", ".join(needs[:4])}. '
                f'Frame the value of the product/service around meeting these.'
            )

        objections = brand_context.get("customer_objections")
        if isinstance(objections, list) and objections:
            parts.append(
                f'- Common objections customers have: {", ".join(objections[:4])}. '
                f'Where the post is persuasive in nature, preempt or address at least one.'
            )

        if brand_context.get("why_customers_choose_us"):
            parts.append(
                f'- Why customers choose this brand: {brand_context["why_customers_choose_us"]}. '
                f'Let this reasoning surface naturally, especially in CTAs.'
            )

        # The rest of the Playbook's customer psychology (until now only the
        # 30-day Calendar read these). Short, top-3 lists so a full profile
        # doesn't drown the prompt.
        psychology = [
            ("What they want", "customer_desires"),
            ("What they fear", "customer_fears"),
            ("What frustrates them", "customer_frustrations"),
            ("What they aspire to", "customer_aspirations"),
            ("Why they hesitate", "customer_hesitations"),
            ("Questions they often ask", "common_questions"),
            ("What makes them buy", "buying_triggers"),
        ]
        psych_lines = []
        for label, key in psychology:
            vals = [str(v).strip() for v in (brand_context.get(key) or []) if str(v).strip()]
            if vals:
                psych_lines.append(f'{label}: {"; ".join(vals[:3])}')
        if psych_lines:
            parts.append(
                '- More about the customer: ' + ' | '.join(psych_lines) + '. '
                'Where it fits the post, speak to one of these directly — use the customer\'s own words '
                'from the questions rather than generic marketing phrasing.'
            )

        for line in brand_brief.audience_adaptation(brand_context):
            parts.append(f"- {line}")

        if brand_context.get("primary_goal"):
            parts.append(
                f'- Brand\'s primary goal: {brand_context["primary_goal"]}. '
                f'Every post should move the reader one step closer to this goal.'
            )

        if brand_context.get("target_platforms"):
            platforms = brand_context["target_platforms"]
            if isinstance(platforms, list) and platforms:
                parts.append(
                    f'- Priority platforms: {", ".join(platforms[:5])}. '
                    f'Keep the tone and format optimized for these specific platforms.'
                )

        if brand_context.get("region"):
            parts.append(
                f'- Market/region: {brand_context["region"]}. '
                f'Use cultural references and examples that resonate specifically in this market.'
            )

        # ── Content strategy ─────────────────────────────────────────────────
        if brand_context.get("content_pillars"):
            pillars = brand_context["content_pillars"]
            if isinstance(pillars, list) and pillars:
                parts.append(
                    f'- Content pillars (priority topics): {", ".join(pillars[:5])}. '
                    f'Anchor the post to the most relevant pillar.'
                )

        if brand_context.get("preferred_formats"):
            formats = brand_context["preferred_formats"]
            if isinstance(formats, list) and formats:
                parts.append(
                    f'- Preferred content formats: {", ".join(formats[:4])}. '
                    f'Structure the post to match one of these formats where appropriate.'
                )

        if brand_context.get("brand_colors"):
            colors = brand_context["brand_colors"]
            if isinstance(colors, list) and colors:
                parts.append(
                    f'- Brand colors: {", ".join(colors)}. '
                    f'Let the tone and energy of the post feel consistent with this visual palette.'
                )

        # ── Happening right now (Business Pulse) ─────────────────────────────
        pulse = brand_brief.pulse(brand_context)
        if pulse:
            parts.append("- Happening right now at this business — use it where it genuinely fits this post, "
                         "quote the real terms and never invent any: " + " ".join(pulse))

        # ── CTAs & links ─────────────────────────────────────────────────────
        # Priority order: override_cta > selected_cta (from image) > cta_styles (all CTAs)
        # This ensures caption CTA matches the image CTA for consistency
        override_cta = brand_context.get("override_cta")
        selected_cta = brand_context.get("selected_cta")  # CTA used in the image

        if override_cta:
            # One-time override CTA (highest priority)
            parts.append(
                f'- Call-to-action: End the post with this exact CTA: "{override_cta}". {_CTA_LAST_LINE}'
            )
        elif selected_cta:
            # Use the SAME CTA that was used in the image (for consistency)
            parts.append(
                f'- Call-to-action: End the post with this exact CTA: "{selected_cta}". {_CTA_LAST_LINE}'
            )
            print(f"✅ Caption will use same CTA as image: '{selected_cta}'")
        elif brand_context.get("cta_styles"):
            # Fallback: if no image CTA was selected, AI can pick from all CTAs
            ctas = brand_context["cta_styles"]
            if isinstance(ctas, list) and ctas:
                parts.append(
                    f'- Preferred CTAs: {", ".join(ctas)}. '
                    f'End the post with the most fitting one. {_CTA_LAST_LINE}'
                )

        if brand_context.get("default_link"):
            parts.append(
                f'- Default link for CTAs: {brand_context["default_link"]}. '
                f'Include it in the CTA if the post calls for a direct link.'
            )

        # ── Competitive context ───────────────────────────────────────────────
        if brand_brief.competitors_line(brand_context):
            parts.append(f"- {brand_brief.competitors_line(brand_context)}")

        # ── Key dates / upcoming events ───────────────────────────────────────
        if brand_context.get("key_dates"):
            parts.append(
                f'- Upcoming key dates/events for this brand: {brand_context["key_dates"]}. '
                f'If any of these are relevant to the post topic, reference them naturally.'
            )

        if len(parts) == 3:
            # No brand instructions, but still check for voice profile
            brand_block = ""
        else:
            brand_block = "\n".join(parts) + "\n\n"

        # ── Voice Profile (Caption Voice System - PRD Section 3 & 6) ──────────
        # TEMPORARILY DISABLED - Testing if this breaks image generation
        # voice_profile = brand_context.get("voice_profile", {})
        # voice_sample_analysis = brand_context.get("voice_sample_analysis", {})
        #
        # if voice_profile or voice_sample_analysis:
        #     voice_instructions = build_voice_profile_instructions(
        #         voice_profile,
        #         voice_sample_analysis,
        #         platform
        #     )
        #     brand_block += voice_instructions

        return brand_block

    @staticmethod
    async def generate_multi_platform_content(
        user_id: str,
        seed_content: str,
        platforms: List[str],
        seed_type: str = "text",
        request_id: Optional[str] = None,
        brand_context: Optional[Dict[str, Any]] = None,
        db: Optional[AsyncIOMotorDatabase] = None,
        reference_image: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generate platform-native content simultaneously for all requested platforms

        Integrates with your existing URI user system and follows established patterns.

        Args:
            user_id: ID of the URI user requesting content
            seed_content: Original content to transform
            platforms: List of platforms to generate content for
            seed_type: Type of seed content (text, url, mention_response, etc.)
            request_id: Optional existing request ID (for regeneration)
            reference_image: Optional base64 data URL for product/context analysis

        Returns:
            Dictionary containing request_id, generated drafts, and status
        """

        # Create or use existing request ID
        if not request_id:
            request_id = str(ObjectId())

        print(f"🤖 Generating content for {len(platforms)} platforms: {platforms}")

        # ── VISION ANALYSIS: If reference_image provided, analyze it first ──────
        enriched_seed_content = seed_content
        if reference_image:
            print(f"🔍 Reference image detected, analyzing product/context...")
            try:
                vision_prompt = f"""Analyze this product image and extract key details to help write engaging social media content.

USER'S REQUEST: {seed_content}

Provide a detailed description including:
1. Product name/type (what is it?)
2. Visual appearance (colors, materials, design style, packaging)
3. Key features or benefits visible in the image
4. Target audience (who would buy this?)
5. Emotional appeal (what feeling does it evoke?)
6. Any text/branding visible on the product

Be specific and descriptive. This analysis will be used to generate authentic product content."""

                vision_request = AIService.build_ai_model(
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": vision_prompt},
                            {"type": "image_url", "image_url": {"url": reference_image}}
                        ]
                    }],
                    temperature=0.5,
                )

                vision_response = await AIService.chat_completion(vision_request)

                if isinstance(vision_response, dict) and "error" in vision_response:
                    print(f"⚠️ Vision analysis failed: {vision_response['error']}")
                else:
                    product_analysis = vision_response.choices[0].message.content.strip()
                    print(f"✅ Product analysis complete: {product_analysis[:150]}...")

                    # Enrich seed_content with product analysis
                    enriched_seed_content = f"""USER REQUEST: {seed_content}

PRODUCT ANALYSIS FROM IMAGE:
{product_analysis}

Create social media content about THIS SPECIFIC PRODUCT based on the image analysis above. Do NOT write about Uri Social or content creation services - write about the PRODUCT shown in the image."""

            except Exception as e:
                print(f"⚠️ Vision analysis error: {e}")
                # Continue with original seed_content if vision fails
        
        # Validate platforms
        supported_platforms = list(ContentGenerationService.PLATFORM_PROMPTS.keys())
        valid_platforms = [p for p in platforms if p in supported_platforms]
        
        if not valid_platforms:
            return UriResponse.error_response(
                f"No supported platforms found. Supported: {supported_platforms}"
            )
        
        # Generate content for each platform concurrently
        generation_tasks = []
        for platform in valid_platforms:
            task = ContentGenerationService._generate_platform_content(
                platform, enriched_seed_content, request_id, user_id, brand_context
            )
            generation_tasks.append(task)
        
        # Wait for all generations to complete
        results = await asyncio.gather(*generation_tasks, return_exceptions=True)
        
        # Process results and prepare response
        drafts = []
        errors = []
        
        for i, result in enumerate(results):
            platform = valid_platforms[i]
            
            if isinstance(result, Exception):
                error_msg = f"Failed to generate {platform} content: {str(result)}"
                print(error_msg)
                errors.append({"platform": platform, "error": error_msg})
                continue
            
            if result and result.get('status'):
                draft_data = result['responseData']
                drafts.append({
                    'id': draft_data['draft_id'],
                    'platform': platform,
                    'content': draft_data['content'],
                    'seed_content': seed_content,
                    'hashtags': draft_data.get('hashtags', []),
                    'word_count': len(draft_data['content'].split()),
                    'ai_metadata': draft_data.get('ai_metadata', {}),
                    'is_twitter_thread': platform == 'twitter' and draft_data.get('is_twitter_thread', False)
                })
            else:
                errors.append({"platform": platform, "error": result.get('responseMessage', 'Unknown error')})
        
        # Determine overall status
        status = 'ready' if len(drafts) > 0 else 'failed'

        generated_at = datetime.utcnow()

        # Persist to DB so the approval workflow can look them up
        if db is not None and drafts:
            try:
                # Save the request record
                # PRD Section 9: Campaign Schema - must track retry_count and image_retry_count
                await db["content_requests"].replace_one(
                    {"id": request_id},
                    {
                        "id": request_id,
                        "user_id": user_id,
                        "seed_content": seed_content,
                        "seed_type": seed_type,
                        "platforms": platforms,
                        "status": status,
                        "retry_count": 0,  # PRD 9: Track full campaign retries
                        "image_retry_count": 0,  # PRD 9: Track image-only retries
                        "text_edit_count": 0,  # PRD 9: Track text rewrites (unlimited, no cost)
                        "credits_used": 1,  # PRD 9: Credits consumed by this campaign
                        "created_at": generated_at,
                        "updated_at": generated_at,
                    },
                    upsert=True,
                )
                # Save each draft
                _brand_id = (brand_context or {}).get("brand_id")
                for draft in drafts:
                    draft_doc = {
                        **draft,
                        "request_id": request_id,
                        "user_id": user_id,
                        "approval_status": "pending",
                        "created_at": generated_at,
                        "updated_at": generated_at,
                    }
                    if _brand_id:
                        draft_doc["brand_id"] = _brand_id
                    await db["content_drafts"].replace_one(
                        {"id": draft["id"]},
                        draft_doc,
                        upsert=True,
                    )
            except Exception as db_err:
                print(f"⚠️ DB persist failed for request_id={request_id}: {db_err}")

        response_data = {
            'request_id': request_id,
            'seed_content': seed_content,
            'seed_type': seed_type,
            'requested_platforms': platforms,
            'successful_platforms': [d['platform'] for d in drafts],
            'drafts': drafts,
            'errors': errors,
            'status': status,
            'generated_at': generated_at.isoformat()
        }

        if status == 'ready':
            print(f"✅ Generated {len(drafts)} drafts successfully")
            return UriResponse.get_single_data_response("content_generation", response_data)
        else:
            return UriResponse.error_response(
                f"Content generation failed for all platforms. Errors: {errors}",
            )
    
    @staticmethod
    async def _generate_platform_content(
        platform: str,
        seed_content: str,
        request_id: str,
        user_id: str,
        brand_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Generate content for a specific platform using your existing AIService
        """

        if platform not in ContentGenerationService.PLATFORM_PROMPTS:
            return UriResponse.error_response(f"Unsupported platform: {platform}")

        # A formal / no-slang platform is written in the main language only — the
        # prompt, the Playbook check and the rewrite all see the same rule.
        brand_context = brand_brief.for_platform(brand_context, platform)
        try:
            # Get platform-specific prompt
            prompt = ContentGenerationService.build_caption_prompt(seed_content, platform, brand_context)
            
            # Use your existing AIService with optimized parameters
            ai_request = AIService.build_ai_model(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,  # Creative but consistent
               # max_tokens=1000,  # Enough for longest content
            )
            
            # Generate content using your existing AI service
            ai_response = await AIService.chat_completion(ai_request)

            # AIService returns a dict with "error" key on quota/rate limit failures
            if isinstance(ai_response, dict) and "error" in ai_response:
                raise Exception(ai_response["error"])

            raw_content = ai_response.choices[0].message.content.strip()
            
            # Post-process content based on platform
            processed_content = ContentGenerationService._post_process_content(raw_content, platform)
            
            # Extract hashtags and clean content
            content, hashtags = ContentGenerationService._extract_and_clean_hashtags(
                processed_content['content'], platform
            )

            # ── Check against the universal rules and the Brand Playbook, rewrite
            # if needed, then enforce what can be guaranteed (PRD Section 7) ────
            custom_banned_words = ContentGenerationService._brand_banned_words(brand_context)
            is_thread = platform in ("twitter", "x")
            # Everything the writer was given — a statistic that isn't in here was invented.
            sources = f"{seed_content}\n{json.dumps(brand_context or {}, default=str, ensure_ascii=False)}"

            def _check(text: str):
                style = CaptionValidatorService.validate_caption(
                    ContentGenerationService._for_validation(text, platform), custom_banned_words)
                if is_thread:
                    # A thread is laid out as tweets, not as one phone-screen caption.
                    kept = [i for i in style["issues"] if i not in ("wall_of_text", "insufficient_line_breaks")]
                    style = {**style, "issues": kept, "passed": not kept}
                return style, brand_brief.audit(text, brand_context, sources=sources)

            validation_result, playbook_issues = _check(content)
            brand_rules = brand_brief.hard_rules(brand_context, captions=True, platform=platform)
            attempt = 0
            # One rewrite for style, as before. A second only if a Playbook rule is still broken.
            while attempt < 2 and (playbook_issues or (attempt == 0 and not validation_result["passed"])):
                attempt += 1
                print(f"[CAPTION] {platform} rewrite {attempt}: style={validation_result['issues'][:4]} "
                      f"playbook={[i['code'] + ':' + i['detail'] for i in playbook_issues]}")
                fix_prompt = CaptionValidatorService.generate_fix_prompt(
                    content, validation_result, playbook_issues=playbook_issues,
                    brand_rules=brand_rules, platform=platform,
                )
                fix_ai_request = AIService.build_ai_model(
                    messages=[{"role": "user", "content": fix_prompt}],
                    temperature=0.7,
                )
                fix_ai_response = await AIService.chat_completion(fix_ai_request)
                if isinstance(fix_ai_response, dict) and "error" in fix_ai_response:
                    break
                fixed_raw = fix_ai_response.choices[0].message.content.strip()
                fixed_processed = ContentGenerationService._post_process_content(fixed_raw, platform)
                fixed_content, fixed_hashtags = ContentGenerationService._extract_and_clean_hashtags(
                    fixed_processed['content'], platform
                )
                if not fixed_content:
                    break
                new_validation, new_playbook = _check(fixed_content)
                # Never trade a rewrite that breaks more of the brand's rules for the original.
                if len(new_playbook) > len(playbook_issues):
                    print(f"[CAPTION] ⚠ rewrite {attempt} for {platform} broke more Playbook rules — kept the previous version")
                    continue
                content, processed_content = fixed_content, fixed_processed
                # The caption is rewritten without its hashtags, so a rewrite that
                # returns none must not wipe the ones already written.
                hashtags = fixed_hashtags or hashtags
                validation_result, playbook_issues = new_validation, new_playbook

            if validation_result["passed"] and not playbook_issues:
                print(f"[CAPTION] ✓ Passed validation for {platform}")
            else:
                print(f"[CAPTION] ⚠ {platform} still has issues: style={validation_result['issues'][:3]} "
                      f"playbook={[i['code'] for i in playbook_issues]}")

            # Guaranteed, whatever the model did: no emojis if the brand said none,
            # the hashtag limit, and the brand's call-to-action as the close.
            content = brand_brief.enforce(content, brand_context, append_cta=not is_thread)
            hashtags = brand_brief.limit_hashtags(hashtags, brand_context)
            if is_thread and processed_content.get('tweets'):
                processed_content['tweets'] = [
                    brand_brief.enforce(t, brand_context, append_cta=False) for t in processed_content['tweets']
                ]
            remaining_issues = [
                f"{i['code']}: {i['detail']}" for i in brand_brief.audit(content, brand_context, sources=sources)
            ]

            # Generate a draft ID
            draft_id = str(ObjectId())
            
            # Prepare AI metadata for tracking
            ai_metadata = {
                'model_used': 'gpt-4o',  # Update this to match your actual model
                'prompt_version': '1.0',
                'generation_time': datetime.utcnow().isoformat(),
                'temperature': 0.7,
                'platform': platform,
                'seed_length': len(seed_content),
                'output_length': len(content),
                'hashtag_count': len(hashtags),
                'nigerian_context': True,  # Flag for Nigerian-optimized content
                'playbook_issues': remaining_issues,  # Playbook rules still broken after rewriting (normally empty)
                'rewrites': attempt,
            }
            
            return UriResponse.get_single_data_response("platform_content", {
                'draft_id': draft_id,
                'request_id': request_id,
                'platform': platform,
                'content': content,
                'original_content': content,  # Store original for edit tracking
                'hashtags': hashtags,
                'ai_metadata': ai_metadata,
                'is_twitter_thread': platform in ('twitter', 'x') and processed_content.get('is_thread', False),
                'tweets': processed_content.get('tweets', []) if platform in ('twitter', 'x') else None
            })
            
        except Exception as e:
            print(f"Error generating {platform} content: {str(e)}")
            return UriResponse.error_response(f"Generation failed: {str(e)}")
    
    @staticmethod
    def _for_validation(text: str, platform: str) -> str:
        """The text the style validator should judge. A thread's "(1/4)" numbering
        is structure, not a "slash construction" or a "parenthetical"."""
        if platform in ("twitter", "x"):
            return re.sub(r'\(\d+/\d+\)', '', text)
        return text

    @staticmethod
    def _post_process_content(raw_content: str, platform: str) -> Dict[str, Any]:
        """
        Post-process AI-generated content based on platform requirements
        """
        
        if platform in ("twitter", "x"):
            # The model often wraps the thread in a ```json fence; without this the
            # fence and brackets became "tweets" of their own (seen on dev).
            raw_content = re.sub(r'^```[a-zA-Z]*\s*|\s*```$', '', raw_content.strip()).strip()
            # Handle X/Twitter thread format
            try:
                # Try to parse as JSON array first
                if raw_content.strip().startswith('['):
                    tweets = json.loads(raw_content)
                    if isinstance(tweets, list):
                        tweets = [str(t).strip() for t in tweets if str(t).strip()]
                        return {
                            'content': '\n\n'.join(tweets),
                            'tweets': tweets,
                            'is_thread': len(tweets) > 1
                        }
            except json.JSONDecodeError:
                pass
            
            # If not JSON, treat as single tweet or split by newlines
            lines = [line.strip() for line in raw_content.split('\n') if line.strip()]
            if len(lines) > 1:
                # Multi-tweet thread
                tweets = []
                for i, line in enumerate(lines[:5]):  # Max 5 tweets
                    if not line.endswith(f'({i+1}/{len(lines)})'):
                        line = f"{line} ({i+1}/{len(lines)})"
                    tweets.append(line[:280])  # Ensure Twitter limit
                
                return {
                    'content': '\n\n'.join(tweets),
                    'tweets': tweets,
                    'is_thread': True
                }
            else:
                # Single tweet
                content = lines[0][:280] if lines else raw_content[:280]
                return {
                    'content': content,
                    'tweets': [content],
                    'is_thread': False
                }
        
        # For other platforms, return as-is
        return {'content': raw_content.strip()}
    
    @staticmethod
    def _extract_and_clean_hashtags(content: str, platform: str) -> Tuple[str, List[str]]:
        """
        Extract hashtags from content and clean up the text
        
        Returns:
            Tuple of (cleaned_content, hashtags_list)
        """
        import re
        
        # Find hashtags
        hashtags = re.findall(r'#\w+', content)
        
        # Remove hashtags from content (we'll add them separately)
        cleaned_content = re.sub(r'#\w+', '', content)

        # Tidy spaces inside each line but KEEP the line breaks. This used to be
        # ' '.join(text.split()), which flattened every caption into one block:
        # the paragraphs the prompts ask for never reached the draft, and the
        # validator's "needs line breaks" check then failed on every caption.
        lines = [re.sub(r'[ \t]+', ' ', line).strip() for line in cleaned_content.replace('\r', '').split('\n')]
        # A line holding only a dot is the old Instagram spacer trick, not content.
        # Also ". . . . ." on one line (seen in a dev preview), with or without spaces.
        lines = ['' if re.fullmatch(r'[.·•\-_~*\s]+', line or ' ') else line for line in lines]
        cleaned_content = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines))
        
        # Platform-specific hashtag limits optimized for Nigerian audience
        limits = {
            'twitter': 3,      # Twitter is character-limited
            'linkedin': 5,     # Professional context, fewer hashtags
            'facebook': 5,     # Community focus, moderate hashtags
            'instagram': 15,   # Visual platform, more hashtags OK
            'tiktok': 10       # Creative platform, good hashtag usage
        }
        
        limit = limits.get(platform, 5)
        limited_hashtags = hashtags[:limit]
        
        # Clean hashtags (remove # symbol for storage)
        clean_hashtags = [tag.replace('#', '') for tag in limited_hashtags]
        
        return cleaned_content.strip(), clean_hashtags
    
    @staticmethod
    async def regenerate_content(
        draft_id: str,
        user_id: str,
        feedback: Optional[str] = None,
        platform: Optional[str] = None,
        original_seed: Optional[str] = None,
        brand_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Regenerate content for a specific draft with optional feedback
        
        This would integrate with your database to fetch original request data
        """
        
        if not platform or not original_seed:
            return UriResponse.error_response("Platform and original seed content required for regeneration")
        
        # Add feedback to the prompt if provided
        enhanced_seed = original_seed
        if feedback:
            enhanced_seed = f"{original_seed}\n\nUser feedback: {feedback}"
        
        # Generate new content
        result = await ContentGenerationService._generate_platform_content(
            platform, enhanced_seed, f"regen_{draft_id}", user_id, brand_context=brand_context
        )
        
        if result.get('status'):
            result['responseData']['is_regenerated'] = True
            result['responseData']['feedback_incorporated'] = bool(feedback)
            result['responseData']['original_draft_id'] = draft_id
        
        return result
    
    @staticmethod
    def get_platform_requirements(platform: str) -> Dict[str, Any]:
        """
        Get content requirements and limits for a specific platform
        Optimized for Nigerian business context
        """
        
        requirements = {
            "linkedin": {
                "max_length": 3000,
                "optimal_length": 200,
                "tone": "Professional B2B, Nigerian business leader",
                "format": "Question-ending, white space, bullet points",
                "hashtag_limit": 5,
                "audience": "Nigerian/African CEOs, entrepreneurs, professionals",
                "context": "Lagos business scene, African market insights"
            },
            "twitter": {
                "max_length": 280,
                "optimal_length": 250,
                "tone": "Punchy, contrarian, tech-savvy Nigerian founder",
                "format": "Thread format for longer content",
                "hashtag_limit": 3,
                "audience": "Nigerian tech community, startup founders",
                "context": "Nigerian fintech, African tech ecosystem"
            },
            "facebook": {
                "max_length": 2000,
                "optimal_length": 150,
                "tone": "Community-focused, warm Nigerian business community",
                "format": "Storytelling, call-to-action",
                "hashtag_limit": 5,
                "audience": "Nigerian SME owners, business community",
                "context": "Local business challenges, community support"
            },
            "instagram": {
                "max_length": 2200,
                "optimal_length": 125,
                "tone": "Visual, inspirational Nigerian entrepreneur",
                "format": "Visual storytelling, line breaks",
                "hashtag_limit": 15,
                "audience": "Young Nigerian entrepreneurs, creatives",
                "context": "Behind-the-scenes business building in Nigeria"
            },
            "x": {
                "max_length": 280,
                "optimal_length": 250,
                "tone": "Punchy, contrarian, tech-savvy Nigerian founder",
                "format": "Thread format (3-5 tweets), each max 280 chars",
                "hashtag_limit": 2,
                "audience": "Nigerian tech community, startup founders",
                "context": "Nigerian fintech, African tech ecosystem"
            },
            "linkedin": {
                "max_length": 3000,
                "optimal_length": 200,
                "tone": "Professional B2B, Nigerian business leader",
                "format": "Question-ending, white space, bullet points",
                "hashtag_limit": 5,
                "audience": "Nigerian/African CEOs, entrepreneurs, professionals",
                "context": "Lagos business scene, African market insights"
            },
            "tiktok": {
                "max_length": 150,
                "optimal_length": 60,
                "tone": "Ultra-casual, meme-friendly, Gen Z Nigerian founder",
                "format": "1-3 lines, caption supports the image, doesn't replace it",
                "hashtag_limit": 5,
                "audience": "Young Nigerian entrepreneurs, creatives, Gen Z",
                "context": "Relatable business moments, behind-the-scenes"
            },
        }

        # Allow 'twitter' as alias for 'x'
        if platform == "twitter":
            platform = "x"

        return requirements.get(platform, {})
    
    @staticmethod
    async def analyze_content_performance(platform: str, content: str) -> Dict[str, Any]:
        """
        Use AI to analyze content and predict performance
        Could integrate with your existing social listening data
        """
        
        analysis_prompt = f"""
        Analyze this {platform} content for a Nigerian business audience:
        
        "{content}"
        
        Provide analysis on:
        1. Engagement prediction (high/medium/low)
        2. Target audience fit
        3. Nigerian market relevance
        4. Suggested improvements
        
        Return as JSON format.
        """
        
        try:
            ai_request = AIService.build_ai_model(
                messages=[{"role": "user", "content": analysis_prompt}],
                temperature=0.3
            )
            
            ai_response = await AIService.chat_completion(ai_request)
            analysis_text = ai_response.choices[0].message.content.strip()
            
            return UriResponse.get_single_data_response("content_analysis", {
                "platform": platform,
                "analysis": analysis_text,
                "analyzed_at": datetime.utcnow().isoformat()
            })
            
        except Exception as e:
            return UriResponse.error_response(f"Content analysis failed: {str(e)}")
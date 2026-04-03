# app/agents/social_media_manager/services/content_generation_service.py

import asyncio
import json
from typing import List, Dict, Optional, Any, Tuple
from datetime import datetime
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.AIService import AIService
from app.domain.responses.uri_response import UriResponse


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
        """
    }
    
    @staticmethod
    def _build_brand_block(brand_context: Optional[Dict[str, Any]], platform: str = "") -> str:
        """
        Build directive brand instructions from every onboarding field.
        Injected at the top of every generation prompt.
        """
        if not brand_context:
            return ""

        parts = ["BRAND INSTRUCTIONS — you are writing on behalf of this specific brand. Apply every rule below:"]

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

        if brand_context.get("primary_goal"):
            parts.append(
                f'- Brand\'s primary goal: {brand_context["primary_goal"]}. '
                f'Every post should move the reader one step closer to this goal.'
            )

        if brand_context.get("region"):
            parts.append(
                f'- Market/region: {brand_context["region"]}. '
                f'Use cultural references and examples that resonate specifically in this market.'
            )

        if brand_context.get("languages"):
            langs = brand_context["languages"]
            if isinstance(langs, list) and langs and langs != ["English"]:
                parts.append(
                    f'- Write in: {", ".join(langs)}. '
                    f'Default to English but naturally weave in local expressions where appropriate.'
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

        # ── Guardrails ───────────────────────────────────────────────────────
        guardrails = brand_context.get("guardrails")
        if guardrails and isinstance(guardrails, dict):
            if guardrails.get("avoid_topics"):
                parts.append(f'- NEVER mention or reference: {guardrails["avoid_topics"]}.')
            if guardrails.get("banned_words"):
                parts.append(f'- NEVER use these words or phrases: {guardrails["banned_words"]}.')
            emoji_rule = guardrails.get("emoji_usage")
            if emoji_rule == "no":
                parts.append('- Use NO emojis under any circumstances.')
            elif emoji_rule == "some":
                parts.append('- Use emojis very sparingly — maximum 1-2 only if they add genuine meaning.')
            max_hash = guardrails.get("max_hashtags")
            if max_hash and max_hash not in ("No limit", ""):
                parts.append(f'- Use a maximum of {max_hash} hashtags.')
            if guardrails.get("compliance_notes"):
                parts.append(f'- Compliance requirement: {guardrails["compliance_notes"]}.')

        # ── CTAs & links ─────────────────────────────────────────────────────
        if brand_context.get("cta_styles"):
            ctas = brand_context["cta_styles"]
            if isinstance(ctas, list) and ctas:
                parts.append(
                    f'- Preferred CTAs: {", ".join(ctas)}. '
                    f'End the post with the most fitting one.'
                )

        if brand_context.get("default_link"):
            parts.append(
                f'- Default link for CTAs: {brand_context["default_link"]}. '
                f'Include it in the CTA if the post calls for a direct link.'
            )

        # ── Competitive context ───────────────────────────────────────────────
        if brand_context.get("competitor_handles"):
            handles = brand_context["competitor_handles"]
            if isinstance(handles, list) and handles:
                parts.append(
                    f'- Competitor accounts: {", ".join(handles[:5])}. '
                    f'Be aware of this competitive landscape — differentiate the brand\'s voice and value clearly.'
                )

        # ── Key dates / upcoming events ───────────────────────────────────────
        if brand_context.get("key_dates"):
            parts.append(
                f'- Upcoming key dates/events for this brand: {brand_context["key_dates"]}. '
                f'If any of these are relevant to the post topic, reference them naturally.'
            )

        if len(parts) == 1:
            return ""

        return "\n".join(parts) + "\n\n"

    @staticmethod
    async def generate_multi_platform_content(
        user_id: str,
        seed_content: str,
        platforms: List[str],
        seed_type: str = "text",
        request_id: Optional[str] = None,
        brand_context: Optional[Dict[str, Any]] = None,
        db: Optional[AsyncIOMotorDatabase] = None,
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
        
        Returns:
            Dictionary containing request_id, generated drafts, and status
        """
        
        # Create or use existing request ID
        if not request_id:
            request_id = str(ObjectId())
        
        print(f"🤖 Generating content for {len(platforms)} platforms: {platforms}")
        
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
                platform, seed_content, request_id, user_id, brand_context
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
                await db["content_requests"].replace_one(
                    {"id": request_id},
                    {
                        "id": request_id,
                        "user_id": user_id,
                        "seed_content": seed_content,
                        "seed_type": seed_type,
                        "platforms": platforms,
                        "status": status,
                        "created_at": generated_at,
                        "updated_at": generated_at,
                    },
                    upsert=True,
                )
                # Save each draft
                for draft in drafts:
                    await db["content_drafts"].replace_one(
                        {"id": draft["id"]},
                        {
                            **draft,
                            "request_id": request_id,
                            "user_id": user_id,
                            "approval_status": "pending",
                            "created_at": generated_at,
                            "updated_at": generated_at,
                        },
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

        try:
            # Get platform-specific prompt
            prompt_template = ContentGenerationService.PLATFORM_PROMPTS[platform]
            brand_block = ContentGenerationService._build_brand_block(brand_context, platform=platform)
            platform_prompt = prompt_template.format(seed_content=seed_content)
            # Brand instructions go first so they govern everything that follows
            prompt = brand_block + platform_prompt if brand_block else platform_prompt
            
            # Use your existing AIService with optimized parameters
            ai_request = AIService.build_ai_model(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,  # Creative but consistent
               # max_tokens=1000,  # Enough for longest content
            )
            
            # Generate content using your existing AI service
            ai_response = await AIService.chat_completion(ai_request)
            raw_content = ai_response.choices[0].message.content.strip()
            
            # Post-process content based on platform
            processed_content = ContentGenerationService._post_process_content(raw_content, platform)
            
            # Extract hashtags and clean content
            content, hashtags = ContentGenerationService._extract_and_clean_hashtags(
                processed_content['content'], platform
            )
            
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
                'nigerian_context': True  # Flag for Nigerian-optimized content
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
    def _post_process_content(raw_content: str, platform: str) -> Dict[str, Any]:
        """
        Post-process AI-generated content based on platform requirements
        """
        
        if platform in ("twitter", "x"):
            # Handle X/Twitter thread format
            try:
                # Try to parse as JSON array first
                if raw_content.strip().startswith('['):
                    tweets = json.loads(raw_content)
                    if isinstance(tweets, list):
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
        
        # Clean up extra whitespace
        cleaned_content = ' '.join(cleaned_content.split())
        
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
        original_seed: Optional[str] = None
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
            platform, enhanced_seed, f"regen_{draft_id}", user_id
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
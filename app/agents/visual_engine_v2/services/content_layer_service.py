"""
Content Layer Service - Visual Engine V2
AI text generation wrapper for Layer 1

PRD Section 7 / Section 3: Content Layer
- Generates headline, subhead, promo (sale/product posts only), CTA
- Can reuse existing V1 content generation logic
- Wraps output in LayerData format
"""

from typing import Dict, List, Optional
from datetime import datetime
import json
from openai import AsyncOpenAI

from app.agents.visual_engine_v2.models.visual_engine_models import LayerData
from app.agents.visual_engine_v2.config.vendor_config import VendorConfig


class ContentLayerService:
    """
    Layer 1: Content generation (AI text).

    Generates structured text for template fill:
    - Headline (short, attention-grabbing)
    - Subtext (supporting detail)
    - CTA (call to action)
    """

    def __init__(self, openai_client: AsyncOpenAI):
        self.openai_client = openai_client
        self.vendor_config = VendorConfig()

    async def generate_content_plan(
        self,
        seed_content: str,
        brand_context: Dict,
        post_intent: str = "announcement",
        platform: str = "instagram"
    ) -> LayerData:
        """
        Generate Layer 1: Content (headline, subtext, CTA).

        Args:
            seed_content: User's content idea/brief
            brand_context: Brand voice, tone, CTA options
            post_intent: "sale", "product", "announcement", "educational", "testimonial"
            platform: Target platform (affects text length)

        Returns:
            LayerData with content structure
        """
        print(f"📝 Generating Layer 1: Content (intent={post_intent}, platform={platform})")

        # Build content generation prompt
        prompt = self._build_content_prompt(seed_content, brand_context, post_intent, platform)

        # Call GPT-4 for structured content
        response = await self.openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": "You are a social media copywriter. Generate structured content for template-based designs."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            temperature=0.8,
            max_tokens=500
        )

        content_text = response.choices[0].message.content.strip()

        # Parse structured output
        content_data = self._parse_content_structure(content_text)

        # Add CTA from brand context if not generated
        if not content_data.get("cta"):
            content_data["cta"] = self._select_cta(brand_context)

        print(f"✓ Content Layer: headline='{content_data['headline'][:40]}...', cta='{content_data['cta']}'")

        return LayerData(
            layer_type="content",
            data=content_data,
            metadata={
                "post_intent": post_intent,
                "platform": platform,
                "model": "gpt-4o",
                "cost": self.vendor_config.cost_model.content_generation_cost,
                "generated_at": datetime.utcnow().isoformat()
            }
        )

    def _build_content_prompt(
        self,
        seed_content: str,
        brand_context: Dict,
        post_intent: str,
        platform: str
    ) -> str:
        """
        Build AI prompt for content generation.
        """
        brand_name = brand_context.get("brand_name", "the brand")
        # brand_voice is already a fully-derived string (from personality_quiz +
        # derived_voice via BrandProfileService.to_brand_context) — the caller
        # must pass a to_brand_context() result here, not a raw brand profile
        # document, which has no top-level "brand_voice" field.
        voice_tone = brand_context.get("brand_voice") or "professional"
        industry = brand_context.get("industry", "")

        # Platform-specific length constraints
        length_guides = {
            "instagram": "Headline: 5-8 words. Subtext: 10-15 words.",
            "facebook": "Headline: 6-10 words. Subtext: 15-20 words.",
            "twitter": "Headline: 10-15 words. Subtext: 5-10 words.",
            "linkedin": "Headline: 8-12 words. Subtext: 15-25 words."
        }
        length_guide = length_guides.get(platform, length_guides["instagram"])

        # PRD Section 6: promo (e.g. "CODE: WEEKEND20") only makes sense for
        # sale/product posts — never invented for announcement/educational/testimonial.
        wants_promo = post_intent in ("sale", "product")
        promo_line = "\nPROMO: [A short promo code or offer line, e.g. \"CODE: WEEKEND20\" — omit the brackets]" if wants_promo else ""
        promo_requirement = "\n- PROMO must be a real, usable code/offer string, not a placeholder" if wants_promo else ""

        prompt = f"""Generate structured social media content for {brand_name} ({industry}).

Content idea: {seed_content}

Post intent: {post_intent}
Brand voice: {voice_tone}
Platform: {platform}

Generate the following components in this EXACT format:

HEADLINE: [Short, attention-grabbing opener - {length_guide.split('.')[0]}]
SUBTEXT: [Supporting detail or value prop - {length_guide.split('.')[1]}]{promo_line}
CTA: [Clear call to action - 2-4 words]

Requirements:
- Headline should hook attention immediately
- Subtext should reinforce the headline with specifics
- CTA should be action-oriented{promo_requirement}
- Match the brand voice: {voice_tone}
- Sound human, not AI-generated
- No hashtags, no emojis (those come later)

Output ONLY the lines above, in order. No explanations."""

        return prompt

    def _parse_content_structure(self, content_text: str) -> Dict:
        """
        Parse GPT output into structured content.

        Expected format:
        HEADLINE: ...
        SUBTEXT: ...
        PROMO: ... (sale/product posts only)
        CTA: ...
        """
        lines = content_text.strip().split("\n")

        content_data = {
            "headline": "",
            "subtext": "",
            "promo": "",
            "cta": ""
        }

        for line in lines:
            line = line.strip()
            if line.startswith("HEADLINE:"):
                content_data["headline"] = line.replace("HEADLINE:", "").strip()
            elif line.startswith("SUBTEXT:"):
                content_data["subtext"] = line.replace("SUBTEXT:", "").strip()
            elif line.startswith("PROMO:"):
                content_data["promo"] = line.replace("PROMO:", "").strip()
            elif line.startswith("CTA:"):
                content_data["cta"] = line.replace("CTA:", "").strip()

        # Fallback: if parsing fails entirely, use the first non-empty lines in order
        if not content_data["headline"]:
            non_empty = [l.strip() for l in lines if l.strip()]
            content_data["headline"] = non_empty[0] if len(non_empty) > 0 else "Check this out"
            content_data["subtext"] = non_empty[1] if len(non_empty) > 1 else ""
            content_data["cta"] = non_empty[2] if len(non_empty) > 2 else "Learn more"

        return content_data

    async def generate_carousel_content_plan(
        self,
        seed_content: str,
        brand_context: Dict,
        carousel_count: int,
        post_intent: str = "carousel",
        platform: str = "instagram"
    ) -> LayerData:
        """
        Generate carousel Layer 1: the whole slide-by-slide narrative arc.

        PRD Section 9.1: "one AI call plans the full narrative arc across
        slides — slide 1 hook, slides 2-N body/value, final slide CTA —
        returning a structured per-slide array (headline, body, image brief
        per slide). One call, not one per slide, to keep cost down."

        This is deliberately a distinct method from generate_content_plan()
        (single post) rather than that method looping N times — a real
        narrative arc needs the whole carousel in view in one completion,
        not N independent single-post generations that don't know about
        each other.
        """
        print(f"📝 Generating Layer 1: Carousel content ({carousel_count} slides, intent={post_intent})")

        prompt = self._build_carousel_content_prompt(seed_content, brand_context, carousel_count, post_intent, platform)

        response = await self.openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": "You are a social media copywriter planning a multi-slide carousel narrative arc. Respond only with valid JSON."
                },
                {"role": "user", "content": prompt}
            ],
            temperature=0.8,
            max_tokens=1200,
            response_format={"type": "json_object"}
        )

        raw = response.choices[0].message.content.strip()
        slides = self._parse_carousel_structure(raw, carousel_count, seed_content)

        # Last slide's CTA falls back to the brand's own CTA rotation if the
        # model left it blank, same source single-post generation uses.
        if slides and not slides[-1].get("cta"):
            slides[-1]["cta"] = self._select_cta(brand_context)

        print(f"✓ Carousel content Layer: {len(slides)} slides planned")

        return LayerData(
            layer_type="content",
            data={"slides": slides, "slide_count": len(slides)},
            metadata={
                "post_intent": post_intent,
                "platform": platform,
                "model": "gpt-4o",
                "cost": self.vendor_config.cost_model.content_generation_cost,
                "generated_at": datetime.utcnow().isoformat()
            }
        )

    def _build_carousel_content_prompt(
        self,
        seed_content: str,
        brand_context: Dict,
        carousel_count: int,
        post_intent: str,
        platform: str
    ) -> str:
        brand_name = brand_context.get("brand_name", "the brand")
        # brand_voice is the fully-derived string from to_brand_context() —
        # caller must pass that, not a raw brand profile document.
        voice_tone = brand_context.get("brand_voice") or "professional"
        industry = brand_context.get("industry", "")

        wants_promo = post_intent in ("sale", "product")
        promo_instruction = (
            '\n- "promo": a short promo code/offer string on the CTA (last) slide only, empty ("") elsewhere'
            if wants_promo else ""
        )

        return f"""Plan a {carousel_count}-slide {platform} carousel for {brand_name} ({industry}).

Content idea: {seed_content}
Brand voice: {voice_tone}

Structure the narrative arc across exactly {carousel_count} slides:
- Slide 1: the hook — grabs attention, states the topic, makes someone want to swipe.
- Middle slides: body/value — one clear point per slide, building the story slide by slide.
- Last slide: the close — a clear call to action.

For EACH slide, provide:
- "headline": short punchy text for that slide (3-8 words)
- "subtext": one supporting line (0-15 words; can be empty on hook/CTA slides)
- "cta": leave empty ("") on every slide except the LAST, where it must be a real 2-4 word call to action{promo_instruction}
- "image_brief": a specific, concrete visual description for an image generator to create a background for THIS slide — each slide needs its OWN distinct scene/composition that fits this slide's point, not the same image description repeated. Do not mention text, logos, or brand names in the brief — imagery only.

Respond with JSON only, in this exact shape:
{{"slides": [{{"headline": "...", "subtext": "...", "cta": "...", "promo": "...", "image_brief": "..."}}, ...]}}

Exactly {carousel_count} entries in the "slides" array, in order."""

    def _parse_carousel_structure(self, raw_json: str, carousel_count: int, seed_content: str) -> List[Dict]:
        """
        Parse the carousel JSON completion into a clean per-slide list. Never
        raises — a parsing miss degrades to a flat plan repeated across
        slides rather than failing the whole carousel job (PRD Section 12
        guiding principle: degrade, don't drop).
        """
        try:
            parsed = json.loads(raw_json)
            raw_slides = parsed.get("slides") or []
            cleaned = []
            for s in raw_slides:
                if not isinstance(s, dict):
                    continue
                cleaned.append({
                    "headline": str(s.get("headline", "")).strip(),
                    "subtext": str(s.get("subtext", "")).strip(),
                    "promo": str(s.get("promo", "")).strip(),
                    "cta": str(s.get("cta", "")).strip(),
                    "image_brief": str(s.get("image_brief", "")).strip() or seed_content,
                })
            if cleaned:
                while len(cleaned) < carousel_count:
                    cleaned.append(dict(cleaned[-1]))
                return cleaned[:carousel_count]
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

        print("⚠️ Carousel content JSON parse failed, falling back to a flat plan repeated across slides")
        return [
            {"headline": seed_content[:60], "subtext": "", "promo": "", "cta": "", "image_brief": seed_content}
            for _ in range(carousel_count)
        ]

    def _select_cta(self, brand_context: Dict) -> str:
        """
        Select CTA from brand profile with round-robin rotation.
        """
        cta_styles = brand_context.get("cta_styles", [])

        if not cta_styles:
            return brand_context.get("default_link", "Learn more")

        # Round-robin rotation
        rotation_index = brand_context.get("cta_rotation_index", 0)
        if rotation_index >= len(cta_styles):
            rotation_index = 0

        cta = cta_styles[rotation_index]

        # Update rotation index (caller should persist this)
        brand_context["cta_rotation_index"] = (rotation_index + 1) % len(cta_styles)

        return cta

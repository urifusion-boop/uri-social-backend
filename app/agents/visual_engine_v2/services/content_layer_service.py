"""
Content Layer Service - Visual Engine V2
AI text generation wrapper for Layer 1

PRD Section 7: Content Layer
- Generates headline, subtext, CTA
- Can reuse existing V1 content generation logic
- Wraps output in LayerData format
"""

from typing import Dict, Optional
from datetime import datetime
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
        voice_tone = brand_context.get("voice_profile", {}).get("tone", ["professional"])
        industry = brand_context.get("industry", "")

        # Platform-specific length constraints
        length_guides = {
            "instagram": "Headline: 5-8 words. Subtext: 10-15 words.",
            "facebook": "Headline: 6-10 words. Subtext: 15-20 words.",
            "twitter": "Headline: 10-15 words. Subtext: 5-10 words.",
            "linkedin": "Headline: 8-12 words. Subtext: 15-25 words."
        }
        length_guide = length_guides.get(platform, length_guides["instagram"])

        prompt = f"""Generate structured social media content for {brand_name} ({industry}).

Content idea: {seed_content}

Post intent: {post_intent}
Brand voice: {', '.join(voice_tone)}
Platform: {platform}

Generate THREE components in this EXACT format:

HEADLINE: [Short, attention-grabbing opener - {length_guide.split('.')[0]}]
SUBTEXT: [Supporting detail or value prop - {length_guide.split('.')[1]}]
CTA: [Clear call to action - 2-4 words]

Requirements:
- Headline should hook attention immediately
- Subtext should reinforce the headline with specifics
- CTA should be action-oriented
- Match the brand voice: {', '.join(voice_tone)}
- Sound human, not AI-generated
- No hashtags, no emojis (those come later)

Output ONLY the three lines above. No explanations."""

        return prompt

    def _parse_content_structure(self, content_text: str) -> Dict:
        """
        Parse GPT output into structured content.

        Expected format:
        HEADLINE: ...
        SUBTEXT: ...
        CTA: ...
        """
        lines = content_text.strip().split("\n")

        content_data = {
            "headline": "",
            "subtext": "",
            "cta": ""
        }

        for line in lines:
            line = line.strip()
            if line.startswith("HEADLINE:"):
                content_data["headline"] = line.replace("HEADLINE:", "").strip()
            elif line.startswith("SUBTEXT:"):
                content_data["subtext"] = line.replace("SUBTEXT:", "").strip()
            elif line.startswith("CTA:"):
                content_data["cta"] = line.replace("CTA:", "").strip()

        # Fallback: if parsing fails, use first 3 lines
        if not content_data["headline"]:
            content_data["headline"] = lines[0] if len(lines) > 0 else "Check this out"
            content_data["subtext"] = lines[1] if len(lines) > 1 else ""
            content_data["cta"] = lines[2] if len(lines) > 2 else "Learn more"

        return content_data

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

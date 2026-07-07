# app/agents/social_media_manager/services/caption_angle_service.py

from typing import Optional


class CaptionAngleService:
    """
    Suggest caption writing angles based on content type, brand voice, and audience.

    PRD Requirement: Weekly Calendar Output - "Suggested Caption Direction"
    "Provide guidance on how to write the caption to match brand personality
    and maximize engagement."
    """

    # Caption structure templates by content type
    CAPTION_STRUCTURES = {
        "educational": {
            "opening": "Start with a question or bold statement that identifies the problem",
            "body": "Share 3-5 actionable insights or tips",
            "closing": "End with a clear takeaway or call-to-action",
            "example": "Hook: 'Still struggling with low engagement?' → Tips → CTA: 'Save this for later'",
        },
        "promotional": {
            "opening": "Lead with the value proposition or offer",
            "body": "Highlight key benefits and create urgency",
            "closing": "Strong call-to-action with time limit or scarcity",
            "example": "Hook: '50% off ends tonight!' → Benefits → CTA: 'Shop now before it's gone'",
        },
        "engagement": {
            "opening": "Ask a thought-provoking or relatable question",
            "body": "Keep it conversational and invite discussion",
            "closing": "Direct question or prompt for comments",
            "example": "Hook: 'Hot take...' → Opinion → CTA: 'Agree or disagree? Comment below'",
        },
        "relatable": {
            "opening": "Start with a shared experience or pain point",
            "body": "Build connection through storytelling or humor",
            "closing": "Tag prompt or engagement question",
            "example": "Hook: 'When you realize it's Monday again...' → Story → CTA: 'Tag someone who relates'",
        },
        "behind_the_scenes": {
            "opening": "Give context about what you're showing",
            "body": "Share the story or process transparently",
            "closing": "Invite audience into your journey",
            "example": "Hook: 'Here's what went into creating...' → Story → CTA: 'Want to see more?'",
        },
    }

    # Voice-specific writing tips
    VOICE_STYLES = {
        "gen_z": {
            "language": "Use Gen Z slang naturally (no cap, fr, lowkey, highkey, iykyk)",
            "tone": "Casual, unfiltered, conversational like texting a friend",
            "structure": "Short sentences. Fragments are fine. Keep it punchy.",
            "avoid": "Corporate jargon, overly formal language, long paragraphs",
        },
        "millennial": {
            "language": "Pop culture references, self-deprecating humor, emoji-friendly",
            "tone": "Authentic, slightly wordy but relatable",
            "structure": "Mix of short and medium sentences, use emojis strategically",
            "avoid": "Being too corporate or too trendy",
        },
        "professional": {
            "language": "Clear, authoritative, industry-appropriate terminology",
            "tone": "Expert but approachable, data-driven",
            "structure": "Well-structured paragraphs, professional formatting",
            "avoid": "Slang, excessive emojis, overly casual language",
        },
        "casual": {
            "language": "Conversational, friendly, accessible",
            "tone": "Warm and approachable like talking to a friend",
            "structure": "Natural flow, moderate length",
            "avoid": "Stiff corporate speak, jargon",
        },
        "witty": {
            "language": "Clever wordplay, humor, unexpected angles",
            "tone": "Playful and entertaining, smart without being pretentious",
            "structure": "Lead with the hook, build to punchline or insight",
            "avoid": "Being too serious or preachy",
        },
    }

    @staticmethod
    def get_caption_angle(
        content_type: str,
        topic: str,
        brand_voice: str,
        audience_age: str = ""
    ) -> str:
        """
        Returns caption writing direction tailored to content and brand.

        Args:
            content_type: educational | promotional | engagement | relatable | behind_the_scenes
            topic: The post subject/title
            brand_voice: Brand's voice description (e.g., "witty, casual, Gen Z")
            audience_age: Target audience age range (e.g., "Gen Z (18-24)")

        Returns:
            Caption writing guidance string

        Example:
            "Open with a relatable frustration ('Tired of manual data entry?'),
            then transition to your solution. Use conversational Gen Z language -
            keep it casual and punchy with slang like 'no cap' or 'fr'."
        """
        # Normalize inputs
        content_type = content_type.lower().replace(" ", "_")
        voice_lower = brand_voice.lower() if brand_voice else ""
        audience_lower = audience_age.lower() if audience_age else ""

        # Determine voice category
        voice_category = CaptionAngleService._categorize_voice(voice_lower, audience_lower)

        # Get base structure for content type
        structure = CaptionAngleService.CAPTION_STRUCTURES.get(
            content_type,
            CaptionAngleService.CAPTION_STRUCTURES["educational"]
        )

        # Get voice-specific guidance
        voice_guide = CaptionAngleService.VOICE_STYLES.get(
            voice_category,
            CaptionAngleService.VOICE_STYLES["casual"]
        )

        # Build comprehensive caption angle
        angle_parts = []

        # 1. Opening guidance
        angle_parts.append(f"{structure['opening']}.")

        # 2. Voice-specific language tips
        if voice_guide["language"]:
            angle_parts.append(f"{voice_guide['language']}.")

        # 3. Tone guidance
        if voice_guide["tone"]:
            angle_parts.append(f"Tone: {voice_guide['tone']}.")

        # 4. Structure tips
        if voice_guide["structure"]:
            angle_parts.append(f"{voice_guide['structure']}.")

        # 5. Example flow
        if structure["example"]:
            angle_parts.append(f"Example flow: {structure['example']}")

        return " ".join(angle_parts)

    @staticmethod
    def _categorize_voice(voice: str, audience: str) -> str:
        """
        Categorize brand voice into predefined voice styles.
        """
        # Check audience age first (strongest signal)
        if "gen z" in audience or "18-24" in audience or "18-25" in audience:
            return "gen_z"

        if "millennial" in audience or "25-40" in audience or "millennials" in audience:
            return "millennial"

        # Check voice descriptors
        if "gen z" in voice or "gen-z" in voice:
            return "gen_z"

        if "witty" in voice or "playful" in voice or "humorous" in voice or "funny" in voice:
            return "witty"

        if "professional" in voice or "authoritative" in voice or "expert" in voice or "formal" in voice:
            return "professional"

        if "casual" in voice or "friendly" in voice or "conversational" in voice or "warm" in voice:
            return "casual"

        # Default
        return "casual"

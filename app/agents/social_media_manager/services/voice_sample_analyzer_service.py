"""
Voice Sample Analyzer Service
URI Social - Caption Voice System (PRD Section 6)

Analyzes real captions written by the brand to extract concrete writing patterns.
This is more powerful than stated preferences - real examples beat preferences every time.
"""

import json
from typing import List, Dict, Any
from openai import AsyncOpenAI

from app.core.config import settings

_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


class VoiceSampleAnalyzerService:
    """
    Analyzes sample captions to extract voice patterns.
    PRD Section 6: Voice Sample Analysis
    """

    @staticmethod
    async def analyze_voice_samples(sample_captions: List[str]) -> Dict[str, Any]:
        """
        Analyze real captions written by a brand to extract writing patterns.

        Args:
            sample_captions: List of 1-3 real captions the user wrote

        Returns:
            Dict with extracted patterns (see PRD Section 6.1 for schema)
        """
        if not sample_captions or len(sample_captions) == 0:
            return {}

        analysis_prompt = """Analyze these real captions written by a brand.
Extract the following and return as JSON only:

{
  "avg_sentence_length": (number of words per sentence),
  "avg_caption_length": (number of lines),
  "uses_emoji": true/false,
  "emoji_frequency": "none|light|moderate|heavy",
  "emoji_types_used": ["fire", "heart", "pointing_down", ...],
  "uses_pidgin": true/false,
  "pidgin_level": "none|light|heavy",
  "common_phrases": ["list of repeated phrases or expressions"],
  "vocabulary_style": "simple|conversational|industry_jargon|mixed",
  "typical_hooks": ["how they start captions"],
  "typical_closers": ["how they end captions"],
  "uses_hashtags": true/false,
  "hashtag_count_avg": (number),
  "uses_line_breaks": true/false,
  "tone": "serious|warm|playful|bold|sarcastic|motivational",
  "punctuation_habits": "formal|casual|minimal",
  "notable_patterns": ["any other distinctive writing habits"]
}

Be specific and extract ACTUAL patterns from the captions, not generic descriptions."""

        try:
            response = await _client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": analysis_prompt},
                    {"role": "user", "content": "\n---\n".join(sample_captions)},
                ],
                max_tokens=500,
                temperature=0,
                response_format={"type": "json_object"},
            )

            content = response.choices[0].message.content or "{}"
            analysis = json.loads(content)

            print(f"📊 Voice sample analysis complete: tone={analysis.get('tone')}, emoji={analysis.get('emoji_frequency')}")
            return analysis

        except Exception as e:
            print(f"⚠️ Voice sample analysis failed: {e}")
            return {}

    @staticmethod
    def merge_analysis_with_profile(
        voice_profile: Dict[str, Any],
        analysis: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Merge voice sample analysis with stated preferences.
        When they conflict, the analysis wins (real behavior > stated preferences).

        Args:
            voice_profile: The user's stated preferences
            analysis: The extracted patterns from their samples

        Returns:
            Updated voice_profile with analysis overrides
        """
        if not analysis:
            return voice_profile

        # Emoji usage: analysis overrides stated preference
        if "emoji_frequency" in analysis:
            emoji_map = {
                "none": "none",
                "light": "light",
                "moderate": "moderate",
                "heavy": "heavy",
            }
            voice_profile["emoji_usage"] = emoji_map.get(analysis["emoji_frequency"], voice_profile.get("emoji_usage", "light"))

        # Slang/Pidgin level: analysis overrides
        if "pidgin_level" in analysis:
            pidgin_map = {
                "none": "pure_english",
                "light": "light_pidgin",
                "heavy": "heavy_pidgin",
            }
            voice_profile["slang_level"] = pidgin_map.get(analysis["pidgin_level"], voice_profile.get("slang_level", "pure_english"))

        # Caption length: derive from analysis
        if "avg_caption_length" in analysis:
            lines = analysis["avg_caption_length"]
            if lines <= 2:
                voice_profile["caption_length"] = "one_liner"
            elif lines <= 5:
                voice_profile["caption_length"] = "short"
            elif lines <= 8:
                voice_profile["caption_length"] = "medium"
            else:
                voice_profile["caption_length"] = "storytelling"

        # Hook style: derive from typical_hooks
        if "typical_hooks" in analysis and len(analysis["typical_hooks"]) > 0:
            first_hook = analysis["typical_hooks"][0].lower()
            if "?" in first_hook:
                voice_profile["hook_style"] = "question"
            elif any(word in first_hook for word in ["new", "just", "now", "today"]):
                voice_profile["hook_style"] = "bold_statement"
            elif any(word in first_hook for word in ["you", "your", "ever"]):
                voice_profile["hook_style"] = "relatable_observation"

        # Sentence style: derive from avg_sentence_length
        if "avg_sentence_length" in analysis:
            avg_words = analysis["avg_sentence_length"]
            if avg_words < 8:
                voice_profile["sentence_style"] = "short_punchy"
            elif avg_words < 15:
                voice_profile["sentence_style"] = "mixed_rhythm"
            else:
                voice_profile["sentence_style"] = "long_flowing"

        # Formality: derive from tone and punctuation
        if "tone" in analysis and "punctuation_habits" in analysis:
            tone = analysis["tone"]
            punct = analysis["punctuation_habits"]

            if tone in ["serious", "professional"] or punct == "formal":
                voice_profile["formality"] = "formal"
            elif tone in ["warm", "motivational"]:
                voice_profile["formality"] = "semi-formal"
            elif tone in ["playful", "sarcastic", "bold"]:
                voice_profile["formality"] = "casual"
            else:
                voice_profile["formality"] = "very-casual"

        print(f"✅ Merged analysis with profile: formality={voice_profile.get('formality')}, slang={voice_profile.get('slang_level')}")
        return voice_profile

    @staticmethod
    def build_voice_instructions_from_analysis(analysis: Dict[str, Any]) -> str:
        """
        Build natural language instructions from analysis for the AI prompt.

        Args:
            analysis: The voice sample analysis result

        Returns:
            Human-readable voice instructions
        """
        if not analysis:
            return ""

        instructions = []

        # Sentence structure
        if "avg_sentence_length" in analysis:
            avg = analysis["avg_sentence_length"]
            if avg < 8:
                instructions.append(f"Write short, punchy sentences (average {avg} words)")
            elif avg < 15:
                instructions.append(f"Mix short and medium sentences (average {avg} words)")
            else:
                instructions.append(f"Use longer, flowing sentences (average {avg} words)")

        # Emoji usage
        if "emoji_frequency" in analysis and analysis.get("uses_emoji"):
            freq = analysis["emoji_frequency"]
            if freq == "none":
                instructions.append("Do not use emojis")
            elif freq == "light":
                instructions.append("Use 1-2 emojis maximum, only if they add meaning")
            elif freq == "moderate":
                instructions.append("Use 3-5 emojis to add personality")
            elif freq == "heavy":
                instructions.append("Use 6+ emojis throughout the caption")

            if "emoji_types_used" in analysis and len(analysis["emoji_types_used"]) > 0:
                emoji_list = ", ".join(analysis["emoji_types_used"][:3])
                instructions.append(f"Preferred emoji types: {emoji_list}")

        # Pidgin/slang usage
        if analysis.get("uses_pidgin") and "common_phrases" in analysis:
            if len(analysis["common_phrases"]) > 0:
                phrases = ", ".join([f'"{p}"' for p in analysis["common_phrases"][:3]])
                instructions.append(f"Use these expressions naturally: {phrases}")

        # Opening style
        if "typical_hooks" in analysis and len(analysis["typical_hooks"]) > 0:
            hooks = analysis["typical_hooks"]
            instructions.append(f"Start captions like this: {hooks[0]}")

        # Closing style
        if "typical_closers" in analysis and len(analysis["typical_closers"]) > 0:
            closers = analysis["typical_closers"]
            instructions.append(f"End captions like this: {closers[0]}")

        # Hashtags
        if "uses_hashtags" in analysis:
            if analysis["uses_hashtags"] and "hashtag_count_avg" in analysis:
                count = analysis["hashtag_count_avg"]
                instructions.append(f"Include approximately {count} hashtags")
            elif not analysis["uses_hashtags"]:
                instructions.append("Do not include hashtags in the caption")

        # Line breaks
        if analysis.get("uses_line_breaks"):
            instructions.append("Use line breaks for visual rhythm and readability")

        # Tone
        if "tone" in analysis:
            instructions.append(f"Maintain a {analysis['tone']} tone throughout")

        # Punctuation
        if "punctuation_habits" in analysis:
            punct = analysis["punctuation_habits"]
            if punct == "minimal":
                instructions.append("Keep punctuation minimal and casual")
            elif punct == "formal":
                instructions.append("Use proper punctuation and grammar")

        if len(instructions) == 0:
            return ""

        return "VOICE PATTERNS FROM YOUR SAMPLE CAPTIONS:\n" + "\n".join(f"- {inst}" for inst in instructions)

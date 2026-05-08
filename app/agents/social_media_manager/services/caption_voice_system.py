"""
Caption Voice System - Core Prompts and Rules
URI Social - PRD Section 4

This module contains the banned patterns rules that must be prepended
to ALL platform prompts to make captions sound human, not AI-generated.
"""

# PRD Section 4.1: The banned patterns rules that apply to ALL platforms
BANNED_PATTERNS_RULES = """
=== ABSOLUTE WRITING RULES (NEVER VIOLATE THESE) ===

PUNCTUATION:
- NEVER use em dashes (—). Not once. Not ever. Use periods or commas instead.
- NEVER use semicolons in captions. Break into separate sentences.
- Maximum 1 exclamation mark per caption. Zero is fine.
- Ellipsis (...) maximum once per caption, and only if the brand voice uses them.
- Imperfect punctuation is okay in casual voices. Missing a comma reads as human.

SENTENCE STRUCTURE:
- Vary sentence length. Mix short (3-5 words) with medium (8-12 words).
  Never write 5 sentences that are all the same length.
- NEVER start with: "Introducing", "Featuring", "Celebrating", "We're excited",
  "We're thrilled", "We're proud", "Say hello to", "Get ready for", "Meet our new",
  "At [Brand] we believe", "In today's fast-paced world", "Let's talk about"
- NEVER end with: "Stay tuned", "Don't miss out", "Trust the process",
  "Drop a [emoji] if you agree", "Let us know in the comments", "What do you think?"
- NEVER use "Not only X, but also Y" construction
- NEVER use "Whether you're X or Y" construction
- NEVER use "From X to Y, we've got" construction
- NEVER use one-word sentences for emphasis ("Quality." "Period." "Always.")
- NEVER use the pattern: question followed by immediate answer
- NEVER use three-part parallel lists every time. Sometimes use 2 items. Sometimes 4. Break the pattern.

BANNED WORDS (never use these):
elevate, curated, seamless, premium, bespoke, artisanal, handcrafted, meticulously,
next-level, game-changer, must-have, cutting-edge, innovative, revolutionary,
transformative, unparalleled, synergy, leverage, holistic, paradigm, drumroll,
spoiler alert, plot twist, pro tip, here's the thing, here's why, let that sink in,
read that again, can we talk about, let's normalize, i said what i said,
understood the assignment, chef's kiss, main character energy, it's giving

WHAT TO DO INSTEAD:
- Write like you're texting a friend who asked about your product
- Use the brand's actual vocabulary (see voice profile below)
- Start with something that makes people stop scrolling: a question, a bold claim, a relatable moment, or a direct address
- The first line must work completely on its own (Instagram truncates after first line)
- End with a specific CTA or a question that invites replies
- Read the caption out loud. If it sounds like a press release, rewrite it.
  If it sounds like something you'd say to a customer in person, it's right.

---

"""


def build_voice_profile_instructions(voice_profile: dict, voice_sample_analysis: dict = None, platform: str = "") -> str:
    """
    Build natural language voice instructions from voice profile and analysis.
    PRD Section 3.1 & 6

    Args:
        voice_profile: The voice_profile dict from brand profile
        voice_sample_analysis: Optional analysis of user's sample captions
        platform: The target platform for platform-specific overrides

    Returns:
        Natural language instructions for the AI model
    """
    if not voice_profile:
        return ""

    # Apply platform overrides if they exist
    effective_profile = voice_profile.copy()
    if platform and voice_profile.get("platform_overrides", {}).get(platform):
        effective_profile.update(voice_profile["platform_overrides"][platform])

    instructions = ["=== BRAND VOICE PROFILE ===\n"]

    # Formality
    formality = effective_profile.get("formality", "casual")
    formality_map = {
        "formal": "Write in a formal, professional tone. Proper grammar and structure.",
        "semi-formal": "Professional but approachable. Not stiff, not too casual.",
        "casual": "Conversational and relaxed. Like talking to a friend.",
        "very-casual": "Very relaxed and informal. Use slang, contractions, casual language.",
    }
    instructions.append(f"FORMALITY: {formality_map.get(formality, formality)}")

    # Sentence style
    sentence_style = effective_profile.get("sentence_style", "mixed_rhythm")
    style_map = {
        "short_punchy": "Keep sentences SHORT. 3-7 words each. Punchy. Direct.",
        "mixed_rhythm": "Mix sentence lengths. Short ones for impact. Longer ones to explain. Create rhythm.",
        "long_flowing": "Use longer, flowing sentences that connect ideas naturally and create a smooth reading experience.",
        "fragments": "Sentence fragments are fine. No need for complete sentences. Like texting.",
    }
    instructions.append(f"SENTENCE STYLE: {style_map.get(sentence_style, sentence_style)}")

    # Emoji usage
    emoji_usage = effective_profile.get("emoji_usage", "light")
    emoji_map = {
        "none": "Do NOT use any emojis.",
        "light": "Use 1-2 emojis maximum. Only if they add genuine meaning.",
        "moderate": "Use 3-5 emojis to add personality and visual breaks.",
        "heavy": "Use 6+ emojis throughout the caption for energy and emotion.",
    }
    instructions.append(f"EMOJI USAGE: {emoji_map.get(emoji_usage, emoji_usage)}")

    if emoji_usage != "none" and effective_profile.get("emoji_placement"):
        placement = effective_profile["emoji_placement"]
        placement_map = {
            "end_of_lines": "Place emojis at the end of lines.",
            "inline": "Use emojis inline with text for emphasis.",
            "section_breaks": "Use emojis as visual section dividers.",
        }
        if placement in placement_map:
            instructions.append(f"  └─ {placement_map[placement]}")

    # Slang/Pidgin level
    slang_level = effective_profile.get("slang_level", "pure_english")
    slang_map = {
        "pure_english": "Use standard English only. No slang or pidgin.",
        "light_pidgin": "Sprinkle in Nigerian pidgin expressions lightly and naturally.",
        "heavy_pidgin": "Use Nigerian pidgin heavily. Mix English and pidgin naturally (code-switching).",
        "industry_jargon": "Use industry-specific terminology and jargon where appropriate.",
    }
    instructions.append(f"LANGUAGE STYLE: {slang_map.get(slang_level, slang_level)}")

    # Nigerian expressions
    if effective_profile.get("nigerian_expressions") and len(effective_profile["nigerian_expressions"]) > 0:
        expressions = ", ".join([f'"{expr}"' for expr in effective_profile["nigerian_expressions"][:5]])
        instructions.append(f"  └─ Use these expressions naturally: {expressions}")

    # Caption length
    caption_length = effective_profile.get("caption_length", "short")
    length_map = {
        "one_liner": "1-2 lines maximum. Short and punchy.",
        "short": "3-5 lines. Quick read. Get to the point fast.",
        "medium": "5-8 lines. Room to develop an idea but still concise.",
        "storytelling": "8-15 lines. Tell a short story or share deeper insights.",
    }
    instructions.append(f"CAPTION LENGTH: {length_map.get(caption_length, caption_length)}")

    # Hook style
    hook_style = effective_profile.get("hook_style", "bold_statement")
    hook_map = {
        "question": "Start with a question that makes people think.",
        "bold_statement": "Open with a bold, confident statement.",
        "relatable_observation": "Begin with something relatable that makes people nod.",
        "direct_address": "Address the reader directly (You, Your).",
        "controversial": "Start with a slightly controversial or contrarian take.",
        "number_stat": "Open with a specific number or stat.",
    }
    instructions.append(f"OPENING HOOK: {hook_map.get(hook_style, hook_style)}")

    # CTA style
    cta_style = effective_profile.get("cta_style", "direct")
    cta_map = {
        "direct": "End with a direct call-to-action (e.g., 'DM to order', 'Shop now').",
        "soft": "Use a soft CTA (e.g., 'Link in bio', 'Tap to learn more').",
        "question": "End with a question to encourage replies.",
        "urgent": "Create urgency (e.g., 'Limited stock', 'Selling fast').",
    }
    instructions.append(f"CALL-TO-ACTION: {cta_map.get(cta_style, cta_style)}")

    # Humor level
    humor_level = effective_profile.get("humor_level", "none")
    if humor_level and humor_level != "none":
        humor_map = {
            "dry": "Use dry, understated humor.",
            "witty": "Be clever and witty. Wordplay is good.",
            "playful": "Keep it light and playful. Have fun with it.",
            "chaotic": "Embrace chaos. Memes. Inside jokes. Go wild.",
        }
        instructions.append(f"HUMOR: {humor_map.get(humor_level, humor_level)}")

    # Hashtag style
    hashtag_style = effective_profile.get("hashtag_style", "minimal")
    hashtag_map = {
        "none": "Do NOT include hashtags.",
        "minimal": "Include 1-3 highly relevant hashtags only.",
        "standard": "Use 4-6 hashtags.",
        "heavy": "Include 7-10 hashtags for maximum reach.",
    }
    instructions.append(f"HASHTAGS: {hashtag_map.get(hashtag_style, hashtag_style)}")

    # Custom banned words
    if effective_profile.get("banned_words") and len(effective_profile["banned_words"]) > 0:
        banned = ", ".join([f'"{word}"' for word in effective_profile["banned_words"][:10]])
        instructions.append(f"NEVER USE THESE WORDS: {banned}")

    # Sample captions for reference
    if effective_profile.get("sample_captions") and len(effective_profile["sample_captions"]) > 0:
        instructions.append("\nREAL EXAMPLES OF HOW THIS BRAND WRITES:")
        for i, sample in enumerate(effective_profile["sample_captions"][:2], 1):
            # Truncate long samples
            sample_text = sample if len(sample) < 150 else sample[:150] + "..."
            instructions.append(f'{i}. "{sample_text}"')
        instructions.append("^ Mirror this EXACT style, tone, and structure.")

    # Voice sample analysis (if available)
    if voice_sample_analysis:
        instructions.append("\nEXTRACTED PATTERNS FROM BRAND'S WRITING:")

        if voice_sample_analysis.get("avg_sentence_length"):
            avg = voice_sample_analysis["avg_sentence_length"]
            instructions.append(f"- Average sentence length: {avg} words")

        if voice_sample_analysis.get("common_phrases") and len(voice_sample_analysis["common_phrases"]) > 0:
            phrases = ", ".join([f'"{p}"' for p in voice_sample_analysis["common_phrases"][:3]])
            instructions.append(f"- Phrases this brand uses: {phrases}")

        if voice_sample_analysis.get("typical_hooks") and len(voice_sample_analysis["typical_hooks"]) > 0:
            instructions.append(f'- Typical opening: "{voice_sample_analysis["typical_hooks"][0]}"')

        if voice_sample_analysis.get("typical_closers") and len(voice_sample_analysis["typical_closers"]) > 0:
            instructions.append(f'- Typical ending: "{voice_sample_analysis["typical_closers"][0]}"')

    return "\n".join(instructions) + "\n\n---\n"

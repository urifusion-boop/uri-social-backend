"""
Caption Validator Service
URI Social - Caption Voice System (PRD Section 7)

Validates generated captions against banned AI patterns to ensure
captions sound human-written, not AI-generated.

This validator catches:
- Em dashes and semicolons
- Banned openers and closers
- Banned words and phrases
- Excessive exclamation marks
- Repetitive parallel structures
"""

from typing import Dict, List, Any


class CaptionValidatorService:
    """
    Validates captions against banned AI patterns.
    PRD Section 7: Post-Generation Caption Validator
    """

    # PRD Section 2.1: Banned Punctuation Patterns
    BANNED_PUNCTUATION = {
        "em_dash": ["—", "--"],
        "semicolon": [";"],
    }

    # PRD Section 2.2: Banned Openers
    BANNED_OPENERS = [
        "introducing",
        "featuring",
        "celebrating",
        "we're excited",
        "we're thrilled",
        "we're proud",
        "say hello",
        "get ready",
        "big news",
        "attention",
        "let's talk about",
        "in today's",
        "at ",  # catches "At [Brand], we believe"
        "meet our",
        "drumroll please",
    ]

    # PRD Section 2.3: Banned Words and Phrases
    BANNED_WORDS = [
        "elevate",
        "curated",
        "seamless",
        "premium",
        "bespoke",
        "artisanal",
        "handcrafted",
        "meticulously",
        "next-level",
        "game-changer",
        "must-have",
        "cutting-edge",
        "innovative",
        "revolutionary",
        "transformative",
        "unparalleled",
        "synergy",
        "leverage",
        "holistic",
        "paradigm",
        "drumroll",
        "spoiler alert",
        "plot twist",
        "pro tip",
        "here's the thing",
        "here's why",
        "let that sink in",
        "read that again",
        "can we talk about",
        "let's normalize",
        "i said what i said",
        "understood the assignment",
        "chef's kiss",
    ]

    # PRD Section 2.2: Banned Closers
    BANNED_CLOSERS = [
        "stay tuned",
        "don't miss out",
        "trust the process",
        "the best is yet to come",
        "drop a",
        "let us know in the comments",
        "what do you think",
        "and that's on",
    ]

    @staticmethod
    def validate_caption(caption: str, custom_banned_words: List[str] = None) -> Dict[str, Any]:
        """
        Validate a caption against all banned AI patterns.

        Args:
            caption: The generated caption to validate
            custom_banned_words: Additional brand-specific banned words

        Returns:
            {
                "passed": bool,
                "issues": List[str],  # List of detected issues
                "severity": str  # "none", "minor", "major"
            }
        """
        issues = []

        # Check for em dashes
        for dash in CaptionValidatorService.BANNED_PUNCTUATION["em_dash"]:
            if dash in caption:
                issues.append(f"contains_em_dash: '{dash}'")

        # Check for semicolons
        if ";" in caption:
            issues.append("contains_semicolon")

        # Check for banned openers
        first_line = caption.split("\n")[0].lower().strip()
        for opener in CaptionValidatorService.BANNED_OPENERS:
            if first_line.startswith(opener):
                issues.append(f"banned_opener: '{opener}'")

        # Check for banned words
        caption_lower = caption.lower()
        all_banned_words = CaptionValidatorService.BANNED_WORDS.copy()
        if custom_banned_words:
            all_banned_words.extend([w.lower() for w in custom_banned_words])

        for word in all_banned_words:
            if word in caption_lower:
                issues.append(f"banned_word: '{word}'")

        # Check for banned closers
        last_lines = caption.split("\n")[-2:]  # Check last 2 lines
        last_text = " ".join(last_lines).lower()
        for closer in CaptionValidatorService.BANNED_CLOSERS:
            if closer in last_text:
                issues.append(f"banned_closer: '{closer}'")

        # Check exclamation count (max 2 allowed)
        exclamation_count = caption.count("!")
        if exclamation_count > 2:
            issues.append(f"too_many_exclamations: {exclamation_count}")

        # Check for excessive three-part parallel lists (X, Y, and Z pattern)
        import re
        parallel_pattern = r"\w+,\s*\w+,\s*and\s+\w+"
        parallel_matches = re.findall(parallel_pattern, caption_lower)
        if len(parallel_matches) > 1:
            issues.append(f"excessive_parallel_lists: {len(parallel_matches)} found")

        # Determine severity
        if len(issues) == 0:
            severity = "none"
        elif len(issues) <= 2:
            severity = "minor"
        else:
            severity = "major"

        return {
            "passed": len(issues) == 0,
            "issues": issues,
            "severity": severity,
        }

    @staticmethod
    def generate_fix_prompt(caption: str, validation_result: Dict[str, Any]) -> str:
        """
        Generate a specific fix prompt based on validation issues.

        Args:
            caption: The original caption that failed validation
            validation_result: The validation result from validate_caption()

        Returns:
            A detailed prompt instructing the model how to fix the issues
        """
        if validation_result["passed"]:
            return ""

        issues = validation_result["issues"]

        fix_instructions = []

        # Build specific fix instructions based on issues
        if any("em_dash" in issue for issue in issues):
            fix_instructions.append("- Replace em dashes (—) with periods or commas")

        if any("semicolon" in issue for issue in issues):
            fix_instructions.append("- Remove semicolons; break into separate sentences instead")

        if any("banned_opener" in issue for issue in issues):
            fix_instructions.append(
                "- Change the opening line. Start with a hook, question, bold statement, or direct address instead"
            )

        if any("banned_word" in issue for issue in issues):
            banned_words_found = [
                issue.split(": ")[1].strip("'") for issue in issues if "banned_word" in issue
            ]
            fix_instructions.append(
                f"- Replace these banned words with simpler, more natural alternatives: {', '.join(banned_words_found)}"
            )

        if any("banned_closer" in issue for issue in issues):
            fix_instructions.append(
                "- Change the ending. Use a specific CTA or question instead of a generic closer"
            )

        if any("too_many_exclamations" in issue for issue in issues):
            fix_instructions.append("- Reduce exclamation marks to 1 or 0. Use stronger words instead of punctuation for emphasis")

        if any("excessive_parallel_lists" in issue for issue in issues):
            fix_instructions.append("- Break up the three-part parallel lists. Use 2 items, or 4 items, or restructure completely")

        fix_prompt = f"""The caption you wrote has these problems:
{chr(10).join(f'  {issue}' for issue in issues)}

Rewrite the caption fixing these specific issues:
{chr(10).join(fix_instructions)}

CRITICAL RULES FOR THE REWRITE:
- Keep the same core message and tone
- Maintain the brand voice
- Sound like a real person, not AI
- Read it out loud - if it sounds like a press release, try again

Original caption:
{caption}

Write ONLY the fixed caption. No explanations."""

        return fix_prompt

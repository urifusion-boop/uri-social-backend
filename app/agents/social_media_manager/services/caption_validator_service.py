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
        import re

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
        parallel_pattern = r"\w+,\s*\w+,\s*and\s+\w+"
        parallel_matches = re.findall(parallel_pattern, caption_lower)
        if len(parallel_matches) > 1:
            issues.append(f"excessive_parallel_lists: {len(parallel_matches)} found")

        # ===== FORMATTING RULES (Caption Formatting Rules PRD) =====

        # Check for raw Markdown characters
        markdown_patterns = [
            (r'\*[^*]+\*', 'markdown_asterisk_emphasis'),
            (r'\*\*[^*]+\*\*', 'markdown_double_asterisk_bold'),
            (r'_[^_]+_', 'markdown_underscore_emphasis'),
            (r'__[^_]+__', 'markdown_double_underscore_bold'),
        ]
        for pattern, name in markdown_patterns:
            if re.search(pattern, caption):
                issues.append(name)

        # Check for hyphen bullets
        if re.search(r'^\s*[-–—]\s+', caption, re.MULTILINE):
            issues.append('hyphen_bullet')

        # Check for pipe dividers
        if ' | ' in caption:
            issues.append('pipe_divider')

        # Check for numbered lists
        if re.search(r'^\s*\d+[.)]\s', caption, re.MULTILINE):
            issues.append('numbered_list')

        # Check for parenthetical explanations (longer than 10 chars)
        if re.search(r'\([^)]{10,}\)', caption):
            issues.append('parenthetical_explanation')

        # Check for quoted product names (capitalized words in quotes)
        if re.search(r'"[A-Z][^"]{2,20}"', caption):
            issues.append('quoted_product_name')

        # Check for slash constructions
        if re.search(r'\w+/\w+', caption):
            issues.append('slash_construction')

        # Check for colon introductions at line start
        if re.search(r'^[A-Z][^:]{2,20}:\s', caption, re.MULTILINE):
            issues.append('colon_introduction')

        # Check for HTML entities
        if re.search(r'&(amp|gt|lt|nbsp|quot);', caption):
            issues.append('html_entity')

        # Check for arrow text
        if '->' in caption or '-->' in caption:
            issues.append('arrow_text')

        # Check for wall of text (more than 2 sentences without a line break)
        lines = caption.split('\n')
        for line in lines:
            sentence_count = len(re.findall(r'[.!?]', line))
            if sentence_count > 2 and len(line) > 150:
                issues.append('wall_of_text')
                break

        # Check minimum line breaks (caption should have at least 2 blank lines)
        blank_lines = len(re.findall(r'\n\n', caption))
        if len(caption) > 100 and blank_lines < 2:
            issues.append('insufficient_line_breaks')

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

        # Formatting-specific fixes
        if any("markdown_" in issue for issue in issues):
            fix_instructions.append("- Remove ALL asterisks (*text*) and underscores (_text_). Use CAPS or line isolation for emphasis")

        if any("hyphen_bullet" in issue for issue in issues):
            fix_instructions.append("- Remove ALL hyphens used as bullets (- item). Write items as separate lines with no bullet character")

        if any("pipe_divider" in issue for issue in issues):
            fix_instructions.append("- Remove ALL pipe dividers (|). Use line breaks to separate sections")

        if any("numbered_list" in issue for issue in issues):
            fix_instructions.append("- Remove numbered lists (1. 2. 3.). Write as separate paragraphs")

        if any("parenthetical_explanation" in issue for issue in issues):
            fix_instructions.append("- Remove parenthetical explanations. Break into separate lines")

        if any("quoted_product_name" in issue for issue in issues):
            fix_instructions.append("- Remove quotation marks around product names")

        if any("slash_construction" in issue for issue in issues):
            fix_instructions.append("- Remove slash constructions (this/that). Pick one or use 'or'")

        if any("colon_introduction" in issue for issue in issues):
            fix_instructions.append("- Remove colon introductions (Label: text). Start with the content")

        if any("html_entity" in issue for issue in issues):
            fix_instructions.append("- Replace HTML entities (&amp; &gt;) with actual characters (and, >)")

        if any("arrow_text" in issue for issue in issues):
            fix_instructions.append("- Remove arrow characters (-> -->). Use words or line breaks")

        if any("wall_of_text" in issue for issue in issues):
            fix_instructions.append("- Break after every 1-2 sentences. Add line breaks between thoughts")

        if any("insufficient_line_breaks" in issue for issue in issues):
            fix_instructions.append("- Add blank lines (double line break) between sections. Need at least 2-3 blank line separators")

        fix_prompt = f"""The caption you wrote has these problems:
{chr(10).join(f'  {issue}' for issue in issues)}

Rewrite the caption fixing these specific issues:
{chr(10).join(fix_instructions)}

CRITICAL FORMATTING RULES:
- MANDATORY: Add a blank line (press Enter twice) after every 1-2 sentences
- MANDATORY: The caption MUST have at least 2-3 blank line separators (\\n\\n)
- MANDATORY: No line should contain more than 2 sentences
- MANDATORY: Break long thoughts into separate paragraphs with blank lines between them
- Keep the same core message and tone
- Maintain the brand voice
- Sound like a real person, not AI
- The caption should look good on a phone screen (short paragraphs, lots of white space)

EXAMPLE FORMAT:
Opening sentence here.

Next thought on its own line.

Another sentence or two here.

Final call to action.

Original caption:
{caption}

Write ONLY the fixed caption with proper blank line spacing. No explanations."""

        return fix_prompt

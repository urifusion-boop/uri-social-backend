"""
Shared regex-based content guards for L4 ad format modules.

Heuristic, defense-in-depth checks for language patterns VSG-01 prohibits
outright — not a substitute for retrieval-time curation (§6). Each format
module's own docstring explains how its specific hard check maps back to
the spec; this module exists only so the shared vocabulary (health/
appearance outcome words, currently used by both Review Card's timed-claim
guard and Day 1 → Day 30's category exclusion) doesn't drift between two
copies.
"""
import re

OUTCOME_WORD = re.compile(
    r"\b(?:skin|complexion|wrinkle|wrinkles|acne|glow|glowing|fair(?:er|ness)?|"
    r"weight|kg|kilos?|fat|slim(?:mer|ming)?|cellulite|stretch\s*marks?|hair\s*"
    r"growth|bleach(?:ed|ing)?)\b",
    re.IGNORECASE,
)


def mentions_health_or_appearance_outcome(text: str) -> bool:
    return bool(OUTCOME_WORD.search(text))

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


# Broader than OUTCOME_WORD — Text on a Face's rule (§2.7) is "never
# touches health, body, finances or personal circumstance" outright, with
# no time-phrase pairing condition the way Review Card's guard has. Best-
# effort, defense-in-depth, same framing as every regex guard in this
# module: not a substitute for retrieval-time curation.
#
# Deliberately does NOT include generic distress verbs like "struggle" or
# "suffer" — a seller saying "I struggled to find reliable suppliers" is
# exactly the first-person "observed situation" §2.7 permits, not a
# violation; those words only matter when aimed at the viewer, which is
# VIEWER_PRESUMPTION's job below, not this list's. Also excludes bare
# "relationship" — "long-term relationships with clients" is ordinary
# business language, not personal circumstance; "body" is kept even
# though it risks the same kind of false positive ("body shop", "body
# wash") because it's one of §2.7's own named categories verbatim.
PERSONAL_TOPIC_WORD = re.compile(
    r"\b(?:skin|complexion|wrinkle|wrinkles|acne|weight|kg|kilos?|fat|slim(?:mer|ming)?|"
    r"cellulite|scar|hair\s*loss|body|"
    r"debt|loan|broke|bankrupt(?:cy)?|salary|income|afford|money\s*problems?|"
    r"financ(?:e|ial|es)|"
    r"divorce|marriage|lonely|loneliness|single\s*(?:mom|mum|dad|parent)|"
    r"heartbreak|breakup|depress(?:ed|ion)|anxiety)\b",
    re.IGNORECASE,
)


def mentions_disallowed_personal_topic(text: str) -> bool:
    return bool(PERSONAL_TOPIC_WORD.search(text))


# §2.7's named example of the rejected pattern: "are you struggling with…"
# — a second-person address paired with a presumptive stance verb, on any
# topic (not only the ones PERSONAL_TOPIC_WORD lists) — "are you looking
# for a better phone repair shop" is the same structural presumption about
# the viewer, just not a personal-attribute one.
VIEWER_PRESUMPTION = re.compile(
    r"\b(?:are you|do you|have you|is your|are your|you're|you are)\b[^.?!]{0,50}"
    r"(?:struggl\w*|suffer\w*|tired\s+of|sick\s+of|deal(?:ing)?\s+with|feel(?:ing)?\s+like|"
    r"experienc\w*|going\s+through|battling|looking\s+for|in\s+need\s+of)",
    re.IGNORECASE,
)


def presumes_viewer_attribute(text: str) -> bool:
    return bool(VIEWER_PRESUMPTION.search(text))

"""
Jane + Ads — automated policy-review gate ("Policy Review" in the engineering work
split — scoped to Ibukun/Meta-policy domain, taken up here as a first-line heuristic
gate. Ibukun's Meta-policy expertise should still tune/extend this before it's the
ONLY thing standing in front of a live submission).

One policy violation can suspend the ENTIRE pooled advertising account, not just one
client's ad — so this errs conservative: any BLOCK-severity match fails the whole
creative. Deterministic, keyword/pattern-based, no live Meta call — fully testable
today, and meant to run on `creative.py`'s output before it's ever handed to an ad
platform (mock or real).

The category rules below (prohibited phrases, exaggerated claims, personal-attribute
targeting, spammy formatting) were written against Meta's own ad policy specifically.
They're run against TikTok-bound creative too (`platform="tiktok"`, see
review_ad_creative) since the broad categories are plausibly similar across
platforms, but that hasn't been independently verified against TikTok's own written
ad policy, which is known to differ from Meta's in places — a real TikTok-specific
policy pass is still open work, not done by this fix. What IS fixed: a TikTok
rejection now names "TikTok" in its guidance text instead of incorrectly blaming
"Meta" for a platform the user never chose.
"""
from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field


class Severity(str, Enum):
    BLOCK = "block"   # would risk the pooled account — never submit
    WARN = "warn"     # low-quality / spammy — submit, but worth flagging for cleanup


class PolicyViolation(BaseModel):
    category: str
    severity: Severity
    matched_text: str
    guidance: str


class PolicyReviewResult(BaseModel):
    approved: bool
    violations: list[PolicyViolation] = Field(default_factory=list)


# ── Hard-block: prohibited content categories (Meta ad policy) ─────────────────
_PROHIBITED_PHRASES: dict[str, list[str]] = {
    "adult_content": ["porn", "onlyfans", "nude photos", "sex tape", "escort service"],
    "illegal_drugs": ["buy cocaine", "buy heroin", "crystal meth", "marijuana for sale", "buy weed"],
    "weapons": ["buy a gun", "firearms for sale", "ammunition for sale", "explosives for sale"],
    "tobacco_vaping": ["cigarettes for sale", "vape juice for sale", "buy tobacco"],
    "unlicensed_gambling": ["online casino", "sports betting site", "bet now win big"],
    "counterfeit_goods": ["replica designer", "fake rolex", "counterfeit goods"],
}

# ── Hard-block: exaggerated / unrealistic-outcome claims (Meta ad policy) ──────
_EXAGGERATED_CLAIM_PHRASES: list[str] = [
    "miracle cure", "guaranteed to cure", "100% guaranteed", "lose weight overnight",
    "get rich quick", "risk-free investment", "instant cure", "guaranteed income",
    "cures cancer", "guaranteed to work",
]

# ── Hard-block: asserting personal attributes about the viewer (Meta ad policy) ─
_PERSONAL_ATTRIBUTE_PATTERNS: list[str] = [
    r"\bare you (fat|overweight|broke|in debt|pregnant|depressed|gay|lonely)\b",
    r"\byour (disability|divorce|bankruptcy|sexual orientation|pregnancy)\b",
    r"\bpeople with (your|these) (disability|condition)\b",
]

def _platform_label(platform: str) -> str:
    return "TikTok" if platform == "tiktok" else "Meta"


# {platform} is filled in by review_ad_creative from its own platform argument —
# these category rules were written and scoped to Meta's own ad policy (module
# docstring), and haven't been independently checked against TikTok's written
# policy, which differs in places. Interpolating the platform name at least stops
# a TikTok-bound rejection from blaming the wrong platform by name; it does not
# mean every rule below has been verified correct for TikTok specifically.
_GUIDANCE = {
    "adult_content": "Adult content is never allowed in {platform} ads.",
    "illegal_drugs": "Illegal drug sales/promotion are never allowed in {platform} ads.",
    "weapons": "Weapon sales are restricted/prohibited on {platform} ads.",
    "tobacco_vaping": "Tobacco/vaping products are restricted on {platform} ads.",
    "unlicensed_gambling": "Gambling ads require {platform}'s specific licensing approval.",
    "counterfeit_goods": "Counterfeit/replica goods are never allowed in {platform} ads.",
    "exaggerated_claims": "{platform} bans unrealistic outcome/guarantee claims — rewrite without the guarantee.",
    "personal_attributes": "{platform} bans directly asserting a viewer's personal attributes — speak to the need, not the person.",
}


def _find_phrase(text: str, phrases: list[str]) -> str | None:
    lowered = text.lower()
    for phrase in phrases:
        if phrase in lowered:
            return phrase
    return None


def _find_pattern(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(0)
    return None


def _has_excessive_caps(text: str) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 8:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return (upper / len(letters)) > 0.6


def _has_excessive_punctuation(text: str) -> bool:
    return bool(re.search(r"[!?]{3,}", text))


def review_ad_creative(
    headline: str, primary_text: str = "", image_prompt: str = "", platform: str = "meta",
) -> PolicyReviewResult:
    """Run before any creative reaches an ad platform (mock or real). Combines the ad
    copy and the image brief into one scan — a prohibited image concept is just as
    dangerous to the pooled account as prohibited ad copy.

    `platform` ("meta" | "tiktok") only changes which platform name the guidance
    text blames — see the _GUIDANCE dict's own note on what that does and doesn't
    mean for TikTok specifically."""
    combined = " ".join(t for t in (headline, primary_text, image_prompt) if t)
    violations: list[PolicyViolation] = []
    platform_label = _platform_label(platform)
    review_label = f"{platform_label}'s review"

    for category, phrases in _PROHIBITED_PHRASES.items():
        hit = _find_phrase(combined, phrases)
        if hit:
            violations.append(PolicyViolation(
                category=category, severity=Severity.BLOCK, matched_text=hit,
                guidance=_GUIDANCE[category].format(platform=platform_label),
            ))

    hit = _find_phrase(combined, _EXAGGERATED_CLAIM_PHRASES)
    if hit:
        violations.append(PolicyViolation(
            category="exaggerated_claims", severity=Severity.BLOCK, matched_text=hit,
            guidance=_GUIDANCE["exaggerated_claims"].format(platform=platform_label),
        ))

    hit = _find_pattern(combined, _PERSONAL_ATTRIBUTE_PATTERNS)
    if hit:
        violations.append(PolicyViolation(
            category="personal_attributes", severity=Severity.BLOCK, matched_text=hit,
            guidance=_GUIDANCE["personal_attributes"].format(platform=platform_label),
        ))

    for field_name, text in (("headline", headline), ("primary_text", primary_text)):
        if not text:
            continue
        if _has_excessive_caps(text):
            violations.append(PolicyViolation(
                category="excessive_caps", severity=Severity.WARN, matched_text=text,
                guidance=f"{field_name} is mostly uppercase — reads as spammy/low-quality to {review_label}.",
            ))
        if _has_excessive_punctuation(text):
            violations.append(PolicyViolation(
                category="excessive_punctuation", severity=Severity.WARN, matched_text=text,
                guidance=f"{field_name} has repeated !/? — reads as spammy/low-quality to {review_label}.",
            ))

    approved = not any(v.severity == Severity.BLOCK for v in violations)
    return PolicyReviewResult(approved=approved, violations=violations)

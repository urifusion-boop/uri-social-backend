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

_GUIDANCE = {
    "adult_content": "Adult content is never allowed in Meta ads.",
    "illegal_drugs": "Illegal drug sales/promotion are never allowed in Meta ads.",
    "weapons": "Weapon sales are restricted/prohibited on Meta ads.",
    "tobacco_vaping": "Tobacco/vaping products are restricted on Meta ads.",
    "unlicensed_gambling": "Gambling ads require Meta's specific licensing approval.",
    "counterfeit_goods": "Counterfeit/replica goods are never allowed in Meta ads.",
    "exaggerated_claims": "Meta bans unrealistic outcome/guarantee claims — rewrite without the guarantee.",
    "personal_attributes": "Meta bans directly asserting a viewer's personal attributes — speak to the need, not the person.",
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


def review_ad_creative(headline: str, primary_text: str = "", image_prompt: str = "") -> PolicyReviewResult:
    """Run before any creative reaches an ad platform (mock or real). Combines the ad
    copy and the image brief into one scan — a prohibited image concept is just as
    dangerous to the pooled account as prohibited ad copy."""
    combined = " ".join(t for t in (headline, primary_text, image_prompt) if t)
    violations: list[PolicyViolation] = []

    for category, phrases in _PROHIBITED_PHRASES.items():
        hit = _find_phrase(combined, phrases)
        if hit:
            violations.append(PolicyViolation(
                category=category, severity=Severity.BLOCK, matched_text=hit,
                guidance=_GUIDANCE[category],
            ))

    hit = _find_phrase(combined, _EXAGGERATED_CLAIM_PHRASES)
    if hit:
        violations.append(PolicyViolation(
            category="exaggerated_claims", severity=Severity.BLOCK, matched_text=hit,
            guidance=_GUIDANCE["exaggerated_claims"],
        ))

    hit = _find_pattern(combined, _PERSONAL_ATTRIBUTE_PATTERNS)
    if hit:
        violations.append(PolicyViolation(
            category="personal_attributes", severity=Severity.BLOCK, matched_text=hit,
            guidance=_GUIDANCE["personal_attributes"],
        ))

    for field_name, text in (("headline", headline), ("primary_text", primary_text)):
        if not text:
            continue
        if _has_excessive_caps(text):
            violations.append(PolicyViolation(
                category="excessive_caps", severity=Severity.WARN, matched_text=text,
                guidance=f"{field_name} is mostly uppercase — reads as spammy/low-quality to Meta's review.",
            ))
        if _has_excessive_punctuation(text):
            violations.append(PolicyViolation(
                category="excessive_punctuation", severity=Severity.WARN, matched_text=text,
                guidance=f"{field_name} has repeated !/? — reads as spammy/low-quality to Meta's review.",
            ))

    approved = not any(v.severity == Severity.BLOCK for v in violations)
    return PolicyReviewResult(approved=approved, violations=violations)

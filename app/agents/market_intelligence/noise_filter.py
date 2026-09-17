"""
Uri Market Intelligence — deterministic pre-classification noise filter.

Adapted from uri-insights' SignalRefineryService: obviously-spam evidence
(promo/bot boilerplate) is filtered by a cheap keyword/pattern check BEFORE
the LLM classification call, per PRD §23 ("use cheaper deterministic filters
before expensive inference"). What was intentionally NOT ported from
SignalRefineryService:

- apply_nigerian_entity_filter: discards anything lacking a Nigerian
  city/currency marker. Market Intelligence topics are not scoped to Nigeria
  specifically (PRD §7/§11 keep geography as an explicit/inferred field, not
  an eligibility gate), so this would wrongly drop valid evidence from any
  other market a brand operates in.
- detect_duplicates: SignalRefineryService's duplicate check is a simple
  text-similarity heuristic; this module already has a more accurate
  embedding-based dedup path in clustering.py, so a second, cruder duplicate
  detector here would only add false positives.

This filter only ever produces a NOISE verdict — it never promotes evidence
to a positive type. Anything it doesn't flag proceeds to classify_evidence()
as before.
"""
from __future__ import annotations

import re

from .models import RawEvidence

# Promotional/scam categories a genuine customer post would never fall into,
# regardless of what business is running the topic.
BLOCKLIST = [
    "forex", "crypto", "bitcoin", "binary options", "investment platform",
    "sugar mummy", "sugar daddy", "hookup", "dating site",
    "bet9ja", "sportybet", "betting tips", "odds today",
    "mlm", "network marketing", "recruit downline", "join my team",
    "work from home", "earn from home", "make money online",
    "adult content", "onlyfans",
]

# Boilerplate phrasing bot/spam accounts use regardless of topic.
BOT_PATTERNS = [
    "limited time offer", "click link in bio", "click the link in bio",
    "dm for rates", "pm me", "inbox me", "hurry now", "last chance",
    "act fast", "don't miss out", "follow for follow", "f4f",
    "like and share", "tag 3 friends",
]


def _matches_any(text_lower: str, phrases: list[str]) -> str | None:
    for phrase in phrases:
        if phrase in text_lower:
            return phrase
    return None


def deterministic_noise_reason(evidence: RawEvidence) -> str | None:
    """Returns a human-readable rejection reason if the evidence is
    deterministically noise, else None. None means "not caught by this
    filter" — it does NOT mean the evidence is confirmed genuine; it still
    goes on to LLM classification for that judgement."""
    text_lower = evidence.text.lower()

    hit = _matches_any(text_lower, BLOCKLIST)
    if hit:
        return f"matched blocklist term '{hit}'"

    hit = _matches_any(text_lower, BOT_PATTERNS)
    if hit:
        return f"matched bot/spam pattern '{hit}'"

    # Repeated-character shouting with no other content (e.g. "!!!!!!!!!!!!")
    # is a common bot filler pattern not covered by phrase matching.
    if re.fullmatch(r"[\W\d_]{0,3}([!?.])\1{6,}[\W\d_]{0,3}", text_lower.strip()):
        return "matched repeated-punctuation spam pattern"

    return None

"""
The Brand Brief — one way to turn the Brand Playbook into instructions.

Every generator (posts, carousels, calendar, ads, video, inbox, blog) used
to build its own partial version of the Playbook, so a brand's guardrails
protected posts but not ads, and the inbox didn't sound like the brand at
all. This module is the single source: each generator asks for the tiers it
needs and gets the same wording and the same rules.

Tiers, in priority order (also stated to the model, so conflicts resolve the
same way everywhere):

  1. HARD RULES   never broken: avoid-topics, banned words, compliance,
                  language. Checked again after writing with violations().
  2. VOICE        how the brand sounds.
  3. AUDIENCE     who it's for; adapts the voice, never replaces it.
  4. OFFER        what the business sells and why people choose it.
  5. RIGHT NOW    current promotions/news (Business Pulse), dropped when old.

brand_context here is BrandProfileService.to_brand_context(profile) — or the
raw profile, which uses the same keys for everything read below except
business_description (product_description on the raw profile).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

PRIORITY_ORDER = (
    "PRIORITY when instructions conflict: (1) HARD RULES are never broken; "
    "(2) BRAND VOICE decides how it sounds; (3) AUDIENCE adapts that voice, it never replaces it; "
    "(4) everything else."
)

PULSE_FRESH_DAYS = 21    # same threshold the content calendar uses
PULSE_EXPIRY_DAYS = 45   # older than this, a "current" promotion is no longer mentioned at all


def _list(value: Any, limit: int = 0) -> List[str]:
    if isinstance(value, str):
        value = re.split(r"[,\n]", value)
    items = [str(v).strip() for v in (value or []) if str(v).strip()]
    return items[:limit] if limit else items


# ── 1. Hard rules ────────────────────────────────────────────────────────────

def banned_words(bc: Optional[Dict[str, Any]]) -> List[str]:
    """Every word/phrase the brand said never to use: the voice profile's
    banned words, the Playbook's "Words to avoid", and the guardrails' banned
    words (free text, comma/newline separated)."""
    bc = bc or {}
    words = _list((bc.get("voice_profile") or {}).get("banned_words"))
    words += _list(bc.get("words_to_avoid"))
    words += _list((bc.get("guardrails") or {}).get("banned_words"))
    seen, out = set(), []
    for w in words:
        if w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out


def languages_rule(bc: Optional[Dict[str, Any]]) -> Optional[str]:
    langs = _list((bc or {}).get("languages"))
    if not langs or langs == ["English"]:
        return None
    if len(langs) == 1:
        return f"Write in {langs[0]}."
    return (f"Languages: {', '.join(langs)}. Write mainly in {langs[0]} and naturally mix in "
            f"{', '.join(langs[1:])} the way this audience actually speaks — not a translation, a natural blend.")


def hard_rules(bc: Optional[Dict[str, Any]], captions: bool = False, language: bool = True) -> List[str]:
    """captions=True adds the caption-only format rules (emoji, hashtag limit).
    language=False for writers that must answer in the reader's own language
    (the inbox replies in the customer's language, not the brand's)."""
    bc = bc or {}
    g = bc.get("guardrails") or {}
    rules: List[str] = []
    if g.get("avoid_topics"):
        rules.append(f"NEVER mention or reference: {g['avoid_topics']}.")
    words = banned_words(bc)
    if words:
        rules.append(f"NEVER use these words or phrases: {', '.join(words)}.")
    if g.get("compliance_notes"):
        rules.append(f"Compliance requirement: {g['compliance_notes']}.")
    if language and languages_rule(bc):
        rules.append(languages_rule(bc))
    emoji = g.get("emoji_usage")
    if emoji == "no":
        rules.append("Use NO emojis under any circumstances.")
    elif emoji == "some":
        rules.append("Use emojis very sparingly — maximum 1-2, only if they add genuine meaning.")
    if captions:
        max_hash = g.get("max_hashtags")
        if max_hash and max_hash not in ("No limit", ""):
            rules.append(f"Use a maximum of {max_hash} hashtags.")
    return rules


def violations(text: str, bc: Optional[Dict[str, Any]]) -> List[str]:
    """Banned words/phrases that appear in `text` (whole words, any case).
    For checking a piece of writing after the model produced it."""
    lowered = (text or "").lower()
    found = []
    for w in banned_words(bc):
        if re.search(r"(?<!\w)" + re.escape(w.lower()) + r"(?!\w)", lowered):
            found.append(w)
    return found


# ── 2. Voice ─────────────────────────────────────────────────────────────────

def voice(bc: Optional[Dict[str, Any]], platform: str = "", sample: bool = True) -> List[str]:
    bc = bc or {}
    lines: List[str] = []
    tones = bc.get("platform_tones") or {}
    platform_tone = tones.get(platform) if (platform and not bc.get("same_tone_everywhere", True)) else None
    active = platform_tone or bc.get("brand_voice") or ""
    if active:
        lines.append(f"Brand voice/tone: {active}. Every sentence must sound like this brand.")
    if sample and bc.get("voice_sample"):
        lines.append(f'Real example of this brand\'s writing: "{str(bc["voice_sample"])[:400]}". '
                     f"Mirror its sentence structure, vocabulary and energy.")
    return lines


# ── 3. Audience ──────────────────────────────────────────────────────────────

_AGE_GROUPS = [
    ("Gen Z", ("gen z", "18-24"),
     "Gen Z (18-24): short sentences, casual and unpolished, like a real person texting; light slang where it "
     "fits (lowkey, fr, ngl, no cap, iykyk); no corporate words."),
    ("Millennials", ("millennial", "25-40"),
     "Millennials (25-40): conversational but polished, relatable, values authenticity and experiences."),
    ("Gen X", ("gen x", "41-56"),
     "Gen X (41-56): straightforward and practical, no hype; focus on value, quality and results."),
    ("Boomers", ("boomer", "57+"),
     "Boomers (57+): clear, respectful, well-structured; emphasise trust and reliability; no slang or internet jargon."),
]


def age_groups(bc: Optional[Dict[str, Any]]) -> List[str]:
    """Which named age groups the Playbook's age range selects (it can hold
    several, comma-joined)."""
    raw = str((bc or {}).get("audience_age_range") or "").lower()
    return [name for name, keys, _ in _AGE_GROUPS if any(k in raw for k in keys)]


def audience_adaptation(bc: Optional[Dict[str, Any]]) -> List[str]:
    """How to adapt the brand voice for the audience's age. One group: lean
    into it. Several: write so all of them feel addressed. Always an
    adjustment to the brand voice — hard rules and voice still apply."""
    raw = str((bc or {}).get("audience_age_range") or "").strip()
    if not raw or "everyone" in raw.lower():
        return []
    groups = age_groups(bc)
    notes = {name: note for name, _, note in _AGE_GROUPS}
    if not groups:
        return [f"Audience age: {raw}. Adjust language and references to match."]
    if len(groups) == 1:
        strong = ""
        if groups[0] == "Gen Z":
            strong = (" If the result sounds like a 40-year-old marketing manager wrote it, it's wrong — "
                      "but stay inside the brand voice and the hard rules.")
        return [f"Adapt the brand voice for {notes[groups[0]]}{strong}"]
    return [
        f"The audience spans {', '.join(groups)}. Adapt the brand voice so all of them feel addressed — "
        f"don't write for only one group: " + " ".join(notes[g] for g in groups)
    ]


def audience(bc: Optional[Dict[str, Any]], detail: str = "summary") -> List[str]:
    """detail="summary": who they are + top pain point/objection lines.
    detail="full": every customer field the Playbook holds."""
    bc = bc or {}
    lines: List[str] = []
    if bc.get("target_audience"):
        lines.append(f"Target audience: {bc['target_audience']}.")
    if bc.get("ideal_customer_profile"):
        lines.append(f"Ideal customer: {bc['ideal_customer_profile']}.")
    who = [bc.get("customer_gender"), bc.get("customer_occupation"),
           f"in {bc['customer_location']}" if bc.get("customer_location") else "",
           f"{bc['customer_income_level']} income" if bc.get("customer_income_level") else ""]
    who = [w for w in who if w]
    if who:
        lines.append(f"Customer profile: {', '.join(who)}.")
    top = 4 if detail == "full" else 3
    for label, key in [("Pain points", "customer_pain_points"), ("Needs", "customer_needs"),
                       ("Objections", "customer_objections")]:
        vals = _list(bc.get(key), top)
        if vals:
            lines.append(f"{label}: {'; '.join(vals)}.")
    if detail == "full":
        for label, key in [("Interests", "customer_interests"), ("What they want", "customer_desires"),
                           ("What they fear", "customer_fears"), ("What frustrates them", "customer_frustrations"),
                           ("What they aspire to", "customer_aspirations"), ("Why they hesitate", "customer_hesitations"),
                           ("Questions they often ask", "common_questions"), ("What makes them buy", "buying_triggers")]:
            vals = _list(bc.get(key), 3)
            if vals:
                lines.append(f"{label}: {'; '.join(vals)}.")
    lines += audience_adaptation(bc)
    return lines


# ── 4. Offer ─────────────────────────────────────────────────────────────────

def offer(bc: Optional[Dict[str, Any]]) -> List[str]:
    bc = bc or {}
    lines: List[str] = []
    description = bc.get("business_description") or bc.get("product_description")
    if description:
        lines.append(f"What the business does: {description}.")
    products = _list(bc.get("key_products_services"), 6)
    if products:
        lines.append(f"Key products/services: {', '.join(products)}.")
    if bc.get("unique_selling_proposition"):
        lines.append(f"What makes it different: {bc['unique_selling_proposition']}.")
    if bc.get("why_customers_choose_us"):
        lines.append(f"Why customers choose it: {bc['why_customers_choose_us']}.")
    if bc.get("price_range"):
        lines.append(f"Price positioning: {bc['price_range']} — match the wording to this price tier.")
    if bc.get("primary_goal"):
        lines.append(f"The brand's main goal: {bc['primary_goal']}.")
    if competitors_line(bc):
        lines.append(competitors_line(bc))
    return lines


def competitors_line(bc: Optional[Dict[str, Any]]) -> Optional[str]:
    """Competitors are for positioning, never for naming: the writing should
    make clear what this brand does differently, and must not mention, tag or
    compare against them by name."""
    names = _list((bc or {}).get("competitor_handles"), 5)
    if not names:
        return None
    return (f"Competitors: {', '.join(names)}. NEVER name, tag or directly compare against them. "
            f"Use this only to make what THIS brand does differently unmistakable.")


# ── 5. Right now (Business Pulse) ────────────────────────────────────────────

def pulse(bc: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> List[str]:
    """Current promotions, campaigns, launches, milestones and news — only
    while they're plausibly still current. Old ones are dropped entirely
    rather than advertised after they've ended."""
    bc = bc or {}
    data = bc.get("business_pulse") or {}
    updated = bc.get("business_pulse_updated_at")
    if isinstance(updated, str):
        try:
            updated = datetime.fromisoformat(updated.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            updated = None
    age_days = ((now or datetime.utcnow()) - updated).days if isinstance(updated, datetime) else None
    if age_days is not None and age_days > PULSE_EXPIRY_DAYS:
        return []
    lines: List[str] = []
    if data.get("current_period_goal"):
        lines.append(f"Goal for this period: {data['current_period_goal']}.")
    for label, key in [("Active promotions/offers", "current_promotions"), ("Active campaigns", "current_campaigns"),
                       ("Newly launched", "new_products_services"), ("Recent milestones", "recent_milestones"),
                       ("News/announcements", "business_news_announcements")]:
        vals = _list(data.get(key), 5)
        if vals:
            lines.append(f"{label}: {'; '.join(vals)}.")
    if lines and age_days is not None and age_days > PULSE_FRESH_DAYS:
        lines.append(f"(Saved {age_days} days ago — may have ended. Mention only if it clearly still fits; never invent terms.)")
    return lines


# ── Build ────────────────────────────────────────────────────────────────────

_ALL = ("rules", "voice", "audience", "offer", "pulse")


def build(
    bc: Optional[Dict[str, Any]],
    sections: Iterable[str] = _ALL,
    platform: str = "",
    detail: str = "summary",
    captions: bool = False,
    language: bool = True,
    now: Optional[datetime] = None,
) -> str:
    """The brief as prompt text. Empty string when the Playbook has nothing
    for the requested sections, so callers can append it unconditionally."""
    if not bc:
        return ""
    sections = tuple(sections)
    parts: List[str] = []

    def block(title: str, lines: List[str]) -> None:
        if lines:
            parts.append(f"{title}\n" + "\n".join(f"- {l}" for l in lines))

    name = bc.get("brand_name")
    if "rules" in sections:
        block("HARD RULES (never break these):", hard_rules(bc, captions=captions, language=language))
    if "voice" in sections:
        block("BRAND VOICE:", voice(bc, platform=platform))
    if "audience" in sections:
        block("AUDIENCE:", audience(bc, detail=detail))
    if "offer" in sections:
        block("THE BUSINESS:", offer(bc))
    if "pulse" in sections:
        block("HAPPENING RIGHT NOW (use where it genuinely fits; quote the real terms, never invent any):", pulse(bc, now=now))
    if not parts:
        return ""
    header = f"BRAND BRIEF{f' — {name}' if name else ''}\n{PRIORITY_ORDER}"
    return header + "\n\n" + "\n\n".join(parts)

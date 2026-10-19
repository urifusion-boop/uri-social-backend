"""
The Brand Brief — the Brand Playbook's hard rules as instructions.

Only the part the 30-day Content Calendar uses: the brand's hard rules
(topics to avoid, banned words, compliance, language, emoji and hashtag
limits, the owner's own instructions), the full banned-word list, and one
matcher that catches every common form of a banned word without flagging
unrelated words ("ok" in "book"). Same code as the development branch's
brand_brief.py, which also carries the voice/audience/offer tiers used by
other generators.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

def _list(value: Any, limit: int = 0) -> List[str]:
    if isinstance(value, str):
        value = re.split(r"[,\n]", value)
    items = [str(v).strip() for v in (value or []) if str(v).strip()]
    return items[:limit] if limit else items


# ── The owner's own instructions (playbook_instructions; empty when not set) ──


INSTRUCTION_SCOPES = ("posts", "ads", "blog", "video", "replies")


_PLATFORM_ALIASES = {"x": "twitter", "x / twitter": "twitter", "twitter / x": "twitter", "ig": "instagram",
                     "fb": "facebook"}


_PLATFORM_LABELS = {"instagram": "Instagram", "facebook": "Facebook", "linkedin": "LinkedIn",
                    "twitter": "X (Twitter)", "tiktok": "TikTok", "pinterest": "Pinterest", "youtube": "YouTube"}


def _platform_key(p: str) -> str:
    k = str(p or "").strip().lower()
    return _PLATFORM_ALIASES.get(k, k)


def owner_instructions(bc: Optional[Dict[str, Any]], kind: str = "posts", platform: str = "") -> List[str]:
    """The owner's standing instructions that apply to this kind of writing
    (posts, ads, blog, video, replies) on this platform. With no platform
    (e.g. a calendar covering several), platform-specific ones are kept and
    say which platform they are for."""
    out: List[str] = []
    here = _platform_key(platform)
    for item in (bc or {}).get("playbook_instructions") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        scope = [str(x).lower() for x in (item.get("applies_to") or ["all"])]
        if "all" not in scope and kind not in scope:
            continue
        platforms = [_platform_key(x) for x in (item.get("platforms") or []) if str(x).strip()]
        if platforms:
            if here and here not in platforms:
                continue
            if not here:
                text = f"On {', '.join(_PLATFORM_LABELS.get(x, x) for x in platforms)}: {text}"
        out.append(text)
    return out


# ── Hard rules ──


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


def banned_pattern(word: str) -> "re.Pattern":
    """Matches a banned word in its common forms, or a banned phrase exactly.
    Banning "streamline" also has to catch "streamlined" and "streamlining" —
    seen on dev, where the exact-word check let those through — but must not
    catch unrelated words that merely contain it ("ok" in "book")."""
    w = word.strip().lower()
    if re.fullmatch(r"[a-z]{3,}", w):
        if w.endswith("e"):
            core = f"(?:{re.escape(w)}(?:s|d|r|rs)?|{re.escape(w[:-1])}(?:ing|ed|er|ers))"
        elif w.endswith("y"):
            core = f"(?:{re.escape(w)}|{re.escape(w[:-1])}(?:ies|ied|ier))"
        else:
            core = f"{re.escape(w)}(?:s|es|ed|ing|er|ers)?"
    else:
        core = re.escape(w)
    return re.compile(r"(?<!\w)" + core + r"(?!\w)")


def find_banned(text: str, words: Iterable[str]) -> List[str]:
    """Which of `words` appear in `text`, in any common form."""
    lowered = (text or "").lower()
    return [w for w in words if str(w).strip() and banned_pattern(str(w)).search(lowered)]


_NO_WORDS = ("no", "none", "never", "zero", "0", "off", "nil", "nothing", "don't", "dont", "avoid")


_FEW_WORDS = ("some", "minimal", "light", "sparing", "few", "little", "1", "2", "one", "two", "occasional", "rare")


def emoji_mode(bc: Optional[Dict[str, Any]]) -> str:
    """The Playbook's emoji setting as "no", "some", "free" (anything else the
    owner wrote) or "" (not set). The page's field is free text, so "None",
    "No emojis" and "never" all have to mean no — only the exact value "no"
    used to, and a brand that typed "None" still got emojis."""
    raw = str(((bc or {}).get("guardrails") or {}).get("emoji_usage") or "").strip().lower()
    if not raw:
        return ""
    words = re.findall(r"[a-z0-9']+", raw)
    if raw in ("yes", "any", "lots", "heavy", "plenty", "moderate"):
        return "free"
    if words and (words[0] in _NO_WORDS or raw.startswith("no ") or "none" in words or "never" in words):
        return "no"
    if any(w in _FEW_WORDS for w in words):
        return "some"
    return "free"


def languages_rule(bc: Optional[Dict[str, Any]]) -> Optional[str]:
    langs = _list((bc or {}).get("languages"))
    if not langs or langs == ["English"]:
        return None
    if len(langs) == 1:
        return f"Write in {langs[0]} — the whole piece, not English."
    others = ", ".join(langs[1:])
    # Two failures seen on dev, in order. "Naturally mix in" alone was ignored:
    # nine pieces in a row came back in plain English. Once it was made a
    # requirement, whole captions came back in Pidgin. So both ends are stated.
    return (f"Languages: {', '.join(langs)}. Write in {langs[0]} with a light, natural touch of {others}, "
            f"the way this audience actually speaks — not a translation. "
            f"REQUIRED: at least one genuine {others} phrase or sentence in every piece; a piece written only "
            f"in {langs[0]} is wrong. LIMIT: {langs[0]} stays the main language — at most about one sentence "
            f"in four carries any {others}; a piece written mostly in {others} is just as wrong.")


def hashtag_limit(bc: Optional[Dict[str, Any]]) -> Optional[int]:
    """The Playbook's "max hashtags" as a number; None when there is no limit."""
    raw = str(((bc or {}).get("guardrails") or {}).get("max_hashtags") or "").strip().lower()
    m = re.search(r"\d+", raw)
    if m:
        return int(m.group())
    if raw in ("none", "no", "zero", "no hashtags", "never") or raw.startswith("no hashtag"):
        return 0
    return None


def hard_rules(bc: Optional[Dict[str, Any]], captions: bool = False, language: bool = True,
               kind: str = "posts", platform: str = "") -> List[str]:
    """captions=True adds the caption-only format rules (emoji, hashtag limit).
    language=False for writers that must answer in the reader's own language
    (the inbox replies in the customer's language, not the brand's).
    kind/platform pick which of the owner's own instructions apply."""
    bc = bc or {}
    g = bc.get("guardrails") or {}
    rules: List[str] = []
    if g.get("avoid_topics"):
        rules.append(f"NEVER mention or reference: {g['avoid_topics']}.")
    words = banned_words(bc)
    if words:
        rules.append(f"NEVER use these words or phrases: {', '.join(words)}. "
                     f"This covers every form of each word (plural, -ed, -ing...).")
    if g.get("compliance_notes"):
        rules.append(f"Compliance requirement: {g['compliance_notes']}.")
    if language and languages_rule(bc):
        rules.append(languages_rule(bc))
    emoji = emoji_mode(bc)
    if emoji == "no":
        rules.append("Use NO emojis under any circumstances.")
    elif emoji == "some":
        rules.append("Use emojis very sparingly — maximum 1-2, only if they add genuine meaning.")
    elif emoji == "free":
        rules.append(f"Emoji usage: {g.get('emoji_usage')}.")
    if captions:
        limit = hashtag_limit(bc)
        raw = str(g.get("max_hashtags") or "").strip()
        if limit == 0:
            rules.append("Use NO hashtags.")
        elif limit:
            rules.append(f"Use a maximum of {limit} hashtags.")
        elif raw and raw.lower() not in ("no limit", "unlimited", "any"):
            rules.append(f"Hashtags: {raw}.")
    owner = owner_instructions(bc, kind=kind, platform=platform)
    if owner:
        rules.append("THE OWNER'S OWN INSTRUCTIONS — follow every one exactly, they override any default "
                     "style or format: " + " | ".join(owner))
    return rules

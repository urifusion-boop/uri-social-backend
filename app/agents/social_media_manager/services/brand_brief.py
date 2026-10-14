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
                  language. Checked again after writing with audit() and
                  applied with enforce().
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
    "PRIORITY when instructions conflict: (1) HARD RULES — including the owner's own instructions — are never broken; "
    "(2) BRAND VOICE decides how it sounds; (3) AUDIENCE adapts that voice, it never replaces it; "
    "(4) everything else."
)

# Seen in real output on dev: a caption invented "over 60% of small businesses
# lose track of contract renewal dates". A made-up number published under a
# brand's name is the brand's liability, so this is stated wherever the brief is.
TRUTH_RULE = (
    "NEVER invent facts: no made-up statistics, percentages, prices, customer names, results, awards "
    "or dates. Use only what is given here; if a claim needs a number you weren't given, make the point without one."
)

PULSE_FRESH_DAYS = 21    # same threshold the content calendar uses
PULSE_EXPIRY_DAYS = 45   # older than this, a "current" promotion is no longer mentioned at all


def _list(value: Any, limit: int = 0) -> List[str]:
    if isinstance(value, str):
        value = re.split(r"[,\n]", value)
    items = [str(v).strip() for v in (value or []) if str(v).strip()]
    return items[:limit] if limit else items


# ── 0. The owner's own instructions ──────────────────────────────────────────
# What the owner told Jane in their own words ("captions start with a one-line
# hook", "always write prices with the ₦ sign"), kept on the profile as
# playbook_instructions by playbook_instructions.py. Each says which kind of
# writing it is for and, optionally, which platforms.

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


_FORMAL_TONE = ("formal", "no slang", "professional", "corporate", "analytical")


# "End Instagram posts with 'DM us to start'" / "End Instagram posts with: DM us to start"
_ENDING = re.compile(r"\bend\b[^:'\"\u2018\u201c]{0,60}?(?:with|using)\s*:?\s*['\"\u2018\u201c]?([^'\"\u2019\u201d\n]{2,80}?)['\"\u2019\u201d]?\s*\.?\s*$",
                     re.IGNORECASE)


def platform_cta(bc: Optional[Dict[str, Any]], platform: str) -> str:
    """The call to action the owner set for this one platform ("Always end
    Instagram posts with 'DM us to start'"), or ""."""
    for item in (bc or {}).get("playbook_instructions") or []:
        if not isinstance(item, dict) or platform.lower() not in [str(x).lower() for x in item.get("platforms") or []]:
            continue
        m = _ENDING.search(str(item.get("text") or "").strip())
        if m and m.group(1).strip():
            return m.group(1).strip().rstrip(".")
    return ""


def for_platform(bc: Optional[Dict[str, Any]], platform: str = "") -> Optional[Dict[str, Any]]:
    """The brand as it should write on `platform`:
      • a call to action the owner set for this platform replaces the general
        one here, so the post ends with it alone (both were added, seen on dev)
      • a platform set to a formal / no-slang tone is written in the brand's
        main language only, without standing instructions about the other
        languages (Pidgin on a "formal, no slang" LinkedIn, seen on dev).
    Everything else is unchanged."""
    if not bc or not platform:
        return bc
    out = dict(bc)
    own_cta = platform_cta(bc, platform)
    if own_cta:
        out["cta_styles"] = [own_cta]
    langs = _list(bc.get("languages"))
    if len(langs) < 2 or bc.get("same_tone_everywhere", True) is not False:
        return out
    tone = str((bc.get("platform_tones") or {}).get(platform.lower()) or "").lower()
    if not (tone and any(word in tone for word in _FORMAL_TONE)):
        return out
    # Standing instructions about the dropped languages ("mix in a little
    # Pidgin") would contradict the tone the same way — unless the owner named
    # this platform in them, which is then their explicit choice.
    dropped = [lang.lower() for lang in langs[1:]]
    out["languages"] = [langs[0]]
    out["playbook_instructions"] = [
        i for i in bc.get("playbook_instructions") or []
        if not (isinstance(i, dict) and platform.lower() not in (i.get("platforms") or [])
                and any(lang in str(i.get("text", "")).lower() for lang in dropped))]
    return out


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


def violations(text: str, bc: Optional[Dict[str, Any]]) -> List[str]:
    """Banned words/phrases that appear in `text` (whole words, any case).
    For checking a piece of writing after the model produced it."""
    return find_banned(text, banned_words(bc))


# ── 1b. After writing: check, then enforce ───────────────────────────────────
# Asking for a rule is not the same as getting it. On dev, with every rule in
# the prompt, captions still came back with an invented "Over 70% of small
# businesses...", no call to action and no Pidgin. audit() finds what a piece
# got wrong so it can be rewritten; enforce() then applies what can be applied
# with certainty, without a model.

_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U000023E9-\U000023FA\U0000FE0F\U0000200D]"
)


def strip_emojis(text: str) -> str:
    if not _EMOJI.search(text or ""):
        return text or ""  # nothing to remove — leave the layout exactly as written
    out = _EMOJI.sub("", text or "")
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"[ \t]+([.,!?])", r"\1", out)
    return "\n".join(line.strip() for line in out.split("\n")).strip()


def hashtag_limit(bc: Optional[Dict[str, Any]]) -> Optional[int]:
    """The Playbook's "max hashtags" as a number; None when there is no limit."""
    raw = str(((bc or {}).get("guardrails") or {}).get("max_hashtags") or "").strip().lower()
    m = re.search(r"\d+", raw)
    if m:
        return int(m.group())
    if raw in ("none", "no", "zero", "no hashtags", "never") or raw.startswith("no hashtag"):
        return 0
    return None


def limit_hashtags(hashtags: Optional[List[str]], bc: Optional[Dict[str, Any]]) -> List[str]:
    limit = hashtag_limit(bc)
    tags = list(hashtags or [])
    return tags if limit is None else tags[:limit]


def ctas(bc: Optional[Dict[str, Any]]) -> List[str]:
    """The call(s) to action this piece may end with: a one-off override, else
    the one already used on the image, else the Playbook's own."""
    bc = bc or {}
    for key in ("override_cta", "selected_cta"):
        if bc.get(key):
            return [str(bc[key]).strip()]
    return _list(bc.get("cta_styles"))


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


_CTA_FILLER = {"a", "an", "the", "your", "our", "us", "to", "now", "today", "for", "of", "and", "with", "on", "in"}


def has_cta(text: str, bc: Optional[Dict[str, Any]]) -> bool:
    """True if the piece carries one of the brand's CTAs: the exact phrase
    anywhere, or all of its meaningful words in the closing lines ("Book your
    free demo today" satisfies "Book a free demo")."""
    wanted = [c for c in ctas(bc) if _norm(c)]
    if not wanted:
        return True
    whole = _norm(text)
    closing = [l for l in (text or "").split("\n") if l.strip() and not l.strip().startswith("#")][-2:]
    closing_words = set(_norm(" ".join(closing)).split())
    for cta in wanted:
        if _norm(cta) in whole:
            return True
        words = [w for w in _norm(cta).split() if w not in _CTA_FILLER]
        if words and all(w in closing_words for w in words):
            return True
    return False


# Words that only appear when a piece really uses the language. Enough to tell
# "none at all" from "some" — it triggers a rewrite, it doesn't grade fluency.
# A language with no entry here is simply not checked.
_LANGUAGE_MARKERS = {
    "pidgin": r"\b(dey|abeg|wahala|wetin|una|sabi|na|no be|go fit|make we|make una|we go|e go|don dey|"
              r"sef|sha|oga|sharp sharp|how far|comot|wey|dem|chop|shey|abi|pikin|wan|naija|omo)\b",
    "yoruba": r"[ẹọṣ]|\b(e ku|bawo|jowo|e se|pele|oya|owo|aje|ore mi|a dupe|e kaaro|e kaasan|ise|ile wa)\b",
    "igbo": r"[ịụṅ]|\b(ndewo|biko|daalu|kedu|nnoo|nwanne|ego|ahia|imela|ka anyi|unu|nke oma)\b",
    "hausa": r"[ɓɗƙ]|\b(sannu|nagode|na gode|yaya|barka|kasuwa|kudi|yauwa|madalla|ina kwana|don allah|ku zo)\b",
}


def missing_languages(text: str, bc: Optional[Dict[str, Any]]) -> List[str]:
    """Languages the brand mixes in that this piece doesn't use at all."""
    langs = _list((bc or {}).get("languages"))
    if len(langs) < 2:
        return []
    lowered = (text or "").lower()
    missing = []
    for lang in langs[1:]:
        key = next((k for k in _LANGUAGE_MARKERS if k in lang.lower()), None)
        if key and not re.search(_LANGUAGE_MARKERS[key], lowered):
            missing.append(lang)
    return missing


def overused_languages(text: str, bc: Optional[Dict[str, Any]]) -> List[str]:
    """Secondary languages that have taken the piece over: the brand asked for
    a blend with its first language as the main one, not a piece written in the
    second. Counted by sentence, and only for pieces long enough to judge."""
    langs = _list((bc or {}).get("languages"))
    if len(langs) < 2:
        return []
    sentences = [x for x in re.split(r"(?<=[.!?])\s+|\n+", (text or "").lower()) if len(x.split()) >= 3]
    if len(sentences) < 4:
        return []
    over = []
    for lang in langs[1:]:
        key = next((k for k in _LANGUAGE_MARKERS if k in lang.lower()), None)
        if not key:
            continue
        hits = sum(1 for x in sentences if re.search(_LANGUAGE_MARKERS[key], x))
        if hits / len(sentences) > 0.5:
            over.append(lang)
    return over


_STATISTIC = re.compile(
    r"(\d+(?:[.,]\d+)?)\s?(?:%|percent\b|per cent\b)"
    r"|\b(\d+)\s+(?:out of|in every|in)\s+(\d+)\b"
    r"|\b(\d+)x\b", re.I)


def unverified_numbers(text: str, sources: str) -> List[str]:
    """Statistics in `text` (percentages, "3 in 5", "10x") whose numbers appear
    nowhere in what the writer was given — i.e. probably made up."""
    known = set(re.findall(r"\d+(?:[.,]\d+)?", sources or ""))
    found: List[str] = []
    for m in _STATISTIC.finditer(text or ""):
        numbers = [g for g in m.groups() if g]
        phrase = m.group(0).strip()
        if phrase.replace(" ", "").lower() in ("100%", "100percent"):
            continue  # "100% Nigerian" is an expression, not a statistic
        if any(n not in known for n in numbers) and phrase not in found:
            found.append(phrase)
    return found


_CITATION = re.compile(
    r"\b(?:according to (?!our\b|the brand\b)[a-z]"
    r"|(?:stud(?:y|ies)|surveys?|reports?|research|polls?)\s+(?:by|conducted|published|shows?|showed|found|finds|"
    r"highlight(?:s|ed)|reveal(?:s|ed)|suggest(?:s|ed)?|indicate(?:s|d)?|confirm(?:s|ed)?)\b"
    r"|(?:statistics|data|experts?|analysts?)\s+(?:shows?|says?|suggests?|indicates?|reveals?|agree|believe))", re.I)


def unverified_sources(text: str, sources: str) -> List[str]:
    """References to a study, survey, report or "experts" that the writer was
    never given — e.g. "a study by the International Finance Corporation
    highlighted..." (seen in a blog on dev). No number, so the statistics check
    can't see it, but it is an invented fact all the same."""
    given = (sources or "").lower()
    found: List[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        m = _CITATION.search(sentence)
        # "Our reports show your spend" / "your data shows up here" is the
        # product talking about itself, not a citation.
        if m and re.search(r"(?:our|your|my|their|its|these|those)\s+(?:\w+\s+)?$", sentence[:m.start()], re.I):
            continue
        if m and m.group(0).lower() not in given:
            snippet = sentence.strip()
            snippet = snippet if len(snippet) <= 140 else snippet[:137].rstrip() + "..."
            if snippet not in found:
                found.append(snippet)
    return found


def audit(text: str, bc: Optional[Dict[str, Any]], sources: str = "") -> List[Dict[str, str]]:
    """What a finished piece got wrong against the Playbook. Each issue says how
    to fix it, so the rewrite is specific. `sources` is everything the writer
    was given (topic + brand), used to tell real numbers from invented ones."""
    bc = bc or {}
    issues: List[Dict[str, str]] = []
    for word in violations(text, bc):
        issues.append({"code": "banned_word", "detail": word,
                       "fix": f'Remove "{word}" in every form (plural, -ed, -ing) and say it another way.'})
    for phrase in unverified_numbers(text, sources):
        issues.append({"code": "unverified_number", "detail": phrase,
                       "fix": f'Remove "{phrase}" — that figure was never provided, so it is invented. '
                              f'Make the same point without a number.'})
    # Only a brand that has set up its Playbook gets this check, like the rest.
    for sentence in (unverified_sources(text, sources) if bc else []):
        issues.append({"code": "unverified_source", "detail": sentence,
                       "fix": f'This sentence cites a study, report or experts nobody provided: "{sentence}" '
                              f"Remove the citation and make the point as the brand's own observation."})
    langs = _list(bc.get("languages"))
    for lang in missing_languages(text, bc):
        issues.append({"code": "missing_language", "detail": lang,
                       "fix": f"Add ONE short, natural {lang} phrase (a few words) — this brand's audience mixes "
                              f"{' and '.join(langs)}. Keep every other sentence in {langs[0]}."})
    for lang in overused_languages(text, bc):
        issues.append({"code": "language_overused", "detail": lang,
                       "fix": f"Too much {lang}: {langs[0]} is this brand's main language. Rewrite it in "
                              f"{langs[0]} and keep {lang} to one or two short phrases."})
    if not has_cta(text, bc):
        cta = ctas(bc)[0]
        issues.append({"code": "missing_cta", "detail": cta,
                       "fix": f'End with this call-to-action as the final line: "{cta}".'})
    return issues


def enforce(text: str, bc: Optional[Dict[str, Any]], append_cta: bool = True) -> str:
    """The rules that can be guaranteed without a model. Runs last, so whatever
    the writer or the rewrite did, these hold on what is saved:
      • "no emojis" — every emoji is removed
      • the brand's call-to-action closes the piece (append_cta=False where a
        line can't simply be added, e.g. a tweet thread)."""
    bc = bc or {}
    out = text or ""
    if emoji_mode(bc) == "no":
        out = strip_emojis(out)
    if append_cta and out.strip() and not has_cta(out, bc):
        cta = ctas(bc)[0].strip()
        if cta[-1:] not in ".!?":
            cta += "."
        out = f"{out.rstrip()}\n\n{cta}"
    return out


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
    kind: str = "posts",
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
        block("HARD RULES (never break these):",
              hard_rules(bc, captions=captions, language=language, kind=kind, platform=platform))
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
    header = f"BRAND BRIEF{f' — {name}' if name else ''}\n{PRIORITY_ORDER}\n{TRUTH_RULE}"
    return header + "\n\n" + "\n\n".join(parts)

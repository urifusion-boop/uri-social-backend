"""
"Tell Jane" — the owner describes what they want in their own words and the
Brand Playbook updates itself.

    "Keep my captions short, no emojis, and always end with 'Order on WhatsApp'.
     On LinkedIn be more formal."

One model call turns that into:
  • changes to the Playbook's own fields where one fits (emoji usage → none,
    CTA → "Order on WhatsApp", LinkedIn tone → formal, caption length → short);
  • standing instructions for everything that has no field ("start every
    caption with a one-line hook in capitals"), kept on the profile as
    playbook_instructions and stated to every writer through the Brand Brief
    (brand_brief.owner_instructions), scoped to posts / ads / blog / video /
    replies and optionally to platforms.

The model only proposes. Every change is validated here against FIELDS (known
field, allowed value, sane size) before anything is written, the before/after
of each touched field is logged in playbook_changes, and any update can be
undone in one step. Newer instructions replace the older ones they contradict,
so the list stays a true picture of what the owner wants now.
"""
from __future__ import annotations

import json
import re
import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from motor.motor_asyncio import AsyncIOMotorDatabase

CHANGES_COLLECTION = "playbook_changes"
USAGE_COLLECTION = "playbook_instruct_usage"
MAX_INSTRUCTIONS = 40
MAX_TEXT = 2000
PER_DAY = 60
MODEL = "gpt-4.1"

SCOPES = ("all", "posts", "ads", "blog", "video", "replies")
PLATFORMS = ("instagram", "facebook", "linkedin", "twitter", "tiktok", "pinterest", "youtube")
TIME_SLOTS = ("Early Morning (6-9am)", "Late Morning (9am-12pm)", "Afternoon (12-4pm)", "Evening (4-8pm)",
              "Night (8pm-12am)")

# Every field the owner can change by describing it. kind: text | list | enum | bool | slots.
# Labels are the ones the Playbook page shows, so the summary reads the same.
FIELDS: Dict[str, Dict[str, Any]] = {
    "derived_voice": {"label": "Brand voice", "kind": "text",
                      "hint": "how the brand sounds overall, e.g. 'warm, witty, plain-spoken'"},
    "tagline": {"label": "Tagline", "kind": "text"},
    "product_description": {"label": "Description", "kind": "text", "hint": "what the business does"},
    "key_products_services": {"label": "Key products / services", "kind": "list"},
    "price_range": {"label": "Price range", "kind": "text"},
    "unique_selling_proposition": {"label": "What makes you different", "kind": "text"},
    "ideal_customer_profile": {"label": "Ideal customer", "kind": "text"},
    "audience_age_range": {"label": "Audience age", "kind": "text",
                           "hint": "e.g. 'Millennials (25-40), Gen X (41-56)'"},
    "primary_goal": {"label": "Primary goal", "kind": "text"},
    "customer_pain_points": {"label": "Customer pain points", "kind": "list"},
    "customer_needs": {"label": "Customer needs", "kind": "list"},
    "customer_objections": {"label": "Common objections", "kind": "list"},
    "why_customers_choose_us": {"label": "Why customers choose you", "kind": "text"},
    "common_questions": {"label": "Common questions customers ask", "kind": "list"},
    "words_to_avoid": {"label": "Words/phrases to avoid", "kind": "list",
                       "hint": "any word or phrase the owner never wants used"},
    "competitor_handles": {"label": "Competitors", "kind": "list"},
    "region": {"label": "Region", "kind": "text"},
    "languages": {"label": "Languages", "kind": "list",
                  "hint": "first one is the main language, e.g. ['English', 'Pidgin']"},
    "content_pillars": {"label": "Content pillars", "kind": "list"},
    "preferred_formats": {"label": "Preferred formats", "kind": "list"},
    "cta_styles": {"label": "Call to action", "kind": "list",
                   "hint": "the exact phrases posts end with, e.g. 'Order on WhatsApp'"},
    "default_link": {"label": "Default link", "kind": "text"},
    "posting_cadence": {"label": "Posting cadence", "kind": "enum",
                        "options": ["Daily", "3–4x per week", "2–3x per week", "Weekly", "Bi-weekly"]},
    "approval_workflow": {"label": "Approval workflow", "kind": "enum", "options": ["review", "auto", "hybrid"],
                          "hint": "review = every post waits for approval; auto = Auto-tab posts publish on "
                                  "schedule; hybrid = Auto-tab posts publish, the owner's own wait"},
    "posting_time_prefs.general": {"label": "Preferred posting times", "kind": "slots", "options": list(TIME_SLOTS)},
    **{f"posting_time_prefs.{p}": {"label": f"Posting times on {p.title() if p != 'twitter' else 'X'}",
                                   "kind": "slots", "options": list(TIME_SLOTS)}
       for p in ("instagram", "facebook", "linkedin", "twitter", "tiktok")},
    **{f"platform_tones.{p}": {"label": f"Tone on {p.title() if p != 'twitter' else 'X'}"
                                       .replace("Linkedin", "LinkedIn").replace("Tiktok", "TikTok"),
                               "kind": "text"}
       for p in ("instagram", "facebook", "linkedin", "twitter", "tiktok")},
    "guardrails.avoid_topics": {"label": "Topics to avoid", "kind": "text"},
    "guardrails.compliance_notes": {"label": "Compliance notes", "kind": "text",
                                    "hint": "legal/regulatory rules, e.g. 'never promise returns'"},
    "guardrails.emoji_usage": {"label": "Emoji usage", "kind": "enum", "options": ["no", "some", "yes"],
                               "hint": "no = none at all; some = 1-2; yes = freely"},
    "guardrails.max_hashtags": {"label": "Max hashtags", "kind": "enum",
                                "options": ["0", "1", "2", "3", "4", "5", "6", "8", "10", "15", "No limit"]},
    "voice_profile.caption_length": {"label": "Caption length", "kind": "enum",
                                     "options": ["one_liner", "short", "medium", "storytelling"],
                                     "hint": "one_liner = 1-2 lines; short = 3-5; medium = 5-8; storytelling = 8-15"},
    "voice_profile.formality": {"label": "Formality", "kind": "enum",
                                "options": ["formal", "semi-formal", "casual", "very-casual"]},
    "voice_profile.sentence_style": {"label": "Sentence style", "kind": "enum",
                                     "options": ["short_punchy", "mixed_rhythm", "long_flowing", "fragments"]},
    "voice_profile.hook_style": {"label": "Opening line", "kind": "enum",
                                 "options": ["question", "bold_statement", "relatable_observation", "direct_address",
                                             "controversial", "number_stat"]},
    "voice_profile.cta_style": {"label": "Call-to-action style", "kind": "enum",
                                "options": ["direct", "soft", "question", "urgent"]},
    "voice_profile.humor_level": {"label": "Humour", "kind": "enum",
                                  "options": ["none", "dry", "witty", "playful", "chaotic"]},
}

_VALUE_LABELS = {
    "review": "Review first", "auto": "Auto-publish", "hybrid": "Hybrid",
    "no": "None", "some": "A few (1–2)", "yes": "Freely",
    "one_liner": "One-liner", "short": "Short (3–5 lines)", "medium": "Medium (5–8 lines)",
    "storytelling": "Storytelling (8–15 lines)", "short_punchy": "Short and punchy", "mixed_rhythm": "Mixed",
    "long_flowing": "Long and flowing", "fragments": "Fragments", "bold_statement": "A bold statement",
    "relatable_observation": "Something relatable", "direct_address": "Speaking to the reader",
    "number_stat": "A number", "very-casual": "Very casual", "semi-formal": "Semi-formal",
}


# ── The model's job ──────────────────────────────────────────────────────────

def _schema_text() -> str:
    lines = []
    for key, f in FIELDS.items():
        desc = f"- {key} ({f['kind']})"
        if f.get("options"):
            desc += f" one of {f['options']}" if f["kind"] == "enum" else f" any of {f['options']}"
        if f.get("hint"):
            desc += f" — {f['hint']}"
        lines.append(desc)
    return "\n".join(lines)


def _current_text(profile: Dict[str, Any]) -> str:
    current = {}
    for key in FIELDS:
        value = get_path(profile, key)
        if value not in (None, "", [], {}):
            current[key] = value
    return json.dumps(current, ensure_ascii=False, default=str)[:6000]


SYSTEM_PROMPT = """You update a business's Brand Playbook from what the owner tells you, in their own words. \
The Playbook is what an AI assistant ("Jane") follows whenever she writes for this business: social posts, \
carousels, the content calendar, ads, blog posts, video scripts and replies to customer messages.

Turn the owner's message into changes. Return ONLY a JSON object, filling "clauses" FIRST:
{
  "clauses": [{"said": "<one thing the owner asked for, in their words>", "handled_by": "<field key> | remember | not_actionable"}],
  "understood": true|false,
  "changes": [{"field": "<field key>", "op": "set"|"add"|"remove", "value": <value>}],
  "remember": [{"text": "<instruction>", "applies_to": ["posts"|"ads"|"blog"|"video"|"replies"|"all"], "platforms": []}],
  "forget": ["<id of an existing instruction>"],
  "reply": "<one or two short sentences to the owner saying what you changed, in plain words>",
  "not_a_preference": "content_request"|"question"|"vague"|null,
  "question": "<only when the request is vague: one short, specific question>"
}

RULES
1. Use a Playbook FIELD whenever the request maps cleanly to one (no emojis → guardrails.emoji_usage "no"; \
"stop saying 'game-changer'" → words_to_avoid add; "end posts with 'Order on WhatsApp'" → cta_styles; \
"be formal on LinkedIn" → platform_tones.linkedin; "keep captions short" → voice_profile.caption_length). \
For list fields use "add"/"remove" with a string or list; "set" replaces the whole list — only when the \
owner clearly wants to replace it. For text and enum fields use "set". To clear a text field set "". \
Emit a change for EVERYTHING the owner states that a field covers, even if you think it is already set or \
is the default — what the owner says must be saved explicitly. A tone for one platform ("formal on \
LinkedIn", "playful on Instagram") ALWAYS goes in platform_tones.<platform>, described in a few words.
2. Everything about HOW content should be written that has no field (structure, format, things to always or \
never do, how to talk about the product, how to address customers) goes in "remember". Write each as one \
clear, self-contained instruction for the writer — e.g. "Start every caption with a one-line hook in capital \
letters." Keep the owner's exact words, phrases, names, prices and numbers. One idea per instruction. \
NEVER also add a "remember" instruction for something a field change already covers (hook style, caption \
length, emoji usage, a platform's tone, words to avoid) — that would say the same thing twice.
3. applies_to: "posts" = social captions, carousels and calendar posts; "replies" = answers to customer DMs and \
comments; "all" when the owner speaks about the brand in general ("we never…", "always…"). If they say \
"captions"/"posts" use posts. platforms only when the owner names one (instagram, facebook, linkedin, twitter, \
tiktok, pinterest, youtube); otherwise [].
4. EXISTING INSTRUCTIONS are listed with ids. Put an id in "forget" when the new message replaces, \
contradicts or cancels it (e.g. a newer "long captions" replaces an older "short captions"; "stop doing X" \
removes the instruction to do X). Never repeat an instruction that already exists.
5. Change ONLY what the owner asked about. Never invent business facts, prices, offers or claims. Never touch \
a field because you think it could be better.
6. If the message is not a preference, set understood false and make no changes: not_a_preference = \
"content_request" when they ask for a piece of content ("write a post about our sale"), "question" for a \
question or small talk. If it is a preference but too vague to act on ("make it better"), set \
not_a_preference = "vague" and ask one short, specific question with two or three concrete options.
7. Split the message into "clauses" — one per thing asked for — and handle EVERY clause: each one must \
appear in "changes" (as the field named in handled_by) or in "remember". "Keep captions short" is a clause and \
must produce voice_profile.caption_length "short" even if that might already be the case.
8. Call to action: "always end with X", "our CTA is X", "use X as the call to action" → cta_styles op "set" \
to [X] (posts rotate through the list, so anything else stays in the rotation); "also use X" → op "add". \
cta_styles applies to EVERY platform: when the owner names one platform ("end Instagram posts with X"), do NOT \
touch cta_styles — put it in "remember" with that platform in "platforms".
9. reply: describe ONLY what is in changes, remember and forget — never claim something you did not change. \
Talk to the owner ("Done — captions will be short, …"). Mention anything significant, e.g. that \
Auto-tab posts will now publish without review.

FIELDS
""" + _schema_text()


async def _ask_model(text: str, profile: Dict[str, Any]) -> Dict[str, Any]:
    from openai import AsyncOpenAI
    from app.core.config import settings

    existing = [{"id": i.get("id"), "text": i.get("text"), "applies_to": i.get("applies_to"),
                 "platforms": i.get("platforms")}
                for i in (profile.get("playbook_instructions") or []) if isinstance(i, dict)]
    user = (f"BUSINESS: {profile.get('brand_name') or 'this business'} ({profile.get('industry') or 'unknown industry'})\n"
            f"CURRENT PLAYBOOK VALUES: {_current_text(profile)}\n"
            f"EXISTING INSTRUCTIONS: {json.dumps(existing, ensure_ascii=False)}\n\n"
            f"THE OWNER SAYS:\n{text}")
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    resp = await client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
        temperature=0.1,
        response_format={"type": "json_object"},
        max_tokens=1500,
    )
    return json.loads(resp.choices[0].message.content or "{}")


# ── Paths and values ─────────────────────────────────────────────────────────

def get_path(profile: Dict[str, Any], key: str) -> Any:
    top, _, sub = key.partition(".")
    value = (profile or {}).get(top)
    if not sub:
        return value
    return (value or {}).get(sub) if isinstance(value, dict) else None


def _clean_text(value: Any, limit: int = 1000) -> str:
    return re.sub(r"\s+", " ", str(value if value is not None else "")).strip()[:limit]


def _as_list(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out, seen = [], set()
    for v in value:
        t = _clean_text(v, 200)
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:30]


def _slots_list(value: Any) -> List[str]:
    return [s for s in (value or "").split(",") if s.strip()] if isinstance(value, str) else []


def _match_option(value: Any, options: List[str]) -> Optional[str]:
    v = _clean_text(value).lower()
    for o in options:
        if v == o.lower():
            return o
    return None


def _new_value(key: str, op: str, value: Any, current: Any) -> Tuple[bool, Any]:
    """(ok, new value) for one proposed change; ok False means it is rejected."""
    f = FIELDS[key]
    kind = f["kind"]
    if kind == "text":
        if op != "set":
            return False, None
        return True, _clean_text(value)
    if kind == "enum":
        option = _match_option(value, f["options"])
        return (option is not None and op == "set"), option
    if kind == "bool":
        return isinstance(value, bool), value
    if kind == "slots":
        chosen = [o for o in f["options"] if any(_match_option(v, [o]) for v in _as_list(value))]
        have = [s for s in _slots_list(current)]
        if op == "add":
            chosen = have + [c for c in chosen if c not in have]
        elif op == "remove":
            chosen = [h for h in have if h not in chosen]
        elif op != "set":
            return False, None
        return True, (f"{','.join(chosen)}," if chosen else "")
    # list
    items = _as_list(value)
    have = _as_list(current)
    if op == "set":
        return True, items
    if op == "add":
        return bool(items), have + [i for i in items if i.lower() not in {h.lower() for h in have}]
    if op == "remove":
        drop = {i.lower() for i in items}
        return bool(items), [h for h in have if h.lower() not in drop]
    return False, None


_FIELD_VALUE_LABELS = {
    ("voice_profile.hook_style", "question"): "A question",
    ("voice_profile.hook_style", "controversial"): "A contrarian take",
    ("voice_profile.cta_style", "question"): "A question",
    ("voice_profile.humor_level", "none"): "None",
}


def _display(key: str, value: Any) -> str:
    if (key, str(value)) in _FIELD_VALUE_LABELS:
        return _FIELD_VALUE_LABELS[(key, str(value))]
    if value in (None, "", [], {}):
        return "—"
    if FIELDS.get(key, {}).get("kind") == "slots":
        return ", ".join(_slots_list(value)) or "—"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if key == "guardrails.max_hashtags" and str(value) == "0":
        return "None"
    text = _VALUE_LABELS.get(str(value), str(value))
    return text[:1].upper() + text[1:] if FIELDS.get(key, {}).get("kind") == "enum" else text


# ── Turning the model's proposal into a safe update ──────────────────────────

_PLATFORM_WORDS = {
    "instagram": ("instagram", " ig "), "facebook": ("facebook", " fb "), "linkedin": ("linkedin",),
    # X only when clearly the platform ("on X", "X posts"), never a stray letter.
    "twitter": ("twitter", " on x ", " for x ", " x posts ", " x post ", " to x "),
    "tiktok": ("tiktok",), "pinterest": ("pinterest",), "youtube": ("youtube",),
}


def platforms_named(text: str) -> List[str]:
    """Platforms the owner names in their message."""
    t = f" {re.sub(r'[^a-z0-9]+', ' ', (text or '').lower())} "
    return [p for p, words in _PLATFORM_WORDS.items() if any(w in t for w in words)]


def scope_platform_ctas(proposal: Dict[str, Any], text: str) -> Dict[str, Any]:
    """cta_styles applies to every platform. A call to action the owner asked
    for on named platforms only ("end Instagram posts with X") becomes a
    standing instruction for those platforms instead of changing it everywhere."""
    named = platforms_named(text)
    if not named:
        return proposal
    changes, remember = [], list(proposal.get("remember") or [])
    for ch in proposal.get("changes") or []:
        if isinstance(ch, dict) and ch.get("field") == "cta_styles" and str(ch.get("op", "set")).lower() != "remove":
            values = ch.get("value") if isinstance(ch.get("value"), list) else [ch.get("value")]
            for v in values:
                v = _clean_text(v, 200)
                if v:
                    remember.append({"text": f"End {', '.join(p.title() for p in named)} posts with: {v}",
                                     "applies_to": ["posts"], "platforms": named})
            continue
        changes.append(ch)
    return {**proposal, "changes": changes, "remember": remember}


# More than this many fields emptied by one message is treated as a mistake:
# a casual "clear everything" must never wipe a Playbook in one go.
MAX_CLEARED_AT_ONCE = 3
_EMPTY_SHOWN = ("—", "", "None", "none")


def plan_update(proposal: Dict[str, Any], profile: Dict[str, Any], now: Optional[datetime] = None
                ) -> Dict[str, Any]:
    """Validate the model's proposal against FIELDS and the current profile.
    Returns {"set": {top_level_key: new_value}, "changes": [human summary],
    "added": [instructions], "removed": [instructions]}. Nothing is written."""
    now = now or datetime.utcnow()
    working: Dict[str, Any] = {}

    def current_top(top: str) -> Any:
        if top not in working:
            working[top] = deepcopy((profile or {}).get(top))
        return working[top]

    summary: List[Dict[str, str]] = []
    for ch in proposal.get("changes") or []:
        if not isinstance(ch, dict):
            continue
        key, op = str(ch.get("field") or ""), str(ch.get("op") or "set").lower()
        if key not in FIELDS:
            continue
        top, _, sub = key.partition(".")
        before = get_path({top: current_top(top)}, key)
        ok, after = _new_value(key, op, ch.get("value"), before)
        if not ok or after == before:
            continue
        if sub:
            container = current_top(top)
            container = dict(container) if isinstance(container, dict) else {}
            container[sub] = after
            working[top] = container
        else:
            working[top] = after
        summary.append({"field": key, "label": FIELDS[key]["label"],
                        "before": _display(key, before), "after": _display(key, after)})

    touched = {c["field"] for c in summary}
    # Keep related settings consistent with what was changed.
    if any(k.startswith("platform_tones.") for k in touched):
        if (profile or {}).get("same_tone_everywhere", True) is not False:
            working["same_tone_everywhere"] = False
    if any(k.startswith("posting_time_prefs.") for k in touched):
        mode = "general" if "posting_time_prefs.general" in touched else "custom"
        if (profile or {}).get("posting_time_mode") != mode:
            working["posting_time_mode"] = mode
    if "guardrails.emoji_usage" in touched or "guardrails.max_hashtags" in touched:
        vp = dict(current_top("voice_profile") or {})
        g = working.get("guardrails") or {}
        if g.get("emoji_usage") == "no":
            vp["emoji_usage"] = "none"
        elif g.get("emoji_usage") == "some":
            vp["emoji_usage"] = "light"
        if str(g.get("max_hashtags")) == "0":
            vp["hashtag_style"] = "none"
        working["voice_profile"] = vp

    # Standing instructions.
    existing = [i for i in (profile or {}).get("playbook_instructions") or [] if isinstance(i, dict)]
    forget = {str(x) for x in (proposal.get("forget") or [])}
    removed = [i for i in existing if str(i.get("id")) in forget]
    kept = [i for i in existing if str(i.get("id")) not in forget]
    added: List[Dict[str, Any]] = []
    known = {re.sub(r"\W+", " ", str(i.get("text", "")).lower()).strip() for i in kept}
    for r in proposal.get("remember") or []:
        if not isinstance(r, dict):
            continue
        text = _clean_text(r.get("text"), 500)
        norm = re.sub(r"\W+", " ", text.lower()).strip()
        if len(text) < 4 or norm in known:
            continue
        scope = [s for s in (str(x).lower() for x in (r.get("applies_to") or [])) if s in SCOPES] or ["posts"]
        if "all" in scope:
            scope = ["all"]
        platforms = []
        for p in r.get("platforms") or []:
            k = str(p).strip().lower()
            k = {"x": "twitter", "ig": "instagram", "fb": "facebook"}.get(k, k)
            if k in PLATFORMS and k not in platforms:
                platforms.append(k)
        known.add(norm)
        added.append({"id": f"ins_{uuid.uuid4().hex[:10]}", "text": text, "applies_to": scope,
                      "platforms": platforms, "created_at": now})
    if removed or added:
        working["playbook_instructions"] = kept + added

    sets = {k: v for k, v in working.items() if v != (profile or {}).get(k)}
    return {"set": sets, "changes": summary, "added": added, "removed": removed}


# ── Public operations ────────────────────────────────────────────────────────

_HOW_TO = ("Tell Jane how you'd like your content written or what to always or never do — e.g. \"Keep captions "
           "short and never use emojis\" or \"Be more formal on LinkedIn\".")


class InstructError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _scope(profile: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """The brand_profiles document this profile is (older personal profiles carry no brand_id)."""
    if profile.get("brand_id"):
        return {"brand_id": profile["brand_id"]}
    return {"user_id": user_id, "brand_id": {"$exists": False}}


async def _count_today(db: AsyncIOMotorDatabase, user_id: str) -> int:
    key = f"{user_id}:{datetime.utcnow():%Y-%m-%d}"
    doc = await db[USAGE_COLLECTION].find_one_and_update(
        {"_id": key}, {"$inc": {"count": 1}, "$setOnInsert": {"created_at": datetime.utcnow()}},
        upsert=True, return_document=True,
    )
    return int((doc or {}).get("count") or 1)


async def instruct(db: AsyncIOMotorDatabase, user_id: str, brand_id: Optional[str], profile: Dict[str, Any],
                   text: str, source: str = "playbook", ask=None) -> Dict[str, Any]:
    """Understand `text`, apply the validated changes, log them. Returns
    {understood, reply, changes, added, removed, change_id, profile_updates}."""
    text = (text or "").strip()
    if len(text) < 3:
        raise InstructError("Tell Jane what you'd like — e.g. \"Keep captions short and never use emojis\".")
    if len(text) > MAX_TEXT:
        raise InstructError(f"That's a lot at once — keep it under {MAX_TEXT} characters.")
    if await _count_today(db, user_id) > PER_DAY:
        raise InstructError("You've made a lot of changes today — try again tomorrow.", status=429)

    try:
        proposal = await (ask or _ask_model)(text, profile)
    except Exception as e:  # model unavailable or returned garbage
        print(f"[PlaybookInstruct] model call failed: {e}")
        raise InstructError("Jane couldn't read that just now — please try again in a minute.", status=503)

    if not proposal.get("understood", True) and not proposal.get("changes") and not proposal.get("remember"):
        kind = proposal.get("not_a_preference")
        if kind == "content_request":
            reply = ("This box changes how Jane writes for you — to make a post, use Create Content. Here you can "
                     "say things like \"Always mention free delivery\" or \"Keep captions under 3 lines\".")
        elif kind == "vague":
            reply = _clean_text(proposal.get("question"), 600) or _HOW_TO
        else:
            reply = _HOW_TO
        return {"understood": False, "reply": reply, "changes": [], "added": [], "removed": [], "change_id": None}

    proposal = scope_platform_ctas(proposal, text)
    plan = plan_update(proposal, profile)
    cleared = [c for c in plan["changes"] if c["after"] in _EMPTY_SHOWN and c["before"] not in _EMPTY_SHOWN]
    if len(cleared) > MAX_CLEARED_AT_ONCE:
        names = ", ".join(c["label"] for c in cleared[:5]) + (" and more" if len(cleared) > 5 else "")
        print(f"[PlaybookInstruct] refused to clear {len(cleared)} fields at once: {[c['field'] for c in cleared]}")
        return {"understood": False, "changes": [], "added": [], "removed": [], "change_id": None,
                "reply": (f"That would clear {len(cleared)} parts of your Playbook ({names}), so I haven't changed "
                          "anything. Tell me the specific parts to clear — for example \"remove our tagline\" — "
                          "or edit them in the Playbook below.")}
    print(f"[PlaybookInstruct] clauses={json.dumps(proposal.get('clauses'), ensure_ascii=False)[:600]} "
          f"applied={[c['field'] for c in plan['changes']]} added={len(plan['added'])} removed={len(plan['removed'])}")
    if not plan["set"]:
        return {"understood": True,
                "reply": "That's already how your Playbook is set — nothing needed changing.",
                "changes": [], "added": [], "removed": [], "change_id": None}

    instructions_after = plan["set"].get("playbook_instructions", profile.get("playbook_instructions") or [])
    if len(instructions_after) > MAX_INSTRUCTIONS:
        raise InstructError(f"Jane is already remembering {MAX_INSTRUCTIONS} instructions. Remove a few you no "
                            f"longer need, then try again.")

    now = datetime.utcnow()
    update = {**plan["set"], "updated_at": now}
    scope = _scope(profile, user_id)
    # An agency brand can be running on its owner's personal Playbook (no
    # profile of its own yet); the change then starts that brand's own profile.
    await db["brand_profiles"].update_one(
        scope, {"$set": update, "$setOnInsert": {"user_id": user_id, "created_at": now}},
        upsert="brand_id" in scope and isinstance(scope["brand_id"], str),
    )

    change_id = f"chg_{uuid.uuid4().hex[:12]}"
    await db[CHANGES_COLLECTION].insert_one({
        "id": change_id, "user_id": user_id, "brand_id": brand_id, "source": source, "text": text,
        "reply": _clean_text(proposal.get("reply"), 600),
        "changes": plan["changes"],
        "added": [{"id": i["id"], "text": i["text"]} for i in plan["added"]],
        "removed": [{"id": i.get("id"), "text": i.get("text")} for i in plan["removed"]],
        "before": {k: (profile or {}).get(k) for k in plan["set"]},
        "after": plan["set"],
        "created_at": now, "undone_at": None,
    })
    return {"understood": True, "reply": _clean_text(proposal.get("reply"), 600) or "Done — your Playbook is updated.",
            "changes": plan["changes"], "added": plan["added"], "removed": plan["removed"],
            "change_id": change_id}


async def undo(db: AsyncIOMotorDatabase, user_id: str, brand_id: Optional[str], profile: Dict[str, Any],
               change_id: str) -> Dict[str, Any]:
    """Put back what one update changed. A field edited again since then is
    left alone (and named), so undo never wipes out later work."""
    q = {"id": change_id, "user_id": user_id}
    if brand_id:
        q["brand_id"] = brand_id
    log = await db[CHANGES_COLLECTION].find_one(q)
    if not log:
        raise InstructError("That change wasn't found.", status=404)
    if log.get("undone_at"):
        raise InstructError("That change was already undone.", status=409)
    restore, skipped = {}, []
    for key, after in (log.get("after") or {}).items():
        if (profile or {}).get(key) == after:
            restore[key] = (log.get("before") or {}).get(key)
        else:
            skipped.append(key)
    if restore:
        unset = {k: "" for k, v in restore.items() if v is None}
        ops: Dict[str, Any] = {"$set": {**{k: v for k, v in restore.items() if v is not None},
                                        "updated_at": datetime.utcnow()}}
        if unset:
            ops["$unset"] = unset
        await db["brand_profiles"].update_one(_scope(profile, user_id), ops)
    await db[CHANGES_COLLECTION].update_one({"id": change_id}, {"$set": {"undone_at": datetime.utcnow()}})
    return {"restored": sorted(restore), "skipped": sorted(skipped)}


async def remove_instruction(db: AsyncIOMotorDatabase, user_id: str, profile: Dict[str, Any],
                             instruction_id: str) -> List[Dict[str, Any]]:
    items = [i for i in (profile.get("playbook_instructions") or []) if isinstance(i, dict)]
    kept = [i for i in items if i.get("id") != instruction_id]
    if len(kept) == len(items):
        raise InstructError("That instruction wasn't found.", status=404)
    await db["brand_profiles"].update_one(_scope(profile, user_id),
                                          {"$set": {"playbook_instructions": kept, "updated_at": datetime.utcnow()}})
    return kept


async def history(db: AsyncIOMotorDatabase, user_id: str, brand_id: Optional[str], limit: int = 15
                  ) -> List[Dict[str, Any]]:
    q: Dict[str, Any] = {"user_id": user_id}
    if brand_id:
        q["brand_id"] = brand_id
    cursor = db[CHANGES_COLLECTION].find(q, {"_id": 0, "before": 0, "after": 0}).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


def public_instruction(i: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": i.get("id"), "text": i.get("text"), "applies_to": i.get("applies_to") or ["posts"],
            "platforms": i.get("platforms") or [], "created_at": i.get("created_at")}

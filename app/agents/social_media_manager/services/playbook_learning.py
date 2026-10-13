"""
The Playbook learns from what the user actually does.

When someone keeps fixing Jane's drafts the same way — stripping emojis,
cutting hashtags, shortening captions, deleting the same word — that's a
Playbook setting they haven't made yet. And when their posts clearly do
better at a certain time of day, that's a posting-time preference.

This module spots those patterns and proposes the matching Playbook change.
Rules, by design:
- Evidence-based and explainable: every suggestion says what was seen.
- Never automatic: nothing changes until the user presses Apply.
- No AI calls: plain pattern checks over edits and results already stored
  (content_drafts.human_edits, content_analytics).
- Quiet: a pattern needs to repeat, and a dismissed suggestion stays away.
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

MIN_EDITS = 4          # don't draw conclusions from one or two edits
MIN_REPEATS = 3        # a habit, not an accident
MAJORITY = 0.6         # ...and in most of the relevant edits
RECENT_DRAFTS = 60
DISMISS_DAYS = 60
MIN_POSTS_FOR_TIMING = 10
MIN_POSTS_PER_SLOT = 3
SLOT_LIFT = 1.3      # the slot must beat the overall average by 30%

_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U0001F900-\U0001F9FF⬀-⯿️]"
)
_WORD = re.compile(r"[a-zA-Z][a-zA-Z'-]{3,}")
_STOP = set("""
about after again also always among another because before being between both could does doing down during each
every from further have having here into itself just more most much must never only other over same should some
such than that their theirs them then there these they this those through under until very were what when where
which while will with would your yours yourself today tomorrow really thing things make makes made want wants
like love good great best come comes going know need needs take takes time week year
""".split())
_HASHTAG_OPTIONS = [3, 5, 10, 15]

# Playbook posting-time slots (same labels as posting_preferences.TIME_SLOTS).
_SLOTS = [("Early Morning (6-9am)", 6, 9), ("Late Morning (9am-12pm)", 9, 12), ("Afternoon (12-4pm)", 12, 16),
          ("Evening (4-8pm)", 16, 20), ("Night (8pm-12am)", 20, 24)]


def collect_edits(drafts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten content_drafts.human_edits into one record per edit."""
    edits = []
    for d in drafts:
        for stamp, edit in sorted((d.get("human_edits") or {}).items()):
            changes = (edit or {}).get("changes") or {}
            content, tags = changes.get("content"), changes.get("hashtags")
            if not content and not tags:
                continue
            edits.append({
                "draft_id": d.get("id"),
                "old": (content or {}).get("old") or "",
                "new": (content or {}).get("new") or "",
                "old_tags": (tags or {}).get("old"),
                "new_tags": (tags or {}).get("new"),
            })
    return edits


def _habit(count: int, relevant: int) -> bool:
    return count >= MIN_REPEATS and relevant > 0 and count / relevant >= MAJORITY


def _words(text: str) -> set:
    return {w.lower() for w in _WORD.findall(text or "") if w.lower() not in _STOP}


def detect_from_edits(edits: List[Dict[str, Any]], profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    content_edits = [e for e in edits if e["old"] and e["new"] and e["old"] != e["new"]]
    if len(edits) < MIN_EDITS:
        return out
    guardrails = profile.get("guardrails") or {}

    # 1. Emojis removed.
    with_emoji = [e for e in content_edits if _EMOJI.search(e["old"])]
    stripped = [e for e in with_emoji if not _EMOJI.search(e["new"])]
    if guardrails.get("emoji_usage") != "no" and _habit(len(stripped), len(with_emoji)):
        out.append({
            "id": "emoji_none",
            "title": "Stop using emojis",
            "evidence": f"You removed the emojis from {len(stripped)} of the last {len(with_emoji)} drafts that had them.",
            "change": "Set emoji usage to “No emojis”. Applies to posts, ads, inbox replies and video scripts.",
            "apply": {"guardrails": {"emoji_usage": "no"}},
        })

    # 2. Hashtags cut down.
    tag_edits = [e for e in edits if isinstance(e["old_tags"], list) and isinstance(e["new_tags"], list)]
    trimmed = [e for e in tag_edits if len(e["new_tags"]) < len(e["old_tags"])]
    if _habit(len(trimmed), len(tag_edits)):
        typical = int(statistics.median(len(e["new_tags"]) for e in trimmed))
        limit = next((o for o in _HASHTAG_OPTIONS if o >= max(typical, 1)), _HASHTAG_OPTIONS[-1])
        current = str(guardrails.get("max_hashtags") or "")
        if not (current.isdigit() and int(current) <= limit):
            out.append({
                "id": f"max_hashtags_{limit}",
                "title": f"Use at most {limit} hashtags",
                "evidence": f"You cut the hashtags on {len(trimmed)} of your last {len(tag_edits)} hashtag edits, "
                            f"usually down to about {typical}.",
                "change": f"Set the hashtag limit to {limit}.",
                "apply": {"guardrails": {"max_hashtags": str(limit)}},
            })

    # 3. Captions shortened.
    shortened = [e for e in content_edits if len(e["new"]) <= 0.75 * len(e["old"])]
    current_length = (profile.get("voice_profile") or {}).get("caption_length") or "short"
    if current_length != "short" and _habit(len(shortened), len(content_edits)):
        out.append({
            "id": "caption_short",
            "title": "Write shorter captions",
            "evidence": f"You shortened {len(shortened)} of your last {len(content_edits)} edited captions by a quarter or more.",
            "change": "Set caption length to short (3-5 lines).",
            "apply": {"voice_profile": {"caption_length": "short"}},
        })

    # 4. The same word deleted again and again — and never put back.
    removed_in: Dict[str, set] = {}
    kept_anywhere: set = set()
    for e in content_edits:
        kept_anywhere |= _words(e["new"])
        for w in _words(e["old"]) - _words(e["new"]):
            removed_in.setdefault(w, set()).add(e["draft_id"])
    already = {str(w).lower() for w in (profile.get("words_to_avoid") or [])}
    already |= {w.strip().lower() for w in str(guardrails.get("banned_words") or "").replace("\n", ",").split(",")}
    brand_terms = _words(" ".join([str(profile.get("brand_name") or ""), str(profile.get("industry") or "")]
                                  + [str(x) for x in (profile.get("key_products_services") or [])]))
    repeat = sorted(
        (w for w, ds in removed_in.items()
         if len(ds) >= MIN_REPEATS and w not in kept_anywhere and w not in already and w not in brand_terms),
        key=lambda w: (-len(removed_in[w]), w),
    )[:3]
    if repeat:
        counts = ", ".join(f"“{w}” ({len(removed_in[w])} drafts)" for w in repeat)
        out.append({
            "id": "avoid_" + "_".join(repeat),
            "title": "Add words you keep deleting to “Words to avoid”",
            "evidence": f"You deleted these from Jane's drafts and never used them yourself: {counts}.",
            "change": f"Add {', '.join(repeat)} to Words to avoid. Jane won't use them anywhere.",
            "apply": {"words_to_avoid_add": repeat},
        })
    return out


def _slot_for(hour: int) -> Optional[str]:
    return next((label for label, start, end in _SLOTS if start <= hour < end), None)


def detect_from_performance(posts: List[Dict[str, Any]], profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Posts clearly do best in a time-of-day slot the Playbook isn't set to use.

    `posts`: published posts with `_published` (naive UTC datetime),
    `engagement_rate` and `impressions`. A slot only counts with at least
    MIN_POSTS_PER_SLOT posts in it and engagement clearly above the rest —
    one lucky post is not a pattern."""
    from datetime import timezone

    from . import posting_preferences

    measured = [p for p in posts if (p.get("impressions") or 0) > 0 and p.get("_published")]
    if len(measured) < MIN_POSTS_FOR_TIMING:
        return []
    tz = posting_preferences.brand_timezone(profile)
    by_slot: Dict[str, List[float]] = {}
    for post in measured:
        local = post["_published"].replace(tzinfo=timezone.utc).astimezone(tz)
        slot = _slot_for(local.hour)
        if slot:
            by_slot.setdefault(slot, []).append(float(post["engagement_rate"]))
    overall = sum(float(p["engagement_rate"]) for p in measured) / len(measured)
    candidates = [(sum(v) / len(v), slot, len(v)) for slot, v in by_slot.items() if len(v) >= MIN_POSTS_PER_SLOT]
    if not candidates or overall <= 0:
        return []
    best_rate, slot, count = max(candidates)
    if best_rate < overall * SLOT_LIFT:
        return []
    mode = str(profile.get("posting_time_mode") or "").lower()
    chosen = str((profile.get("posting_time_prefs") or {}).get("general") or "")
    if mode == "general" and slot in chosen:
        return []
    return [{
        "id": "post_time_" + re.sub(r"[^a-z0-9]+", "_", slot.lower()).strip("_"),
        "title": f"Post in the {slot.split(' (')[0].lower()}",
        "evidence": f"Your {count} posts that went out in the {slot.split(' (')[0].lower()} averaged "
                    f"{best_rate:.1f}% engagement, against {overall:.1f}% across all {len(measured)} measured posts.",
        "change": f"Set your preferred posting time to {slot}. Used when scheduling and for auto-published posts.",
        "apply": {"posting_time_mode": "general", "posting_time_general": slot},
    }]


def filter_dismissed(suggestions: List[Dict[str, Any]], profile: Dict[str, Any],
                     now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    now = now or datetime.utcnow()
    dismissed = profile.get("playbook_suggestions_dismissed") or {}

    def hidden(sid: str) -> bool:
        when = dismissed.get(sid)
        if isinstance(when, str):
            try:
                when = datetime.fromisoformat(when)
            except ValueError:
                return False
        return isinstance(when, datetime) and now - when < timedelta(days=DISMISS_DAYS)

    return [s for s in suggestions if not hidden(s["id"])]


def build_update(apply: Dict[str, Any], profile: Dict[str, Any]) -> Dict[str, Any]:
    """The brand_profiles $set for a suggestion. Nested objects are written
    whole (a profile's guardrails/voice_profile can be missing or null, and
    Mongo can't set a field inside null)."""
    update: Dict[str, Any] = {}
    if "guardrails" in apply:
        update["guardrails"] = {**(profile.get("guardrails") or {}), **apply["guardrails"]}
    if "voice_profile" in apply:
        update["voice_profile"] = {**(profile.get("voice_profile") or {}), **apply["voice_profile"]}
    if "words_to_avoid_add" in apply:
        existing = [str(w) for w in (profile.get("words_to_avoid") or [])]
        lowered = {w.lower() for w in existing}
        update["words_to_avoid"] = existing + [w for w in apply["words_to_avoid_add"] if w.lower() not in lowered]
    if "posting_time_mode" in apply:
        update["posting_time_mode"] = apply["posting_time_mode"]
        update["posting_time_prefs"] = {**(profile.get("posting_time_prefs") or {}),
                                        "general": f"{apply['posting_time_general']},"}
    return update


async def suggestions_for(db, user_id: str, brand_id: Optional[str], profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Everything worth suggesting to this brand right now."""
    from app.models.brand_account import BrandAccount

    scope = ({"brand_id": brand_id} if brand_id and brand_id != BrandAccount.personal_brand_id(user_id)
             else {"user_id": user_id})
    drafts = await db["content_drafts"].find(
        {**scope, "human_edits": {"$exists": True, "$ne": {}}}, {"id": 1, "human_edits": 1},
    ).sort("updated_at", -1).to_list(length=RECENT_DRAFTS)
    found = detect_from_edits(collect_edits(drafts), profile)
    try:
        from app.services.brand_alert_jobs import _published_with_stats
        posts = await _published_with_stats(db, user_id, brand_id, datetime.utcnow() - timedelta(days=90))
        found += detect_from_performance(posts, profile)
    except Exception as exc:
        print(f"[PlaybookLearning] performance read failed: {exc}")
    return filter_dismissed(found, profile)

"""
Playbook health: how complete a Brand Playbook is, what's missing, and what
each setting actually powers.

The registry below is the map of "Playbook setting -> which of Jane's jobs
use it". It is what the Playbook page shows under each field ("Used in:
Posts · Ads · Inbox") and what drives the suggestions ("Add your key
products — improves Posts, Ads, Inbox, Leads"). Keep it true to the code:
when a generator starts or stops reading a field (see brand_brief.py and
content_generation_service._build_brand_block), update it here.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from . import brand_brief

POSTS, IMAGES, CALENDAR, ADS, INBOX, VIDEO, BLOG, LEADS, AUTO, SCHEDULING = (
    "Posts", "Images", "Calendar", "Ads", "Inbox", "Video", "Blog", "Leads", "Auto tab", "Scheduling",
)
_WRITING = [POSTS, CALENDAR, ADS, INBOX, VIDEO, BLOG]

# key, label (as shown on the Playbook page), section, weight (3 = Jane is guessing without it),
# used_in, why (shown as the suggestion).
FIELDS: List[Dict[str, Any]] = [
    # Brand Identity
    {"key": "brand_name", "label": "Brand name", "section": "Brand Identity", "weight": 3,
     "used_in": [POSTS, IMAGES, CALENDAR, ADS, INBOX, VIDEO, BLOG, LEADS], "why": "Jane needs to know who she's speaking for."},
    {"key": "industry", "label": "Industry", "section": "Brand Identity", "weight": 3,
     "used_in": [POSTS, IMAGES, CALENDAR, ADS, LEADS], "why": "Sets the vocabulary, the content plan and the default look."},
    {"key": "product_description", "label": "Description", "section": "Brand Identity", "weight": 3,
     "used_in": _WRITING, "why": "Without it Jane writes from your name and industry alone, and it shows."},
    {"key": "key_products_services", "label": "Key products / services", "section": "Brand Identity", "weight": 3,
     "used_in": [POSTS, ADS, INBOX, VIDEO, BLOG, LEADS], "why": "So posts, ads and replies talk about what you actually sell."},
    {"key": "tagline", "label": "Tagline", "section": "Brand Identity", "weight": 1,
     "used_in": [POSTS, IMAGES, ADS], "why": "Gives posts and poster-style images a signature line."},
    {"key": "website", "label": "Website", "section": "Brand Identity", "weight": 1,
     "used_in": [POSTS, INBOX, VIDEO], "why": "Jane can point customers to it."},
    {"key": "logo_url", "label": "Logo", "section": "Brand Identity", "weight": 3,
     "used_in": [IMAGES, ADS, VIDEO], "why": "Your images and ads go out unbranded without it."},
    # Business Details
    {"key": "price_range", "label": "Price range", "section": "Business Details", "weight": 2,
     "used_in": _WRITING, "why": "Keeps the wording right for your price level (premium vs budget)."},
    {"key": "unique_selling_proposition", "label": "What makes you different", "section": "Business Details", "weight": 2,
     "used_in": _WRITING, "why": "Gives hooks and ads a reason to choose you over a competitor."},
    {"key": "business_stage", "label": "Business stage", "section": "Business Details", "weight": 1,
     "used_in": [POSTS, CALENDAR], "why": "New brands need trust-building; established ones need authority."},
    {"key": "business_priorities", "label": "Current business priorities", "section": "Business Details", "weight": 1,
     "used_in": [POSTS], "why": "Jane leans posts toward what matters to you right now."},
    # Look
    {"key": "brand_colors", "label": "Brand colors", "section": "Brand Colors", "weight": 3,
     "used_in": [IMAGES, ADS, VIDEO], "why": "Images use only your colours; without them Jane picks her own."},
    {"key": "style_selections", "label": "Visual style", "section": "Visual Style", "weight": 2,
     "used_in": [IMAGES, CALENDAR, ADS], "why": "Makes every image recognisably yours instead of a new look each time."},
    # Voice
    {"key": "brand_voice", "label": "Brand voice", "section": "Brand Voice", "weight": 3,
     "used_in": [POSTS, CALENDAR, ADS, INBOX, VIDEO], "why": "How everything Jane writes should sound."},
    {"key": "voice_sample", "label": "Voice sample", "section": "Brand Voice", "weight": 2,
     "used_in": [POSTS], "why": "One real example of your writing teaches Jane more than any description."},
    # Content
    {"key": "content_pillars", "label": "Content pillars", "section": "Content Strategy", "weight": 2,
     "used_in": [POSTS, CALENDAR], "why": "Keeps posts and the monthly plan on your topics."},
    {"key": "preferred_formats", "label": "Preferred formats", "section": "Content Strategy", "weight": 1,
     "used_in": [POSTS], "why": "Shapes how posts are structured."},
    {"key": "key_dates", "label": "Important dates", "section": "Important Dates", "weight": 1,
     "used_in": [POSTS, IMAGES, CALENDAR], "why": "So your launches and anniversaries make it into the plan."},
    # Audience
    {"key": "ideal_customer_profile", "label": "Ideal customer", "section": "Audience", "weight": 3,
     "used_in": [POSTS, CALENDAR, ADS, VIDEO], "why": "Jane writes for a real person instead of 'everyone'."},
    {"key": "audience_age_range", "label": "Audience age", "section": "Audience", "weight": 1,
     "used_in": [POSTS, VIDEO], "why": "Adapts your voice to the age group you serve."},
    {"key": "primary_goal", "label": "Primary goal", "section": "Audience", "weight": 2,
     "used_in": [POSTS, ADS, VIDEO, BLOG, LEADS], "why": "Every post and ad should move people toward it."},
    {"key": "customer_pain_points", "label": "Customer pain points", "section": "Audience", "weight": 2,
     "used_in": [POSTS, CALENDAR, VIDEO], "why": "The best hooks start from a problem your customer already feels."},
    {"key": "customer_needs", "label": "Customer needs", "section": "Audience", "weight": 1,
     "used_in": [POSTS, CALENDAR, VIDEO], "why": "Frames your product around what people need from it."},
    {"key": "customer_objections", "label": "Common objections", "section": "Audience", "weight": 1,
     "used_in": [POSTS, CALENDAR, VIDEO], "why": "Lets Jane answer doubts before they're raised."},
    {"key": "why_customers_choose_us", "label": "Why customers choose you", "section": "Audience", "weight": 2,
     "used_in": _WRITING, "why": "Your strongest selling line, in your customers' own reasoning."},
    {"key": "common_questions", "label": "Common questions customers ask", "section": "Audience", "weight": 2,
     "used_in": [POSTS, CALENDAR, INBOX], "why": "Feeds post ideas and helps Jane answer in the inbox."},
    {"key": "words_to_avoid", "label": "Words/phrases to avoid", "section": "Audience", "weight": 1,
     "used_in": _WRITING, "why": "Jane will never use them, anywhere."},
    {"key": "competitor_handles", "label": "Competitors", "section": "Audience", "weight": 1,
     "used_in": [POSTS, ADS, VIDEO, BLOG], "why": "Jane positions you against them — without ever naming them."},
    # Language & region
    {"key": "region", "label": "Region", "section": "Language & Region", "weight": 2,
     "used_in": [POSTS, IMAGES, CALENDAR, ADS, INBOX, VIDEO, LEADS, SCHEDULING],
     "why": "Local references, local-looking images, ad targeting and your posting timezone."},
    {"key": "languages", "label": "Languages", "section": "Language & Region", "weight": 1,
     "used_in": [POSTS, CALENDAR, ADS, VIDEO, BLOG], "why": "Jane writes in the language your customers speak."},
    # Rules
    {"key": "guardrails", "label": "Guardrails", "section": "Guardrails", "weight": 2,
     "used_in": _WRITING, "why": "Topics to avoid, banned words and compliance notes are enforced everywhere Jane writes."},
    # CTA
    {"key": "cta_styles", "label": "Call to action", "section": "Call to Action", "weight": 3,
     "used_in": [POSTS, IMAGES, CALENDAR], "why": "Without one, posts end with a generic 'Link in bio'."},
    {"key": "default_link", "label": "Default link", "section": "Call to Action", "weight": 1,
     "used_in": [POSTS, INBOX], "why": "Where 'order now' should send people."},
    # Workflow
    {"key": "posting_cadence", "label": "Posting cadence", "section": "Posting & Workflow", "weight": 1,
     "used_in": [AUTO], "why": "How often the Auto tab creates posts for you."},
    {"key": "approval_workflow", "label": "Approval workflow", "section": "Posting & Workflow", "weight": 1,
     "used_in": [AUTO], "why": "Whether Auto-tab posts wait for you or go out on schedule."},
    # Right now
    {"key": "business_pulse", "label": "Business Pulse", "section": "Business Pulse", "weight": 2,
     "used_in": [POSTS, CALENDAR, ADS, VIDEO, BLOG], "why": "Tell Jane about this week's promotion and she'll use it everywhere."},
]

# Extra labels on the page that belong to a registry field (shown with the same "Used in").
_LABEL_ALIASES = {
    "Topics to avoid": "guardrails", "Banned words / phrases": "guardrails", "Compliance notes": "guardrails",
    "Emoji usage": "guardrails", "Max hashtags": "guardrails",
    "Customer desires": "customer_pain_points", "Customer fears": "customer_pain_points",
    "Customer frustrations": "customer_pain_points", "Customer aspirations": "customer_pain_points",
    "Reasons customers hesitate": "customer_objections", "Buying triggers": "customer_needs",
    "Customer gender": "ideal_customer_profile", "Customer occupation": "ideal_customer_profile",
    "Customer location": "ideal_customer_profile", "Customer income level": "ideal_customer_profile",
    "Customer interests": "ideal_customer_profile",
}
_USED_IN_OVERRIDES = {
    # These read narrower than the field they're grouped under.
    "Customer desires": [POSTS, CALENDAR], "Customer fears": [POSTS, CALENDAR],
    "Customer frustrations": [POSTS, CALENDAR], "Customer aspirations": [POSTS, CALENDAR],
    "Reasons customers hesitate": [POSTS, CALENDAR], "Buying triggers": [POSTS, CALENDAR],
    "Customer interests": [POSTS], "Max hashtags": [POSTS],
    "Customer gender": [POSTS, VIDEO], "Customer occupation": [POSTS, VIDEO],
    "Customer location": [POSTS, INBOX, VIDEO], "Customer income level": [POSTS, VIDEO],
}


def used_in_by_label() -> Dict[str, List[str]]:
    by_key = {f["key"]: f["used_in"] for f in FIELDS}
    out = {f["label"]: f["used_in"] for f in FIELDS}
    for label, key in _LABEL_ALIASES.items():
        out[label] = _USED_IN_OVERRIDES.get(label, by_key[key])
    return out


def _has(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has(v) for v in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_has(v) for v in value)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None and value is not False


def _filled(profile: Dict[str, Any], key: str, now: datetime) -> bool:
    if key == "brand_voice":
        return _has(profile.get("derived_voice")) or _has(profile.get("personality_quiz")) or _has(profile.get("brand_voice"))
    if key == "product_description":
        return _has(profile.get("product_description")) or _has(profile.get("business_description"))
    if key == "guardrails":
        g = profile.get("guardrails") or {}
        return any(_has(g.get(k)) for k in ("avoid_topics", "banned_words", "compliance_notes"))
    if key == "business_pulse":
        return bool(brand_brief.pulse(profile, now=now))  # empty once it has expired
    if key == "style_selections":
        return any(_has(profile.get(k)) for k in ("style_selections", "selected_custom_guides", "selected_custom_guides_v2"))
    return _has(profile.get(key))


def assess(profile: Optional[Dict[str, Any]], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Score (0-100, weighted), the most valuable things still missing, and
    the per-field "used in" map for the Playbook page."""
    profile = profile or {}
    now = now or datetime.utcnow()
    total = sum(f["weight"] for f in FIELDS)
    missing = [f for f in FIELDS if not _filled(profile, f["key"], now)]
    earned = total - sum(f["weight"] for f in missing)
    score = round(earned / total * 100) if total else 0
    level = ("Excellent" if score >= 90 else "Strong" if score >= 75 else "Good start" if score >= 50 else "Needs attention")

    pulse_data = profile.get("business_pulse") or {}
    pulse_expired = _has(pulse_data) and not brand_brief.pulse(profile, now=now)

    def suggestion(f: Dict[str, Any]) -> Dict[str, Any]:
        why = f["why"]
        if f["key"] == "business_pulse" and pulse_expired:
            why = "Your current promotions are over 45 days old, so Jane has stopped mentioning them. Update them."
        return {"key": f["key"], "label": f["label"], "section": f["section"], "improves": f["used_in"], "why": why}

    ranked = sorted(missing, key=lambda f: (-f["weight"], -len(f["used_in"])))
    return {
        "score": score,
        "level": level,
        "filled": len(FIELDS) - len(missing),
        "total": len(FIELDS),
        "suggestions": [suggestion(f) for f in ranked[:5]],
        "missing_count": len(missing),
        "used_in": used_in_by_label(),
    }

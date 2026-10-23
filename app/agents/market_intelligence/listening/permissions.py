"""
Per-source permission records (PRD §15): "each connector has a permission
record (terms, allowed use, retention, display rules) approved before
production use." Not just a policy document — collector.collect_market()
actually refuses to collect from a source with no approved record, and
evidence samples are truncated to the record's own display_rules instead
of a hardcoded constant, so this has real teeth rather than being
documentation nobody enforces.

X (both Apify actors) ship pre-approved: they're already this platform's
production source as of the pilot (§19 Phase 0), so seeding them here on
first boot is recording a decision already made, not making a new one. A
new source added later starts unapproved until someone explicitly
approves it — collection for it simply won't run until they do.

TikTok, Instagram, YouTube, Reddit and News (added when the module grew
past X-only) are seeded here specifically so they're *visible* as rows in
the admin Sources tab with their real caveats attached, rather than being
invisible until someone already knows the exact source key to approve —
but every one of them ships with `approved: False`. Each adapter's own
module docstring explains why: actor ids/pricing/field mappings were
researched against the live Apify Store, not run and confirmed, so each
needs one real small test scan reviewed by an admin before approval is
anything but a guess.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

_X_TERMS = "https://developer.x.com/en/developer-terms/agreement-and-policy"
_ALLOWED_USE = ["evidence_display", "aggregate_counts", "classification"]
_DISPLAY_RULES = {"max_excerpt_chars": 280, "require_link": True, "require_attribution": True}

_SEED = [
    {"source": "x", "terms_url": _X_TERMS, "allowed_use": _ALLOWED_USE, "retention_days": 90,
     "display_rules": _DISPLAY_RULES, "approved": True, "approved_by": "pilot-default",
     "notes": "Apify test actor, pilot scale."},
    {"source": "x_pro", "terms_url": _X_TERMS, "allowed_use": _ALLOWED_USE, "retention_days": 90,
     "display_rules": _DISPLAY_RULES, "approved": True, "approved_by": "pilot-default",
     "notes": "Apify production actor."},
    {"source": "tiktok", "terms_url": "https://www.tiktok.com/legal/page/global/terms-of-service/en",
     "allowed_use": _ALLOWED_USE, "retention_days": 90, "display_rules": _DISPLAY_RULES,
     "approved": False, "approved_by": None,
     "notes": "Apify actor apidojo~tiktok-scraper — unverified, needs one real test scan before approval."},
    {"source": "tiktok_pro", "terms_url": "https://www.tiktok.com/legal/page/global/terms-of-service/en",
     "allowed_use": _ALLOWED_USE, "retention_days": 90, "display_rules": _DISPLAY_RULES,
     "approved": False, "approved_by": None,
     "notes": "Apify actor clockworks~tiktok-scraper — unverified, needs one real test scan before approval."},
    {"source": "instagram", "terms_url": "https://help.instagram.com/581066165581870",
     "allowed_use": _ALLOWED_USE, "retention_days": 90, "display_rules": _DISPLAY_RULES,
     "approved": False, "approved_by": None,
     "notes": "Apify actor apify~instagram-hashtag-scraper, test cap — unverified, needs one real test scan "
              "before approval."},
    {"source": "instagram_pro", "terms_url": "https://help.instagram.com/581066165581870",
     "allowed_use": _ALLOWED_USE, "retention_days": 90, "display_rules": _DISPLAY_RULES,
     "approved": False, "approved_by": None,
     "notes": "Apify actor apify~instagram-hashtag-scraper, higher cap — unverified, needs one real test scan "
              "before approval."},
    {"source": "youtube", "terms_url": "https://www.youtube.com/t/terms",
     "allowed_use": _ALLOWED_USE, "retention_days": 90, "display_rules": _DISPLAY_RULES,
     "approved": False, "approved_by": None,
     "notes": "Apify actor automation-lab~youtube-scraper, test cap — unverified, needs one real test scan "
              "before approval."},
    {"source": "youtube_pro", "terms_url": "https://www.youtube.com/t/terms",
     "allowed_use": _ALLOWED_USE, "retention_days": 90, "display_rules": _DISPLAY_RULES,
     "approved": False, "approved_by": None,
     "notes": "Apify actor automation-lab~youtube-scraper, higher comment cap — unverified, needs one real "
              "test scan before approval."},
    {"source": "reddit", "terms_url": "https://www.redditinc.com/policies/user-agreement",
     "allowed_use": _ALLOWED_USE, "retention_days": 90, "display_rules": _DISPLAY_RULES,
     "approved": False, "approved_by": None,
     "notes": "Apify actor clearpath~reddit-search-scraper, test cap — unverified, needs one real test scan "
              "before approval."},
    {"source": "reddit_pro", "terms_url": "https://www.redditinc.com/policies/user-agreement",
     "allowed_use": _ALLOWED_USE, "retention_days": 90, "display_rules": _DISPLAY_RULES,
     "approved": False, "approved_by": None,
     "notes": "Apify actor clearpath~reddit-search-scraper, higher cap — unverified, needs one real test scan "
              "before approval."},
    {"source": "news", "terms_url": "https://policies.google.com/terms",
     "allowed_use": _ALLOWED_USE, "retention_days": 90, "display_rules": _DISPLAY_RULES,
     "approved": False, "approved_by": None,
     "notes": "Apify actor cleanscrape~google-news-scraper, test cap — unverified, needs one real test scan "
              "before approval."},
    {"source": "news_pro", "terms_url": "https://policies.google.com/terms",
     "allowed_use": _ALLOWED_USE, "retention_days": 90, "display_rules": _DISPLAY_RULES,
     "approved": False, "approved_by": None,
     "notes": "Apify actor cleanscrape~google-news-scraper, higher cap — unverified, needs one real test scan "
              "before approval."},
]

DEFAULT_DISPLAY_RULES = {"max_excerpt_chars": 280, "require_link": True, "require_attribution": True}


async def ensure_seeded(db) -> None:
    for rec in _SEED:
        if not await db["mi_source_permissions"].find_one({"source": rec["source"]}):
            await db["mi_source_permissions"].insert_one({**rec, "approved_at": datetime.utcnow()})


async def get(db, source: str) -> Optional[dict]:
    doc = await db["mi_source_permissions"].find_one({"source": source})
    if doc:
        doc.pop("_id", None)
    return doc


async def is_approved(db, source: str) -> bool:
    doc = await get(db, source)
    return bool(doc and doc.get("approved"))


async def display_rules(db, source: str) -> dict:
    doc = await get(db, source)
    return (doc or {}).get("display_rules") or DEFAULT_DISPLAY_RULES


async def set_approval(db, source: str, approved: bool, approved_by: str, notes: Optional[str] = None) -> dict:
    now = datetime.utcnow()
    update: dict = {"approved": approved, "approved_by": approved_by, "approved_at": now}
    if notes is not None:
        update["notes"] = notes
    await db["mi_source_permissions"].update_one({"source": source}, {"$set": update}, upsert=True)
    return await get(db, source)

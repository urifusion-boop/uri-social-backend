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
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

_SEED = [
    {"source": "x", "terms_url": "https://developer.x.com/en/developer-terms/agreement-and-policy",
     "allowed_use": ["evidence_display", "aggregate_counts", "classification"], "retention_days": 90,
     "display_rules": {"max_excerpt_chars": 280, "require_link": True, "require_attribution": True},
     "approved": True, "approved_by": "pilot-default", "notes": "Apify test actor, pilot scale."},
    {"source": "x_pro", "terms_url": "https://developer.x.com/en/developer-terms/agreement-and-policy",
     "allowed_use": ["evidence_display", "aggregate_counts", "classification"], "retention_days": 90,
     "display_rules": {"max_excerpt_chars": 280, "require_link": True, "require_attribution": True},
     "approved": True, "approved_by": "pilot-default", "notes": "Apify production actor."},
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

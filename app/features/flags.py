"""
Feature switches for staged rollout (PRD shared requirements; plan: "staff first, then 5% of
brands, then everyone, with the old flow one switch away").

Each switch has two settings, kept in Mongo so they change without a deploy:
  - staff:   admins see it whatever the percentage
  - percent: the share of brands that see it, 0–100

A brand's bucket is a stable hash of (switch, brand), so raising 5% → 20% only ADDS brands and a
brand never flickers between the old and new flow. Settings are cached in each process for a
minute, so checking a switch costs nothing at scale.

New switches start staff-only (percent 0) unless FEATURE_DEFAULT_PERCENT says otherwise.
"""
from __future__ import annotations

import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Optional

COLLECTION = "feature_rollouts"
CACHE_SECONDS = 60

FLAGS: dict[str, str] = {
    "website_playbook": "Website → Playbook (website-first setup and the Playbook panel)",
    "builder_products": "Builder: pick what you're advertising from the Playbook",
    "budget_suggestions": "Builder: suggested budgets",
    "competitors": "Competitors page (businesses, their ads, make one like this)",
}

_cache: dict[str, object] = {"at": 0.0, "docs": {}}


def default_percent() -> int:
    try:
        return max(0, min(100, int(os.getenv("FEATURE_DEFAULT_PERCENT", "0"))))
    except ValueError:
        return 0


def bucket(flag: str, brand_id: str) -> int:
    """0–99, stable for this switch and brand."""
    return int(hashlib.sha256(f"{flag}:{brand_id}".encode()).hexdigest()[:8], 16) % 100


async def _settings(db) -> dict[str, dict]:
    now = time.monotonic()
    if now - float(_cache["at"]) < CACHE_SECONDS and _cache["docs"]:
        return _cache["docs"]  # type: ignore[return-value]
    docs = {d["flag"]: d for d in await db[COLLECTION].find({}, {"_id": 0}).to_list(length=200)}
    _cache.update(at=now, docs=docs)
    return docs


def clear_cache() -> None:
    _cache.update(at=0.0, docs={})


def setting(docs: dict[str, dict], flag: str) -> dict:
    d = docs.get(flag) or {}
    return {"flag": flag, "label": FLAGS[flag], "staff": d.get("staff", True),
            "percent": int(d.get("percent", default_percent())), "updated_at": d.get("updated_at"),
            "updated_by": d.get("updated_by", "")}


async def enabled_for(db, brand_id: str, is_staff: bool) -> dict[str, bool]:
    docs = await _settings(db)
    out = {}
    for flag in FLAGS:
        s = setting(docs, flag)
        out[flag] = (is_staff and s["staff"]) or (bool(brand_id) and bucket(flag, brand_id) < s["percent"])
    return out


async def is_enabled(db, flag: str, brand_id: str, is_staff: bool = False) -> bool:
    return (await enabled_for(db, brand_id, is_staff)).get(flag, False)


async def update(db, flag: str, *, percent: Optional[int] = None, staff: Optional[bool] = None, by: str = "") -> dict:
    if flag not in FLAGS:
        raise KeyError(flag)
    change: dict = {"flag": flag, "updated_at": datetime.now(timezone.utc), "updated_by": by}
    if percent is not None:
        change["percent"] = max(0, min(100, int(percent)))
    if staff is not None:
        change["staff"] = bool(staff)
    await db[COLLECTION].update_one({"flag": flag}, {"$set": change}, upsert=True)
    clear_cache()
    return setting(await _settings(db), flag)

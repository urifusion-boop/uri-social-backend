"""
Jane + Ads — recall across campaigns (campaign roadmap, Tier 2 — memory).

PRD §6: "Platform tools are stateless per campaign. Jane is not." A returning
business shouldn't have to re-explain what Jane already learned launching their
last campaign.

Deliberately reuses `jane_ads_meta_campaigns` rather than a separate memory
store — every launched campaign already carries business_name, category, goal,
budget, city, and creative, keyed by business_id. A second collection would
just be a second place for the same facts to drift out of sync.
"""
from __future__ import annotations

from typing import Optional


async def get_campaign_history(db, business_id: str, limit: int = 3) -> list[dict]:
    """Most recent past campaigns for this business, newest first. Never raises —
    this is a nice-to-have that makes Jane feel like she remembers, not a
    dependency anything else should block on. Empty for a brand-new business."""
    if db is None or not business_id:
        return []
    try:
        cursor = db["jane_ads_meta_campaigns"].find(
            {"business_id": business_id},
            {
                "_id": 0, "campaign_id": 1, "display_name": 1, "category": 1,
                "goal": 1, "budget_ngn": 1, "city": 1, "headline": 1, "created_at": 1,
            },
        ).sort("created_at", -1).limit(limit)
        return await cursor.to_list(length=limit)
    except Exception as e:
        print(f"[History] lookup failed for {business_id}: {e}", flush=True)
        return []


def remembered_business_name(history: list[dict]) -> str:
    """The most recent real business name — 'Campaign' is the enrichment's
    generic fallback when nothing was ever actually named, not worth repeating
    back to the user or the ad copy."""
    return next((h["display_name"] for h in history if h.get("display_name") and h["display_name"] != "Campaign"), "")


def remembered_category(history: list[dict]) -> str:
    return next((h["category"] for h in history if h.get("category")), "")


def remembered_budget_ngn(history: list[dict]) -> Optional[float]:
    return next((h["budget_ngn"] for h in history if h.get("budget_ngn")), None)

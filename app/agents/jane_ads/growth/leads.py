"""
Lead quality and follow-up (PRD §4.6, §4.9), from the unified inbox.

A lead is an inbox conversation Meta tells us started from one of the campaign's
ads (a click-to-message referral, or a comment on the ad). Its stage is set by a
person — new, contacted, qualified, converted, lost — with who set it and when.
Nothing here infers a sale from a click or a message.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

STAGES = ("new", "contacted", "qualified", "converted", "lost")
FOLLOW_UP_AFTER = timedelta(hours=1)


def _aware(at) -> Optional[datetime]:
    if not isinstance(at, datetime):
        return None
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at


async def conversations_for(db, brand_id: str, ad_ids: list[str]) -> list[dict]:
    from app.agents.inbox.entities import CONVERSATIONS
    ids = [a for a in ad_ids if a]
    if not ids:
        return []
    return await db[CONVERSATIONS].find(
        {"workspace_id": brand_id, "source_ad_id": {"$in": ids}}).to_list(2000)


async def _last_direction(db, brand_id: str, conv_id: str) -> tuple[str, Optional[datetime]]:
    from app.agents.inbox.entities import MESSAGES
    m = await db[MESSAGES].find({"workspace_id": brand_id, "conversation_id": conv_id}) \
        .sort("received_at", -1).limit(1).to_list(1)
    if not m:
        return "", None
    return m[0].get("direction", ""), _aware(m[0].get("received_at"))


async def stage_of(db, brand_id: str, conv: dict) -> str:
    if conv.get("lead_stage") in STAGES:
        return conv["lead_stage"]
    direction, _ = await _last_direction(db, brand_id, str(conv["_id"]))
    return "contacted" if direction == "outbound" else "new"


async def stats(db, brand_id: str, ad_ids: list[str], spend_ngn: float,
                since: Optional[datetime] = None) -> dict:
    convs = await conversations_for(db, brand_id, ad_ids)
    counts = {s: 0 for s in STAGES}
    revenue = 0.0
    recent = 0
    since = since or datetime.now(timezone.utc) - timedelta(days=3)
    for c in convs:
        st = await stage_of(db, brand_id, c)
        counts[st] += 1
        if st == "converted":
            revenue += float(c.get("lead_value") or 0)
        first = _aware(c.get("created_at")) or _aware(c.get("last_activity_at"))
        if first and first >= since:
            recent += 1
    qualified = counts["qualified"] + counts["converted"]
    with_outcome = qualified + counts["lost"]
    return {
        "leads": len(convs),
        "by_stage": counts,
        "with_outcome": with_outcome,
        "qualified_rate": round(qualified / with_outcome, 3) if with_outcome else None,
        "cost_per_lead_ngn": round(spend_ngn / len(convs), 2) if convs else None,
        "cost_per_qualified_lead_ngn": round(spend_ngn / qualified, 2) if qualified else None,
        "customer_acquisition_cost_ngn": round(spend_ngn / counts["converted"], 2) if counts["converted"] else None,
        "revenue_ngn": round(revenue, 2),
        "roas": round(revenue / spend_ngn, 2) if revenue and spend_ngn else None,
        "recent_from_ads": recent,
        "available": bool(convs),
        "note": "" if with_outcome else
        "Lead quality needs outcomes: mark leads Qualified, Converted or Lost in Customer Messages.",
    }


async def follow_up_gaps(db, brand_id: str, ad_ids: list[str],
                         at: Optional[datetime] = None) -> dict:
    """Ad leads whose last message is the customer's and has waited over an hour."""
    at = at or datetime.now(timezone.utc)
    waiting, oldest = [], 0.0
    for c in await conversations_for(db, brand_id, ad_ids):
        if c.get("status") == "resolved" or c.get("lead_stage") in ("converted", "lost"):
            continue
        direction, when = await _last_direction(db, brand_id, str(c["_id"]))
        if direction == "inbound" and when and at - when > FOLLOW_UP_AFTER:
            hours = (at - when).total_seconds() / 3600
            waiting.append(str(c["_id"]))
            oldest = max(oldest, hours)
    return {"waiting": len(waiting), "conversation_ids": waiting[:20], "oldest_hours": round(oldest, 1)}

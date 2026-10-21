"""
Weekly listening digest (PRD §10): a once-a-week bundle of this business's
top findings from the last 7 days, for owners who'd rather not get an
alert for every card. Shares the "Trending topic in your niche" opt-in
with personalize.notify's immediate alerts — same feature to the owner,
just a different cadence; a brand that wants neither simply never ticks it.

"If no useful change exists, skip the digest rather than create filler"
(same policy as notification_delivery.send_daily_digests) — a business
with nothing new this week gets nothing, not an empty email.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional


async def _claim(db, brand_id: str, now: datetime) -> bool:
    """One claim per brand per ISO week — whichever daily tick gets there
    first sends it; every other tick that week finds the claim taken."""
    from pymongo.errors import DuplicateKeyError

    claim_id = f"mi_listen_digest:{brand_id}:{now.strftime('%G-W%V')}"
    try:
        await db["mi_listen_digest_claims"].insert_one({"_id": claim_id, "brand_id": brand_id, "claimed_at": now})
        return True
    except DuplicateKeyError:
        return False


async def send_weekly_digests(db, cfg: dict, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    dl = cfg["delivery"]
    sent = skipped_empty = skipped_claimed = failed = 0

    async for prof in db["mi_listening_profiles"].find({"status": "active"}):
        brand_id = prof["brand_id"]
        if not await _claim(db, brand_id, now):
            skipped_claimed += 1
            continue
        cards = [c async for c in db["mi_listen_cards"].find(
            {"brand_id": brand_id, "status": {"$in": ["new", "shown", "saved"]},
             "expires_at": {"$gt": now}, "created_at": {"$gte": now - timedelta(days=7)}},
        ).sort([("scores.total", -1)]).limit(dl["digest_max_items"])]
        if not cards:
            skipped_empty += 1
            continue
        try:
            from app.core.config import settings
            from app.services import brand_notifier

            app_url = (settings.WEB_APP_URL or "https://www.urisocial.com").strip("'\"")
            names = [c.get("topic_name") or c["topic"] for c in cards]
            headline = names[0] if len(names) == 1 else f"{names[0]} and {len(names) - 1} more"
            await brand_notifier.notify(
                db, prof["user_id"], brand_id, "trending_topic",
                title=f"This week in your market: {headline}",
                message="; ".join(f"{c.get('topic_name') or c['topic']} ({c['type']})" for c in cards),
                link=f"{app_url}/workspace/?tab=market-intelligence",
                metadata={"mode": "weekly_digest", "card_ids": [c["id"] for c in cards]},
            )
            sent += 1
        except Exception as e:
            print(f"[MI][listen] weekly digest failed for {brand_id}: {e}")
            failed += 1
    return {"sent": sent, "skipped_empty": skipped_empty, "skipped_claimed": skipped_claimed, "failed": failed}

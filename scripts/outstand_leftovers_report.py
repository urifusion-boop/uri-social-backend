"""
Read-only report: what is still left over from Outstand (retired) in the database.

    python -m scripts.outstand_leftovers_report

Prints counts only (no customer details): connections still recorded as connected through
Outstand, by network; posts still carrying an Outstand post id and waiting; and how many posts
the scheduler marked "replaced" between 12 Nov 2026 and the fix. Changes nothing.
"""
import asyncio
import re
from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings
from app.database import with_current_docdb_password


async def main() -> None:
    db = AsyncIOMotorClient(with_current_docdb_password(settings.MONGODB_URI))[settings.MONGODB_DB]

    print("Connections recorded as connected through Outstand (platform, status, count):")
    async for r in db.social_connections.aggregate([
        {"$match": {"connected_via": "outstand"}},
        {"$group": {"_id": {"platform": "$platform", "status": "$connection_status"}, "n": {"$sum": 1}}},
        {"$sort": {"n": -1}},
    ]):
        print(f"  {r['_id'].get('platform')}  {r['_id'].get('status')}  {r['n']}")
    users = await db.social_connections.distinct("user_id", {"connected_via": "outstand", "connection_status": "active"})
    print(f"  customers with an active Outstand-recorded connection: {len(users)}")

    fb_direct = re.compile(r"^\d+_\d+$")
    now = datetime.utcnow()
    waiting = {"due later": 0, "already past due": 0}
    async for d in db.content_drafts.find(
        {"status": {"$in": ["scheduled", "confirming"]}, "platform_post_id": {"$exists": True, "$nin": [None, ""]}},
        {"platform_post_id": 1, "scheduled_date": 1},
    ):
        if fb_direct.match(str(d.get("platform_post_id"))):
            continue
        sd = d.get("scheduled_date")
        waiting["due later" if isinstance(sd, datetime) and sd > now else "already past due"] += 1
    print(f"Posts still carrying an Outstand post id and waiting: {waiting}")

    q = {"status": "replaced", "error_message": "Superseded by a newer draft for the same platform.",
         "updated_at": {"$gte": datetime(2026, 11, 12)}}
    n = await db.content_drafts.count_documents(q)
    u = await db.content_drafts.distinct("user_id", q)
    later = await db.content_drafts.count_documents({**q, "scheduled_date": {"$gt": now}})
    print(f"Posts the scheduler marked 'replaced' since 12 Nov: {n} (customers: {len(u)}; still due later: {later})")


if __name__ == "__main__":
    asyncio.run(main())

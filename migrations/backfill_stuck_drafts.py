"""
One-off: mark stuck drafts as image_failed=True.

Stuck = has_image:True but image_url is null/missing — these show infinite
shimmer in the frontend because the background task died silently before the
image_failed flag existed.
"""
import asyncio
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorClient

MONGODB_URI = "mongodb://urifusion:UriTest2024%21@4.221.74.63:27018/Uri_Insight?authSource=admin"
MONGODB_DB = "Uri_Insight"


async def run():
    client = AsyncIOMotorClient(MONGODB_URI)
    db = client[MONGODB_DB]

    query = {
        "has_image": True,
        "$or": [
            {"image_url": None},
            {"image_url": {"$exists": False}},
            {"image_url": ""},
        ],
    }

    cursor = db["content_drafts"].find(query)
    stuck = []
    async for doc in cursor:
        stuck.append({
            "id": doc.get("id"),
            "image_failed": doc.get("image_failed"),
            "updated_at": doc.get("updated_at"),
        })

    print(f"Found {len(stuck)} stuck draft(s):")
    for d in stuck:
        print(f"  id={d['id']}  image_failed={d['image_failed']}  updated_at={d['updated_at']}")

    if not stuck:
        print("Nothing to update.")
        return

    result = await db["content_drafts"].update_many(
        query,
        {"$set": {"image_failed": True, "updated_at": datetime.utcnow()}},
    )
    print(f"\nMarked {result.modified_count} draft(s) as image_failed=True")


if __name__ == "__main__":
    asyncio.run(run())

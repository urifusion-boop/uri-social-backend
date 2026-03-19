from datetime import datetime, timedelta
from typing import Any, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase


class CacheRepository:
    DEFAULT_CACHE_TTL = timedelta(hours=1)

    @staticmethod
    async def get_cache(db: AsyncIOMotorDatabase, cache_key: str) -> Optional[Any]:
        cached_data = await db["cache"].find_one({"cache_key": cache_key})
        if cached_data:
            expiry_date = cached_data.get("expires_at")
            if not expiry_date or expiry_date > datetime.utcnow():
                return cached_data["data"]
            else:
                await db["cache"].delete_one({"cache_key": cache_key})
        return None

    @staticmethod
    async def set_cache(
        db: AsyncIOMotorDatabase,
        cache_key: str,
        data: Any,
        ttl: timedelta = DEFAULT_CACHE_TTL,
    ) -> None:
        now = datetime.utcnow()
        cache_entry = {
            "cache_key": cache_key,
            "data": data,
            "created_at": now,
        }
        if ttl:
            cache_entry["expires_at"] = now + ttl
        await db["cache"].replace_one(
            {"cache_key": cache_key}, cache_entry, upsert=True
        )

    @staticmethod
    async def clear_cache(db: AsyncIOMotorDatabase, cache_key: str) -> bool:
        result = await db["cache"].delete_one({"cache_key": cache_key})
        await CacheRepository.clear_expired_cache(db)
        return result.deleted_count > 0

    @staticmethod
    async def clear_expired_cache(db: AsyncIOMotorDatabase) -> int:
        result = await db["cache"].delete_many(
            {"expires_at": {"$lt": datetime.utcnow()}}
        )
        return result.deleted_count

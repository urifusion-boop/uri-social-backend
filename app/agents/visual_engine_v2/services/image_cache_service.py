"""
V2-only cache for cleaned Path B product images.

PRD Section 10.2: "Cleaned product images are cached: once an uploaded photo
has had background removal / reframing applied, the cleaned version is
stored and reused, so we don't re-pay the cleanup cost on every post using
that product."

Keyed by (user_id, brand_id, raw image hash, cleanup_level, format) — the
same raw upload with the same cleanup settings and target format always
produces the same cleaned result, so a repeat upload can skip background
removal entirely. Lives in its own V2 collection, not on any V1 model.
"""
import hashlib
from datetime import datetime
from typing import Any, Dict, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

COLLECTION = "visual_engine_v2_image_cache"


def hash_image_bytes(image_data: bytes) -> str:
    return hashlib.sha256(image_data).hexdigest()


class ImageCacheServiceV2:
    @staticmethod
    async def get_cached(
        db: AsyncIOMotorDatabase, user_id: str, brand_id: str,
        image_hash: str, cleanup_level: str, format: str
    ) -> Optional[Dict[str, Any]]:
        doc = await db[COLLECTION].find_one({
            "user_id": user_id, "brand_id": brand_id, "image_hash": image_hash,
            "cleanup_level": cleanup_level, "format": format,
        })
        if not doc:
            return None
        print(f"✓ [Path B cache] Reusing cleaned image, skipped re-running cleanup (hash={image_hash[:12]}...)")
        return {"imagery_url": doc["imagery_url"], "path": "B", "cost": 0.0}

    @staticmethod
    async def store(
        db: AsyncIOMotorDatabase, user_id: str, brand_id: str,
        image_hash: str, cleanup_level: str, format: str, imagery_url: str
    ) -> None:
        await db[COLLECTION].update_one(
            {
                "user_id": user_id, "brand_id": brand_id, "image_hash": image_hash,
                "cleanup_level": cleanup_level, "format": format,
            },
            {"$set": {"imagery_url": imagery_url, "cached_at": datetime.utcnow()}},
            upsert=True,
        )

    @staticmethod
    async def has_any_for_brand(db: AsyncIOMotorDatabase, user_id: str, brand_id: str) -> bool:
        """
        PRD Section 10.1: "product_images[] missing → Route post to Path A
        (generate) instead of Path B." V1's brand profile has no
        product_images[] field to check (it was never built there either),
        so this uses V2's own cleaned-image cache as the signal instead —
        if this brand has ever had a Path B upload cleaned and cached, they
        have a usable product photo on file; if not, Path A is the sensible
        default to recommend.
        """
        doc = await db[COLLECTION].find_one({"user_id": user_id, "brand_id": brand_id})
        return doc is not None

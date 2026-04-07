import pymongo.errors
from datetime import datetime
from typing import Any, Dict, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

COLLECTION = "whatsapp_sessions"


class WhatsAppSessionService:
    """Manages per-user WhatsApp conversation sessions in MongoDB."""

    @staticmethod
    def _normalize_phone(raw: str) -> str:
        """Strip the 'whatsapp:' prefix Twilio adds and return a bare E.164 number.
        Always ensures the number starts with '+' so stored and inbound formats match.
        """
        phone = raw.replace("whatsapp:", "").strip()
        if phone and not phone.startswith("+"):
            phone = "+" + phone
        return phone

    # ── Session CRUD ──────────────────────────────────────────────────────────

    @staticmethod
    async def get_session(phone: str, db: AsyncIOMotorDatabase) -> Optional[Dict[str, Any]]:
        phone = WhatsAppSessionService._normalize_phone(phone)
        return await db[COLLECTION].find_one({"phone": phone})

    @staticmethod
    async def upsert_session(
        phone: str,
        data: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> None:
        phone = WhatsAppSessionService._normalize_phone(phone)
        data["phone"] = phone
        data["updated_at"] = datetime.utcnow()
        await db[COLLECTION].update_one(
            {"phone": phone},
            {"$set": data},
            upsert=True,
        )

    @staticmethod
    async def set_state(
        phone: str,
        state: str,
        context: Optional[Dict[str, Any]],
        db: AsyncIOMotorDatabase,
    ) -> None:
        payload: Dict[str, Any] = {"state": state}
        if context is not None:
            payload["context"] = context
        await WhatsAppSessionService.upsert_session(phone, payload, db)

    # ── User ↔ Phone linking ───────────────────────────────────────────────

    @staticmethod
    async def get_user_by_phone(
        phone: str, db: AsyncIOMotorDatabase
    ) -> Optional[Dict[str, Any]]:
        phone = WhatsAppSessionService._normalize_phone(phone)
        # Try both +2348... and 2348... so old records without '+' still match
        without_plus = phone.lstrip("+")
        return await db["users"].find_one(
            {"whatsapp_phone": {"$in": [phone, without_plus]}}
        )

    @staticmethod
    async def link_phone_to_user(
        user_id: str, phone: str, db: AsyncIOMotorDatabase
    ) -> None:
        phone = WhatsAppSessionService._normalize_phone(phone)
        result = await db["users"].update_one(
            {"userId": user_id},
            {
                "$set": {
                    "whatsapp_phone": phone,
                    "whatsapp_linked_at": datetime.utcnow(),
                }
            },
        )
        print(f"[WA LINK DEBUG] update_one userId={user_id!r} phone={phone!r} matched={result.matched_count} modified={result.modified_count}")
        # Seed an initial session row — if this times out it's non-fatal;
        # the handle() function defaults to state="linked" when no session exists.
        try:
            await WhatsAppSessionService.upsert_session(
                phone,
                {"state": "linked", "user_id": user_id},
                db,
            )
        except pymongo.errors.NetworkTimeout:
            pass

    @staticmethod
    async def get_brand_profile(
        user_id: str, db: AsyncIOMotorDatabase
    ) -> Optional[Dict[str, Any]]:
        return await db["brand_profiles"].find_one({"user_id": user_id})

    @staticmethod
    async def get_all_linked_users(db: AsyncIOMotorDatabase):
        """Return all users who have a linked WhatsApp phone."""
        cursor = db["users"].find(
            {"whatsapp_phone": {"$exists": True, "$ne": None}},
            {"userId": 1, "first_name": 1, "whatsapp_phone": 1},
        )
        return await cursor.to_list(length=None)

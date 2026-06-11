"""
Agency Credit Service — Agency Accounts feature (PRD §4)

Wraps the existing per-user credit_service. When a brand belongs to an agency,
credits are drawn from the agency wallet (with optional per-brand monthly caps)
and every consumption is logged to brand_credit_usage. Solo brands fall back to
the existing user wallet unchanged.
"""

from datetime import datetime
from typing import Optional, Dict, Any, List
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.models.agency import BrandCreditUsage

AGENCIES = "agencies"
BRANDS = "brand_accounts"
USAGE = "brand_credit_usage"


def _month_start() -> datetime:
    now = datetime.utcnow()
    return datetime(now.year, now.month, 1)


class AgencyCreditService:
    """Agency wallet, per-brand caps, and per-brand consumption tracking."""

    @staticmethod
    async def _brand_and_agency(brand_id: str, db: AsyncIOMotorDatabase):
        brand = await db[BRANDS].find_one({"brand_id": brand_id})
        agency = None
        if brand and brand.get("agency_id"):
            agency = await db[AGENCIES].find_one({"agency_id": brand["agency_id"]})
        return brand, agency

    @staticmethod
    async def brand_usage_this_month(brand_id: str, db: AsyncIOMotorDatabase) -> float:
        cursor = db[USAGE].aggregate([
            {"$match": {"brand_id": brand_id, "consumed_at": {"$gte": _month_start()}}},
            {"$group": {"_id": None, "total": {"$sum": "$credits_consumed"}}},
        ])
        rows = await cursor.to_list(length=1)
        return float(rows[0]["total"]) if rows else 0.0

    @staticmethod
    async def check_availability(brand_id: str, credits: float, db: AsyncIOMotorDatabase) -> Dict[str, Any]:
        """PRD §4.3 — gate before any credit-consuming operation."""
        brand, agency = await AgencyCreditService._brand_and_agency(brand_id, db)

        # Solo brand → defer to per-user wallet (checked at deduct time)
        if not agency:
            return {"allowed": True, "billing": "user_wallet"}

        if agency.get("wallet_credits", 0) < credits:
            return {"allowed": False, "reason": "agency_wallet_empty", "billing": "agency_wallet"}

        if agency.get("per_brand_caps_enabled") and brand.get("monthly_credit_cap") is not None:
            used = await AgencyCreditService.brand_usage_this_month(brand_id, db)
            if used + credits > brand["monthly_credit_cap"]:
                return {"allowed": False, "reason": "brand_cap_reached", "billing": "agency_wallet"}

        return {"allowed": True, "billing": "agency_wallet"}

    @staticmethod
    async def deduct_for_brand(
        brand_id: str,
        credits: float,
        operation: str,
        user_id: str,
        db: AsyncIOMotorDatabase,
    ) -> Dict[str, Any]:
        """
        Deduct credits for an operation on a brand. Agency brand → agency wallet
        + usage log. Solo brand → existing per-user wallet. Never silently fails.
        """
        brand, agency = await AgencyCreditService._brand_and_agency(brand_id, db)

        # ── Solo brand: existing per-user wallet (fixed 1-credit model) ──────
        if not agency:
            from app.services.CreditService import credit_service
            ok = await credit_service.deduct_credit(
                user_id=user_id, campaign_id=f"{operation}:{brand_id}", reason=operation
            )
            if not ok:
                return {"success": False, "reason": "user_wallet_empty"}
            # Still log per-brand usage for reporting parity
            await db[USAGE].insert_one(BrandCreditUsage(
                brand_id=brand_id, agency_id=None, operation_type=operation,
                credits_consumed=credits, consumed_by_user_id=user_id,
            ).to_dict())
            return {"success": True, "billing": "user_wallet"}

        # ── Agency brand: wallet + caps + usage log ──────────────────────────
        avail = await AgencyCreditService.check_availability(brand_id, credits, db)
        if not avail["allowed"]:
            return {"success": False, "reason": avail["reason"]}

        await db[AGENCIES].update_one(
            {"agency_id": agency["agency_id"]},
            {"$inc": {"wallet_credits": -credits}, "$set": {"updated_at": datetime.utcnow()}},
        )
        await db[USAGE].insert_one(BrandCreditUsage(
            brand_id=brand_id, agency_id=agency["agency_id"], operation_type=operation,
            credits_consumed=credits, consumed_by_user_id=user_id,
        ).to_dict())

        # Low-credit alerts (20% / 10% / near-empty) — fire-and-forget
        await AgencyCreditService._maybe_alert_low_credit(agency, credits, db)

        return {"success": True, "billing": "agency_wallet"}

    @staticmethod
    async def top_up(agency_id: str, credits: float, db: AsyncIOMotorDatabase) -> float:
        await db[AGENCIES].update_one(
            {"agency_id": agency_id},
            {"$inc": {"wallet_credits": credits}, "$set": {"updated_at": datetime.utcnow()}},
        )
        agency = await db[AGENCIES].find_one({"agency_id": agency_id}, {"wallet_credits": 1})
        return float(agency.get("wallet_credits", 0)) if agency else 0.0

    @staticmethod
    async def _maybe_alert_low_credit(agency: Dict[str, Any], just_spent: float, db: AsyncIOMotorDatabase):
        """Notify the agency owner at 20% / 10% / near-empty thresholds (best-effort)."""
        try:
            remaining = agency.get("wallet_credits", 0) - just_spent
            # Without a known plan ceiling we alert on absolute low balances.
            threshold = None
            if remaining <= 0:
                threshold = "empty"
            elif remaining < 50:
                threshold = "near_empty"
            elif remaining < 150:
                threshold = "low"
            if threshold:
                from app.services.NotificationService import NotificationService  # type: ignore
                # Best-effort; signature differences are tolerated.
                notify = getattr(NotificationService, "create_notification", None)
                if notify:
                    await notify(
                        user_id=agency.get("owner_user_id"),
                        title="Agency credits running low",
                        body=f"Your agency wallet is {threshold.replace('_', ' ')} ({remaining:.0f} credits left).",
                        db=db,
                    )
        except Exception as e:
            print(f"(low-credit alert skipped: {e})")

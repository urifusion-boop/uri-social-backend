"""
Uri Market Intelligence — workspace budget reservation (PRD §23, P0-15).

"Before a scan, reserve a conservative maximum cost from the workspace
allowance. Stop enqueueing when the cap is reached, reconcile actual charges
and release unused reservation. Avoid uncapped supplier calls in P0."

reserve_budget() uses a single atomic `find_one_and_update` with the
allowance check baked into the query filter (via $expr) rather than a
read-then-write — MongoDB only applies the update when the filter matches,
so two concurrent reservations against the same near-exhausted budget can
never both succeed and push spend past the allowance (PRD P0-15:
"Concurrent jobs cannot reserve beyond allowance"). This guarantee comes
from MongoDB's document-level atomicity, not from anything Python-level, so
it holds under real concurrent requests even though this module's own test
suite (single-process, no real concurrency) can only exercise the reserve/
release/reconcile logic itself, not a genuine race.
"""
from __future__ import annotations

from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorDatabase

from .models import BrandBudget


def _current_period(now: datetime | None = None) -> str:
    return (now or datetime.utcnow()).strftime("%Y-%m")


async def get_or_create_budget(db: AsyncIOMotorDatabase, brand_id: str) -> dict:
    """Creates a default-allowance budget on first use, and resets usage
    (never the allowance itself) when the calendar month has rolled over."""
    now = datetime.utcnow()
    period = _current_period(now)

    existing = await db["mi_budgets"].find_one({"brand_id": brand_id})
    if existing is None:
        budget = BrandBudget(brand_id=brand_id, period=period)
        await db["mi_budgets"].insert_one(budget.dict())
        return budget.dict()

    if existing.get("period") != period:
        await db["mi_budgets"].update_one(
            {"brand_id": brand_id},
            {"$set": {"period": period, "reserved_usd": 0.0, "spent_usd": 0.0, "updated_at": now}},
        )
        existing.update({"period": period, "reserved_usd": 0.0, "spent_usd": 0.0})
    return existing


async def reserve_budget(db: AsyncIOMotorDatabase, brand_id: str, amount_usd: float) -> tuple[bool, str]:
    """Atomically reserves `amount_usd` if doing so would not exceed the
    brand's monthly allowance. Returns (False, reason) — never raises — so
    callers can route straight to ScanStatus.BUDGET_LIMITED."""
    budget = await get_or_create_budget(db, brand_id)
    if amount_usd <= 0:
        return True, "no reservation needed for a zero-cost scan"

    result = await db["mi_budgets"].find_one_and_update(
        {
            "brand_id": brand_id,
            "period": budget["period"],
            "$expr": {
                "$lte": [
                    {"$add": ["$reserved_usd", "$spent_usd", amount_usd]},
                    "$monthly_allowance_usd",
                ]
            },
        },
        {"$inc": {"reserved_usd": amount_usd}, "$set": {"updated_at": datetime.utcnow()}},
    )
    if result is None:
        return False, (
            f"reserving ${amount_usd:.2f} would exceed the ${budget['monthly_allowance_usd']:.2f} "
            f"monthly allowance (already reserved ${budget['reserved_usd']:.2f}, "
            f"spent ${budget['spent_usd']:.2f} this period)"
        )
    return True, "reserved"


async def reconcile_spend(db: AsyncIOMotorDatabase, brand_id: str, reserved_usd: float, actual_usd: float) -> None:
    """PRD §23: 'reconcile actual charges and release unused reservation.'
    Releases exactly what was reserved and books the actual charge, so the
    allowance is consumed by actual_usd net — never drifts even if actual
    differs from the original estimate."""
    if reserved_usd <= 0 and actual_usd <= 0:
        return
    await db["mi_budgets"].update_one(
        {"brand_id": brand_id},
        {"$inc": {"reserved_usd": -reserved_usd, "spent_usd": actual_usd}, "$set": {"updated_at": datetime.utcnow()}},
    )

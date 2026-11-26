"""Owner alerts: sent once, only for things that matter, never able to break the job."""
import asyncio
from datetime import timedelta

from mongomock_motor import AsyncMongoMockClient

from app.agents.catalog import alerts, indexes, items, reservations, stock
from app.agents.catalog.models import utcnow

B = "biz1"


def run(c):
    return asyncio.run(c)


async def fresh():
    db = AsyncMongoMockClient()["t"]
    indexes.reset()
    await indexes.ensure_indexes(db)
    indexes.reset()
    return db


def test_low_and_empty_stock_notify_the_owner_once_a_day():
    async def go():
        db = await fresh()
        await items.create_item(db, B, {"name": "Shoe", "description": "d", "item_type": "physical", "category": "c",
                                        "price": 1, "initial_quantity": 2, "low_stock_threshold": 3}, "owner1")
        await items.create_item(db, B, {"name": "Cap", "description": "d", "item_type": "physical", "category": "c",
                                        "price": 1, "initial_quantity": 0, "sku": "cap"}, "owner1")
        sent = []

        async def notifier(db_, user, brand, event, title, message):
            sent.append((user, event, title))
        assert await alerts.send_alerts(db, notifier) == 2
        assert await alerts.send_alerts(db, notifier) == 0
        assert {t for _, _, t in sent} == {"Shoe is running low", "Cap is out of stock"}
        assert all(u == "owner1" and e == "catalog_alert" for u, e, _ in sent)
        later = utcnow() + timedelta(days=1)
        assert await alerts.send_alerts(db, notifier, now=later) == 2
    run(go())


def test_a_failing_notifier_never_breaks_the_job():
    async def go():
        db = await fresh()
        await items.create_item(db, B, {"name": "Cap", "description": "d", "item_type": "physical", "category": "c",
                                        "price": 1, "initial_quantity": 0}, "o")

        async def boom(*a):
            raise RuntimeError("smtp down")
        assert await alerts.send_alerts(db, boom) == 0
    run(go())


def test_thin_capacity_expiring_holds_and_stale_availability_show_in_the_dashboard():
    async def go():
        db = await fresh()
        tour = await items.create_item(db, B, {"name": "Tour", "description": "d", "item_type": "capacity", "category": "c",
                                               "price": 1, "availability_config": {"low_capacity_threshold": 5}}, "o")
        start = utcnow() + timedelta(days=10)
        await stock.upsert_slot(db, B, tour["_id"], start, start + timedelta(days=1), 5)
        await reservations.reserve(db, B, tour["_id"], 1, start_at=start)
        shoe = await items.create_item(db, B, {"name": "Shoe", "description": "d", "item_type": "physical", "category": "c",
                                               "price": 1, "initial_quantity": 5, "reservation_minutes": 10}, "o")
        await reservations.reserve(db, B, shoe["_id"], 1)
        old = await items.create_item(db, B, {"name": "Hotel", "description": "d", "item_type": "capacity", "category": "c",
                                              "price": 1, "availability_type": "manual", "sku": "h"}, "o")
        await db["catalog_items"].update_one({"_id": old["_id"]}, {"$set": {"availability_updated_at": utcnow() - timedelta(days=3)}})
        kinds = sorted(a["type"] for a in await alerts.compute_alerts(db, B))
        assert kinds == ["capacity_low", "reservation_expiring", "stale_availability"]
        assert [a for a in await alerts.compute_alerts(db, "other")] == []
    run(go())


def test_unusually_large_adjustments_are_flagged():
    assert alerts.is_unusual(10, 60) and alerts.is_unusual(200, -100) and not alerts.is_unusual(200, -20)

"""Catalog & Availability: availability models, atomic reservations, ledger, lifecycle."""
import asyncio
from datetime import datetime, timedelta

import pytest
from mongomock_motor import AsyncMongoMockClient

from app.agents.catalog import availability, events, indexes, items, reservations, stock
from app.agents.catalog.models import CatalogError, normalize_item, utcnow

B = "biz1"


def run(c):
    return asyncio.run(c)


async def db_():
    db = AsyncMongoMockClient()["t"]
    indexes.reset()
    await indexes.ensure_indexes(db)
    indexes.reset()
    return db


def raw(**over):
    base = {"name": "Air Max", "description": "Running shoe", "item_type": "physical", "category": "footwear",
            "price": 45000, "initial_quantity": 10}
    base.update(over)
    return base


async def make(db, **over):
    return await items.create_item(db, B, raw(**over), "u1")


def test_validation_rejects_bad_items():
    for bad in ({"name": ""}, {"description": ""}, {"category": ""}, {"item_type": "boat"}, {"price": -1},
                {"price": None}, {"currency": "naira"}, {"availability_type": "capacity"},
                {"item_type": "digital", "availability_type": "capped"}):
        with pytest.raises(CatalogError):
            normalize_item({**raw(), **bad})
    assert normalize_item({**raw(), "price": None, "pricing_model": "quote"})["price"] is None


def test_physical_stock_reserve_commit_and_ledger_reconciles():
    async def go():
        db = await db_()
        item = await make(db)
        r = await reservations.reserve(db, B, item["_id"], 3, actor="ai", idempotency_key="k1")
        row = await stock.get_stock(db, B, item["_id"])
        assert (row["on_hand"], row["reserved"], row["available"]) == (10, 3, 7)
        out = await availability.check(db, B, item["_id"], quantity=8)
        assert out["available"] is False and out["available_quantity"] == 7 and out["reason"] == "not_enough_stock"
        await reservations.commit(db, B, r["_id"], "pay")
        row = await stock.get_stock(db, B, item["_id"])
        assert (row["on_hand"], row["reserved"], row["available"]) == (7, 0, 7)
        assert (await stock.reconcile(db, B, item["_id"]))["matches"]
    run(go())


def test_only_one_of_two_customers_gets_the_last_unit():
    async def go():
        db = await db_()
        item = await make(db, initial_quantity=1)
        results = await asyncio.gather(*[
            reservations.reserve(db, B, item["_id"], 1, idempotency_key=f"k{i}") for i in range(5)],
            return_exceptions=True)
        won = [r for r in results if not isinstance(r, Exception)]
        assert len(won) == 1
        assert all(isinstance(r, CatalogError) and r.status == 409 for r in results if isinstance(r, Exception))
        assert (await stock.get_stock(db, B, item["_id"]))["available"] == 0
    run(go())


def test_reservations_are_idempotent():
    async def go():
        db = await db_()
        item = await make(db)
        a = await reservations.reserve(db, B, item["_id"], 2, idempotency_key="same")
        b = await reservations.reserve(db, B, item["_id"], 2, idempotency_key="same")
        assert a["_id"] == b["_id"] and b.get("idempotent_replay")
        assert (await stock.get_stock(db, B, item["_id"]))["reserved"] == 2
    run(go())


def test_expired_unpaid_reservation_is_released_and_late_payment_retakes_it():
    async def go():
        db = await db_()
        item = await make(db, reservation_minutes=15)
        t0 = utcnow()
        r = await reservations.reserve(db, B, item["_id"], 4, now=t0)
        assert await reservations.expire_due(db, B, now=t0 + timedelta(minutes=16)) == 1
        assert (await stock.get_stock(db, B, item["_id"]))["available"] == 10
        assert (await reservations.get_reservation(db, B, r["_id"]))["status"] == "expired"
        # Paid late and the units are still free: re-held and sold.
        done = await reservations.commit(db, B, r["_id"], "pay", now=t0 + timedelta(minutes=20))
        assert done["status"] == "confirmed" and (await stock.get_stock(db, B, item["_id"]))["on_hand"] == 6
        # Paid late and the units are gone: refuse with a clear reason.
        item2 = await make(db, name="One", initial_quantity=1, sku="ONE")
        r2 = await reservations.reserve(db, B, item2["_id"], 1, now=t0)
        await reservations.expire_due(db, B, now=t0 + timedelta(minutes=31))
        await reservations.reserve(db, B, item2["_id"], 1)
        with pytest.raises(CatalogError) as e:
            await reservations.commit(db, B, r2["_id"], "pay")
        assert e.value.reason == "expired_unavailable"
    run(go())


def test_a_transfer_in_progress_extends_once_instead_of_releasing():
    async def go():
        db = await db_()
        item = await make(db, reservation_minutes=15)
        t0 = utcnow()
        r = await reservations.reserve(db, B, item["_id"], 1, now=t0)
        await reservations.mark_payment_processing(db, B, r["_id"])
        assert await reservations.expire_due(db, B, now=t0 + timedelta(minutes=16)) == 0
        assert (await reservations.get_reservation(db, B, r["_id"]))["status"] == "pending"
        assert await reservations.expire_due(db, B, now=t0 + timedelta(minutes=60)) == 1
    run(go())


def test_release_cancel_and_fulfilled_never_restock_themselves():
    async def go():
        db = await db_()
        item = await make(db)
        r = await reservations.reserve(db, B, item["_id"], 2)
        await reservations.release(db, B, r["_id"], "changed mind")
        assert (await stock.get_stock(db, B, item["_id"]))["available"] == 10
        r2 = await reservations.reserve(db, B, item["_id"], 2)
        await reservations.commit(db, B, r2["_id"])
        await reservations.release(db, B, r2["_id"], "cancelled before shipping")      # unfulfilled: restocked
        assert (await stock.get_stock(db, B, item["_id"]))["on_hand"] == 10
        r3 = await reservations.reserve(db, B, item["_id"], 2)
        await reservations.commit(db, B, r3["_id"])
        await reservations.fulfil(db, B, r3["_id"])
        with pytest.raises(CatalogError) as e:
            await reservations.release(db, B, r3["_id"])
        assert e.value.reason == "fulfilled_no_restock"
        assert (await stock.get_stock(db, B, item["_id"]))["on_hand"] == 8
        assert (await stock.reconcile(db, B, item["_id"]))["matches"]
    run(go())


def test_stock_adjustments_are_recorded_and_reserved_units_are_protected():
    async def go():
        db = await db_()
        item = await make(db)
        await reservations.reserve(db, B, item["_id"], 8)
        with pytest.raises(CatalogError):
            await stock.adjust(db, B, item["_id"], None, "lost", quantity=5, reason="stolen")
        with pytest.raises(CatalogError):
            await stock.adjust(db, B, item["_id"], None, "adjusted", quantity_change=-1)       # no reason
        await stock.adjust(db, B, item["_id"], None, "damaged", quantity=2, reason="water damage", actor="u1")
        await stock.adjust(db, B, item["_id"], None, "received", quantity=5, reason="new batch", actor="u1")
        row = await stock.get_stock(db, B, item["_id"])
        assert (row["on_hand"], row["available"]) == (13, 5)
        assert (await stock.reconcile(db, B, item["_id"]))["matches"]
    run(go())


def test_variants_have_their_own_stock_and_must_be_chosen():
    async def go():
        db = await db_()
        item = await make(db, initial_quantity=None)
        v42 = await items.create_variant(db, B, item["_id"], {"name": "Black / 42", "attributes": {"size": "42"},
                                                               "initial_quantity": 3}, "u1")
        await items.create_variant(db, B, item["_id"], {"name": "Black / 43", "initial_quantity": 0}, "u1")
        with pytest.raises(CatalogError) as e:
            await reservations.reserve(db, B, item["_id"], 1)
        assert e.value.reason == "variant_required"
        out = await availability.check(db, B, item["_id"])
        assert out["variant_required"] and [v["available_quantity"] for v in out["variants"]] == [3, 0]
        assert (await availability.check(db, B, item["_id"], variant_id=v42["_id"], quantity=3))["available"]
        await reservations.reserve(db, B, item["_id"], 3, variant_id=v42["_id"])
        assert not (await availability.check(db, B, item["_id"], variant_id=v42["_id"]))["available"]
    run(go())


def test_unlimited_and_capped_digital_products():
    async def go():
        db = await db_()
        ebook = await make(db, name="E-book", item_type="digital", availability_type="unlimited", sku="EB")
        for _ in range(3):
            assert (await reservations.reserve(db, B, ebook["_id"], 50))["status"] == "pending"
        plan = await make(db, name="Starter plan", item_type="digital", availability_type="capped", sku="ST",
                          availability_config={"cap": 2, "period": "month"}, billing_interval="monthly")
        a = await reservations.reserve(db, B, plan["_id"], 1)
        await reservations.reserve(db, B, plan["_id"], 1)
        with pytest.raises(CatalogError):
            await reservations.reserve(db, B, plan["_id"], 1)
        await reservations.commit(db, B, a["_id"])
        assert not (await availability.check(db, B, plan["_id"]))["available"]       # cap reached, stays reached
        # Next month the cap starts over.
        nxt = utcnow() + timedelta(days=40)
        assert (await availability.check(db, B, plan["_id"], now=nxt))["available_quantity"] == 2
    run(go())


def test_capacity_seats_per_departure():
    async def go():
        db = await db_()
        tour = await make(db, name="Zanzibar", item_type="capacity", availability_type="capacity", sku="ZNZ")
        start = utcnow() + timedelta(days=30)
        slot = await stock.upsert_slot(db, B, tour["_id"], start, start + timedelta(days=5), 3, label="Dec departure")
        out = await availability.check(db, B, tour["_id"], quantity=2, start_at=start)
        assert out["available"] and out["available_quantity"] == 3
        a = await reservations.reserve(db, B, tour["_id"], 2, start_at=start)
        with pytest.raises(CatalogError) as e:
            await reservations.reserve(db, B, tour["_id"], 2, start_at=start)
        assert e.value.extra["available"] == 1
        await reservations.reserve(db, B, tour["_id"], 1, start_at=start)
        await reservations.release(db, B, a["_id"])
        assert (await availability.check(db, B, tour["_id"], start_at=start))["available_quantity"] == 2
        listing = await availability.check(db, B, tour["_id"])
        assert listing["needs_time"] and listing["slots"][0]["slot_id"] == slot["_id"]
    run(go())


def test_appointments_cannot_be_double_booked():
    async def go():
        db = await db_()
        cut = await make(db, name="Consultation", item_type="service", availability_type="schedule", sku="CON",
                         availability_config={"days": [0, 1, 2, 3, 4, 5, 6], "start": "09:00", "end": "12:00",
                                              "slot_minutes": 60, "timezone": "UTC"})
        out = await availability.check(db, B, cut["_id"])
        assert out["slots"], "slots are generated from the weekly hours"
        first = out["slots"][0]["start_at"]
        results = await asyncio.gather(
            reservations.reserve(db, B, cut["_id"], 1, start_at=first, idempotency_key="a"),
            reservations.reserve(db, B, cut["_id"], 1, start_at=first, idempotency_key="b"),
            return_exceptions=True)
        assert sum(not isinstance(r, Exception) for r in results) == 1
        after = await availability.check(db, B, cut["_id"], start_at=first)
        assert after["available"] is False and after["reason"] == "slot_full"
    run(go())


def test_schedule_respects_blackouts_notice_and_advance_limit():
    async def go():
        db = await db_()
        today = utcnow().date()
        dates = [(today + timedelta(days=i)).isoformat() for i in range(0, 4)]
        svc = await make(db, name="Fix", item_type="service", availability_type="schedule", sku="FIX",
                         availability_config={"days": list(range(7)), "start": "09:00", "end": "11:00", "slot_minutes": 60,
                                              "timezone": "UTC", "blackout_dates": dates[:2],
                                              "advance_booking_days": 5, "min_notice_hours": 1})
        out = await availability.check(db, B, svc["_id"])
        days = {s["start_at"][:10] for s in out["slots"]}
        assert not days & set(dates[:2])
        assert all(d <= (today + timedelta(days=6)).isoformat() for d in days)
    run(go())


def test_manual_confirmation_never_claims_a_booking():
    async def go():
        db = await db_()
        gig = await make(db, name="Bespoke design", item_type="service", availability_type="manual", sku="BD",
                         availability_config={"available": True})
        out = await availability.check(db, B, gig["_id"])
        assert out["available"] and out["requires_confirmation"]
        r = await reservations.reserve(db, B, gig["_id"], 1)
        assert r["status"] == "awaiting_confirmation"
        with pytest.raises(CatalogError) as e:
            await reservations.commit(db, B, r["_id"])
        assert e.value.reason == "needs_approval"
        assert (await reservations.approve(db, B, r["_id"], "owner"))["status"] == "pending"
        assert (await reservations.commit(db, B, r["_id"]))["status"] == "confirmed"
    run(go())


def test_stale_manual_availability_follows_the_configured_fallback():
    async def go():
        db = await db_()
        old = utcnow() - timedelta(days=3)
        for fallback, want_available, want_confirm in (("require_confirmation", True, True),
                                                       ("assume_unavailable", False, True), ("allow", True, True)):
            item = await make(db, name=f"Seats {fallback}", item_type="capacity", availability_type="manual",
                              sku=fallback, availability_config={"available": True, "stale_fallback": fallback})
            await db["catalog_items"].update_one({"_id": item["_id"]}, {"$set": {"availability_updated_at": old}})
            out = await availability.check(db, B, item["_id"])
            assert out["stale"] and out["available"] is want_available and out["requires_confirmation"] is want_confirm
        fresh = await make(db, name="Fresh", item_type="capacity", availability_type="manual", sku="fresh")
        assert not (await availability.check(db, B, fresh["_id"]))["stale"]
    run(go())


def test_made_to_order_capacity_and_lead_time():
    async def go():
        db = await db_()
        table = await make(db, name="Dining table", item_type="made_to_order", availability_type="made_to_order",
                           sku="TBL", requires_confirmation=True,
                           availability_config={"max_orders": 2, "period": "month", "lead_time_days": 21,
                                                "deposit_percent": 50})
        out = await availability.check(db, B, table["_id"])
        assert out["available_quantity"] == 2 and out["lead_time_days"] == 21 and out["deposit_percent"] == 50
        await reservations.reserve(db, B, table["_id"], 1)
        await reservations.reserve(db, B, table["_id"], 1)
        assert (await availability.check(db, B, table["_id"]))["reason"] == "production_full"
        none = await make(db, name="Cake", item_type="made_to_order", sku="CK")
        assert (await availability.check(db, B, none["_id"]))["available"]
        assert "estimated_ready" not in (await availability.check(db, B, none["_id"]))     # never promise an undated finish
    run(go())


def test_changing_the_availability_model_is_blocked_while_commitments_exist():
    async def go():
        db = await db_()
        item = await make(db)
        r = await reservations.reserve(db, B, item["_id"], 1)
        with pytest.raises(CatalogError) as e:
            await items.update_item(db, B, item["_id"], {"availability_type": "unlimited"}, "u1")
        assert e.value.reason == "has_commitments"
        await reservations.release(db, B, r["_id"])
        assert (await items.update_item(db, B, item["_id"], {"availability_type": "unlimited"}, "u1"))["availability_type"] == "unlimited"
    run(go())


def test_tenants_cannot_see_each_others_items_and_cost_stays_private():
    async def go():
        db = await db_()
        item = await make(db, cost_price=20000, sales_notes="push the black pair")
        with pytest.raises(CatalogError) as e:
            await items.get_item(db, "other", item["_id"])
        assert e.value.status == 404
        assert "cost_price" in items.serialise(item, "owner")
        assert "cost_price" not in items.serialise(item, "staff")
        customer = items.serialise(item, "customer")
        assert "cost_price" not in customer and "sales_notes" not in customer
        assert (await items.list_items(db, "other"))["total"] == 0
    run(go())


def test_sku_must_be_unique_per_business():
    async def go():
        db = await db_()
        await make(db, sku="A1")
        with pytest.raises(CatalogError) as e:
            await make(db, name="Other", sku="A1")
        assert e.value.reason == "duplicate_sku"
        await items.create_item(db, "biz2", raw(sku="A1"), "u")
    run(go())


def test_events_are_deduplicated_and_retried():
    async def go():
        db = await db_()
        events.unsubscribe_all()
        seen, fail = [], {"n": 1}

        async def sub(ev):
            if fail["n"]:
                fail["n"] -= 1
                raise RuntimeError("down")
            seen.append(ev["type"])
        events.subscribe(sub)
        await events.emit(db, B, "x.y", {}, dedupe_key="same")
        assert await events.emit(db, B, "x.y", {}, dedupe_key="same") is None
        assert (await events.dispatch_pending(db))["failed"] == 1
        assert (await events.dispatch_pending(db))["delivered"] == 1 and seen == ["x.y"]
        assert (await events.dispatch_pending(db)) == {"delivered": 0, "failed": 0}
        events.unsubscribe_all()
    run(go())

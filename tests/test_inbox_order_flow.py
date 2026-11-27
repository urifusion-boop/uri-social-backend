"""Jane starts an order from buying intent; only the customer's own yes, after seeing the summary, confirms it."""
import asyncio
from datetime import timedelta

from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.agents.catalog import indexes as catalog_indexes, items
from app.agents.inbox import order_flow
from app.agents.inbox.entities import now
from app.agents.orders import indexes as order_indexes
from app.agents.orders.models import ORDERS

W = "w1"


def run(c):
    return asyncio.run(c)


async def setup():
    db = AsyncMongoMockClient()["t"]
    catalog_indexes.reset()
    order_indexes.reset()
    shoe = await items.create_item(db, W, {"name": "Air Max", "description": "Shoe", "item_type": "physical", "category": "shoes",
                                           "price": 45000, "initial_quantity": 2, "sku": "AM"}, "u")
    conv = {"_id": ObjectId(), "display_name": "Ada", "platform": "instagram", "kind": "dm"}
    return db, shoe, conv, str(conv["_id"])


def llm_says(intent, items_=None):
    async def llm(system, user):
        return {"intent": intent, "items": items_ or []}
    return llm


def msg(direction, text, at=None):
    return {"id": text, "direction": direction, "text": text, "received_at": at or now()}


def test_buying_intent_makes_a_summary_and_reserves_nothing():
    async def go():
        db, shoe, conv, cid = await setup()
        out = await order_flow.step(db, W, conv, cid, "I'll take the air max", [msg("inbound", "I'll take the air max")], "m1",
                                    llm_says("order", [{"name": "Air Max", "quantity": 1}]))
        assert "ORDER SUMMARY ORD-00001" in out[0]["text"] and "₦45,000" in out[0]["text"] and "not confirmed" in out[0]["text"]
        order = await db[ORDERS].find_one({})
        assert order["status"] == "awaiting_customer" and order["source_ref"] == cid
        assert (await db["catalog_stock"].find_one({"item_id": shoe["_id"]}))["reserved"] == 0
        again = await order_flow.step(db, W, conv, cid, "I'll take the air max", [msg("inbound", "I'll take the air max")], "m1",
                                      llm_says("order", [{"name": "Air Max", "quantity": 1}]))
        assert await db[ORDERS].count_documents({}) == 1 and again
    run(go())


def test_a_yes_only_confirms_after_the_customer_was_sent_the_summary():
    async def go():
        db, shoe, conv, cid = await setup()
        await order_flow.step(db, W, conv, cid, "air max please", [msg("inbound", "air max please")], "m1",
                              llm_says("order", [{"name": "Air Max", "quantity": 1}]))
        yes = [msg("inbound", "air max please"), msg("inbound", "yes")]
        out = await order_flow.step(db, W, conv, cid, "yes", yes, "m2", llm_says("accept"))
        assert "OPEN ORDER" in out[0]["text"], "no reply with the summary went out yet, so a yes confirms nothing"
        assert (await db[ORDERS].find_one({}))["status"] == "awaiting_customer"
        sent = [msg("inbound", "air max please"), msg("outbound", "Summary...", now() + timedelta(seconds=5)), msg("inbound", "yes")]
        out = await order_flow.step(db, W, conv, cid, "yes", sent, "m3", llm_says("accept"))
        assert "CONFIRMED" in out[0]["text"] and "NOT been received" in out[0]["text"]
        assert (await db[ORDERS].find_one({}))["status"] == "confirmed"
        assert (await db["catalog_stock"].find_one({"item_id": shoe["_id"]}))["reserved"] == 1
    run(go())


def test_a_failed_confirmation_is_reported_not_promised():
    async def go():
        db, shoe, conv, cid = await setup()
        await order_flow.step(db, W, conv, cid, "2 air max", [msg("inbound", "2 air max")], "m1",
                              llm_says("order", [{"name": "Air Max", "quantity": 2}]))
        await items.update_item(db, W, shoe["_id"], {"price": 50000}, "u")
        sent = [msg("outbound", "Summary", now() + timedelta(seconds=5)), msg("inbound", "yes")]
        out = await order_flow.step(db, W, conv, cid, "yes", sent, "m2", llm_says("accept"))
        assert "COULD NOT BE CONFIRMED" in out[0]["text"] and "price changed" in out[0]["text"]
        assert (await db[ORDERS].find_one({}))["status"] == "awaiting_customer"
    run(go())


def test_declining_cancels_and_questions_touch_nothing():
    async def go():
        db, shoe, conv, cid = await setup()
        assert await order_flow.step(db, W, conv, cid, "what time do you close", [], "m0", llm_says("none")) == []
        await order_flow.step(db, W, conv, cid, "air max", [msg("inbound", "air max")], "m1",
                              llm_says("order", [{"name": "Air Max", "quantity": 1}]))
        out = await order_flow.step(db, W, conv, cid, "never mind", [msg("inbound", "never mind")], "m2", llm_says("decline"))
        assert "CANCELLED" in out[0]["text"] and (await db[ORDERS].find_one({}))["status"] == "cancelled"
    run(go())


def test_an_unmatched_item_starts_no_order():
    async def go():
        db, shoe, conv, cid = await setup()
        out = await order_flow.step(db, W, conv, cid, "air max", [msg("inbound", "air max")], "m1",
                                    llm_says("order", [{"name": "Rocket Boots", "quantity": 1}]))
        assert "ORDER NOT STARTED" in out[0]["text"] and await db[ORDERS].count_documents({}) == 0
    run(go())


def test_asking_for_something_else_replaces_the_open_order_instead_of_confirming_it():
    async def go():
        db, shoe, conv, cid = await setup()
        hoodie = await items.create_item(db, W, {"name": "Hoodie", "description": "Black hoodie", "item_type": "physical", "category": "wear",
                                                 "price": 18000, "initial_quantity": 3, "sku": "HD"}, "u")
        await order_flow.step(db, W, conv, cid, "air max", [msg("inbound", "air max")], "m1",
                              llm_says("order", [{"name": "Air Max", "quantity": 1}]))
        sent = [msg("inbound", "air max"), msg("outbound", "Summary", now() + timedelta(seconds=5)), msg("inbound", "the hoodie, yes")]
        out = await order_flow.step(db, W, conv, cid, "the hoodie, yes", sent, "m2", llm_says("order", [{"name": "Hoodie", "quantity": 1}]))
        assert "ORDER SUMMARY ORD-00002" in out[0]["text"] and "Hoodie" in out[0]["text"] and "replaces ORD-00001" in out[0]["text"]
        statuses = {o["order_number"]: o["status"] for o in await db[ORDERS].find({}).to_list(None)}
        assert statuses == {"ORD-00001": "cancelled", "ORD-00002": "awaiting_customer"}
        for stock in await db["catalog_stock"].find({}).to_list(None):
            assert stock["reserved"] == 0
        out = await order_flow.step(db, W, conv, cid, "rocket", sent, "m3", llm_says("order", [{"name": "Rocket Boots", "quantity": 1}]))
        assert "ORD-00002" in out[0]["text"] and "unchanged" in out[0]["text"]
        assert (await db[ORDERS].find_one({"order_number": "ORD-00002"}))["status"] == "awaiting_customer"
        assert hoodie
    run(go())

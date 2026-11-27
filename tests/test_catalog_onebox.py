"""Jane answers from the live catalog: real prices and stock, never invented ones."""
import asyncio

from mongomock_motor import AsyncMongoMockClient

from app.agents.catalog import ai_tools, indexes, items, reservations
from app.agents.catalog.models import DEMAND, CatalogError
from app.agents.inbox import agent
from tests.test_inbox_agent import _cls, _draft, decide

B = "biz1"


def run(c):
    return asyncio.run(c)


async def fresh():
    db = AsyncMongoMockClient()["t"]
    indexes.reset()
    await indexes.ensure_indexes(db)
    indexes.reset()
    return db


async def shoe(db, **over):
    base = {"name": "Air Max", "description": "Running shoe", "item_type": "physical", "category": "footwear",
            "price": 45000, "cost_price": 20000, "sales_notes": "push the black pair", "sku": "AM"}
    base.update(over)
    return await items.create_item(db, B, base, "u1")


async def with_sizes(db):
    item = await shoe(db)
    await items.create_variant(db, B, item["_id"], {"name": "Black / 42", "attributes": {"size": "42", "colour": "black"},
                                                    "initial_quantity": 3}, "u1")
    await items.create_variant(db, B, item["_id"], {"name": "Black / 43", "attributes": {"size": "43", "colour": "black"},
                                                    "initial_quantity": 0}, "u1")
    return item


def test_a_stock_question_gets_live_numbers_the_price_and_no_secrets():
    async def go():
        db = await fresh()
        await with_sizes(db)
        src = (await ai_tools.catalog_sources(db, B, "Do you have the air max in black size 42?", message_id="m1"))[0]
        text = src["text"]
        assert src["approved"] and src["id"].startswith("catalog:") and src["price_or_stock"] is False
        assert "₦45,000" in text and "Black / 42: in stock" in text and "Black / 43: out of stock" in text
        assert "3 available" not in text, "the default hides exact counts from customers"
        assert "20000" not in text and "20,000" not in text, "cost price must never reach a customer-facing prompt"
        assert "push the black pair" in text and "never quote" in text
    run(go())


def test_an_unavailable_item_offers_alternatives_and_records_missed_demand_once():
    async def go():
        db = await fresh()
        out = await shoe(db, name="Dunk", sku="DK", initial_quantity=0)
        await shoe(db, name="Pegasus", sku="PG", initial_quantity=5, price=30000)
        for _ in range(2):
            src = (await ai_tools.catalog_sources(db, B, "is the dunk available", message_id="m9"))[0]
        assert "Out of stock" in src["text"] and "Pegasus (₦30,000)" in src["text"]
        assert await db[DEMAND].count_documents({"business_id": B, "item_id": out["_id"]}) == 1
    run(go())


def test_a_follow_up_without_the_name_still_finds_the_item():
    async def go():
        db = await fresh()
        await with_sizes(db)
        earlier = [{"direction": "inbound", "text": "I'm looking at the Air Max"}, {"direction": "outbound", "text": "Great!"}]
        found = await ai_tools.catalog_sources(db, B, "do you have it in 42?", earlier, message_id="m2")
        assert found and "Black / 42: Available." in found[0]["text"]
        assert await ai_tools.catalog_sources(db, B, "what time do you close?", [], message_id="m3") == []
    run(go())


def test_a_booking_that_needs_confirmation_is_never_described_as_booked():
    async def go():
        db = await fresh()
        await items.create_item(db, B, {"name": "Bespoke table", "description": "Custom table", "item_type": "made_to_order",
                                        "category": "furniture", "pricing_model": "quote", "requires_confirmation": True,
                                        "availability_config": {"lead_time_days": 21}}, "u1")
        text = (await ai_tools.catalog_sources(db, B, "can I order a bespoke table?", message_id="m4"))[0]["text"]
        assert "price on request" in text and "team must confirm" in text and "lead time: 21 days" in text.lower()
    run(go())


def test_a_catalog_price_answer_can_go_out_without_review_but_playbook_prices_cannot():
    catalog = [{"id": "catalog:1", "label": "Catalog · Air Max", "approved": True}]
    ok, reason = decide(cls=_cls(exclusions=["stock_or_price"]), draft=_draft(sources=catalog))
    assert ok, reason
    playbook = [{"id": "playbook:price_range", "label": "Playbook", "approved": False}]
    assert not decide(cls=_cls(exclusions=["stock_or_price"]), draft=_draft(sources=playbook, cites_price_or_stock=True))[0]


def test_the_agent_cannot_invent_a_catalog_citation():
    async def go():
        calls = {}

        async def llm(system, user):
            calls["system"] = system
            return {"reply": "Yes, 99 in stock!", "cited": ["S77"], "confidence": 99, "unanswered": []}
        sources = [{"id": "catalog:1", "label": "Catalog · Air Max", "text": "CATALOG ITEM: Air Max", "approved": True,
                    "price_or_stock": False}]
        out = await agent.draft_reply("how many air max?", [], sources, "dm", "instagram", "Shop", llm)
        assert out["confidence"] == 0, "a source that doesn't exist means the facts came from somewhere else"
        assert "catalog:" in calls["system"] and "never say anything is booked" in calls["system"]
    run(go())


def test_draft_orders_reserve_all_lines_or_none():
    async def go():
        db = await fresh()
        a = await shoe(db, initial_quantity=1)
        b = await shoe(db, name="Hoodie", sku="HD", initial_quantity=0)
        draft = await ai_tools.create_draft_order(db, B, [{"item_id": a["_id"], "quantity": 1}, {"item_id": b["_id"], "quantity": 1}])
        try:
            await ai_tools.reserve_draft_order(db, B, draft["_id"], True)
            raise AssertionError("expected a conflict")
        except CatalogError as e:
            assert e.status == 409 and "Hoodie" in e.detail
        assert (await reservations.list_reservations(db, B, status="pending"))["total"] == 0
        assert (await ai_tools.get_draft_order(db, B, draft["_id"]))["status"] == "draft"
        try:
            await ai_tools.reserve_draft_order(db, B, draft["_id"], "yes")
            raise AssertionError("expected a refusal")
        except CatalogError as e:
            assert e.reason == "not_confirmed"
    run(go())


def test_stock_visibility_controls_what_a_customer_can_be_told():
    async def go():
        db = await fresh()
        item = await with_sizes(db)
        ask = "Do you have the air max in black size 42?"
        exact = await items.update_item(db, B, item["_id"], {"stock_visibility": "exact"}, "u1")
        assert "Black / 42: 3 available" in (await ai_tools.catalog_sources(db, B, ask))[0]["text"]
        await items.update_item(db, B, item["_id"], {"stock_visibility": "low_only", "low_stock_threshold": 3}, "u1")
        assert "Black / 42: limited stock" in (await ai_tools.catalog_sources(db, B, ask))[0]["text"]
        await items.update_item(db, B, item["_id"], {"stock_visibility": "never"}, "u1")
        text = (await ai_tools.catalog_sources(db, B, ask))[0]["text"]
        assert "Black / 42: in stock" in text and "limited" not in text and "3" not in text.split("By option")[1].split("Black / 43")[0]
        res = await ai_tools.ai_check(db, B, item["_id"], quantity=2)
        assert res["available_quantity"] is None
        too_many = await ai_tools.ai_check(db, B, item["_id"], res_variant(await items.list_variants(db, B, item["_id"])), 9)
        assert "isn't enough" in too_many["message"] and not any(ch.isdigit() for ch in too_many["message"])
        assert exact["stock_visibility"] == "exact"
    run(go())


def res_variant(variants):
    return next(v["_id"] for v in variants if v["name"] == "Black / 42")

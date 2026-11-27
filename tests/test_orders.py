"""Orders: server-side pricing, all-or-none reservation, separate payment/fulfilment, and tenant isolation."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.agents.catalog import indexes as catalog_indexes
from app.agents.catalog.router import router as catalog_router
from app.agents.orders import indexes as order_indexes
from app.agents.orders.router import router
from app.dependencies import get_active_brand_context, get_db_dependency


@pytest.fixture()
def env():
    db = AsyncMongoMockClient()["t"]
    catalog_indexes.reset()
    order_indexes.reset()
    app = FastAPI()
    app.include_router(catalog_router)
    app.include_router(router)
    who = {"user_id": "u1", "brand_id": "b1", "agency_id": None}
    app.dependency_overrides[get_db_dependency] = lambda: db
    app.dependency_overrides[get_active_brand_context] = lambda: who
    with TestClient(app) as client:
        client.who, client.db = who, db
        yield client
    catalog_indexes.reset()
    order_indexes.reset()


def item(client, **over):
    body = {"name": "Air Max", "description": "Shoe", "item_type": "physical", "category": "footwear", "price": 45000,
            "initial_quantity": 2, "sku": "AM-1"}
    body.update(over)
    res = client.post("/catalog/items", json=body)
    assert res.status_code == 201, res.text
    return res.json()


def draft(client, item_id, qty=1, **over):
    res = client.post("/orders", json={"source": "onebox", "source_ref": "conv_1", "customer": {"name": "Ada", "phone": "+234801"},
                                       "items": [{"catalog_item_id": item_id, "quantity": qty, "unit_price": 1}], **over})
    assert res.status_code == 201, res.text
    return res.json()


def stock_of(client, item_id):
    return client.get(f"/catalog/items/{item_id}").json()["stock"]


def test_the_server_sets_the_price_and_snapshots_the_line(env):
    shoe = item(env)
    order = draft(env, shoe["id"], 2, delivery={"method": "delivery", "address": "Lekki", "fee": 1500})
    assert order["lines"][0]["unit_amount"] == 4_500_000, "a client-supplied price is ignored"
    assert order["subtotal"] == 9_000_000 and order["fees_total"] == 150_000 and order["total"] == 9_150_000
    assert order["status"] == "draft" and order["payment_status"] == "unpaid" and order["order_number"] == "ORD-00001"
    assert order["source_ref"] == "conv_1" and stock_of(env, shoe["id"])["available"] == 2, "a draft reserves nothing"
    env.patch(f"/catalog/items/{shoe['id']}", json={"name": "Renamed", "price": 99999})
    assert env.get(f"/orders/{order['id']}").json()["lines"][0]["snapshot"]["name"] == "Air Max"


def test_idempotency_key_stops_a_duplicate_order(env):
    shoe = item(env)
    body = {"items": [{"catalog_item_id": shoe["id"], "quantity": 1}]}
    a = env.post("/orders", json=body, headers={"Idempotency-Key": "k1"}).json()
    b = env.post("/orders", json=body, headers={"Idempotency-Key": "k1"}).json()
    assert a["id"] == b["id"] and env.get("/orders").json()["total"] == 1


def test_confirm_needs_acceptance_and_reserves_all_or_nothing(env):
    plenty, scarce = item(env, sku="A"), item(env, name="Dunk", sku="D", initial_quantity=1)
    order = env.post("/orders", json={"items": [{"catalog_item_id": plenty["id"], "quantity": 1},
                                               {"catalog_item_id": scarce["id"], "quantity": 2}]}).json()
    assert env.post(f"/orders/{order['id']}/confirm", json={}).status_code == 400
    res = env.post(f"/orders/{order['id']}/confirm", json={"customer_accepted": True})
    assert res.status_code == 409 and "Dunk" in res.json()["detail"]
    assert stock_of(env, plenty["id"])["available"] == 2, "the first line's hold was given back"
    assert env.get(f"/orders/{order['id']}").json()["status"] == "draft"


def test_payment_request_never_marks_paid_and_paying_commits_stock(env):
    shoe = item(env)
    order = draft(env, shoe["id"])
    oid = order["id"]
    assert env.post(f"/orders/{oid}/payment-requests", json={}).status_code == 409
    confirmed = env.post(f"/orders/{oid}/confirm", json={"customer_accepted": True}).json()
    assert confirmed["status"] == "confirmed" and stock_of(env, shoe["id"])["available"] == 1
    req = env.post(f"/orders/{oid}/payment-requests", json={"amount": 2_000_000}).json()
    assert req["status"] == "pending" and req["reference"].startswith("ORD-00001-P1-")
    got = env.get(f"/orders/{oid}").json()
    assert got["payment_status"] == "pending" and got["paid_amount"] == 0
    part = env.post(f"/orders/{oid}/payments", json={"amount": 2_000_000, "reference": req["reference"], "method": "transfer"}).json()
    assert part["payment_status"] == "partially_paid" and part["due_amount"] == 2_500_000
    assert env.post(f"/orders/{oid}/payments", json={"amount": 9_000_000}).status_code == 409, "overpayment is a mismatch"
    full = env.post(f"/orders/{oid}/payments", json={"amount": 2_500_000}).json()
    assert full["payment_status"] == "paid" and full["status"] == "confirmed" and full["due_amount"] == 0
    assert {p["status"] for p in env.get(f"/orders/{oid}").json()["payments"]} == {"paid"}
    row = stock_of(env, shoe["id"])
    assert row["on_hand"] == 1 and row["reserved"] == 0, "payment turned the hold into a sale"


def test_a_provider_transaction_counts_once(env):
    from app.agents.orders import service
    import asyncio
    shoe = item(env)
    oid = draft(env, shoe["id"])["id"]
    env.post(f"/orders/{oid}/confirm", json={"customer_accepted": True})

    async def twice():
        for _ in range(2):
            await service.record_payment(env.db, "b1", oid, 1_000_000, "provider", "psp", transaction_id="txn_1")
    asyncio.run(twice())
    assert env.get(f"/orders/{oid}").json()["paid_amount"] == 1_000_000
    with pytest.raises(Exception):
        asyncio.run(service.record_payment(env.db, "b1", oid, 5, "chat_screenshot", "ai"))


def test_fulfilment_is_separate_from_payment(env):
    shoe = item(env)
    order = draft(env, shoe["id"])
    oid, line = order["id"], order["lines"][0]["id"]
    env.post(f"/orders/{oid}/confirm", json={"customer_accepted": True})
    assert env.post(f"/orders/{oid}/fulfilments", json={"line_id": line, "status": "fulfilled"}).status_code == 409
    env.post(f"/orders/{oid}/fulfilments", json={"line_id": line, "status": "processing"})
    mid = env.get(f"/orders/{oid}").json()
    assert mid["status"] == "in_progress" and mid["payment_status"] == "unpaid"
    env.post(f"/orders/{oid}/payments", json={"amount": 4_500_000})
    done = env.post(f"/orders/{oid}/fulfilments", json={"line_id": line, "status": "fulfilled"}).json()
    assert done["status"] == "completed" and done["fulfilment_status"] == "fulfilled" and done["payment_status"] == "paid"


def test_cancel_releases_stock_and_flags_a_refund_without_completing_it(env):
    shoe = item(env)
    oid = draft(env, shoe["id"])["id"]
    env.post(f"/orders/{oid}/confirm", json={"customer_accepted": True})
    env.post(f"/orders/{oid}/payments", json={"amount": 4_500_000})
    out = env.post(f"/orders/{oid}/cancel", json={"reason": "changed mind"}).json()
    assert out["status"] == "cancelled" and out["refund_due"] is True and out["payment_status"] == "paid"
    assert stock_of(env, shoe["id"])["available"] == 2
    refund = env.post(f"/orders/{oid}/refund-requests", json={"amount": 4_500_000, "reason": "cancelled"})
    assert refund.status_code == 201 and refund.json()["status"] == "requested"
    assert env.post(f"/orders/{oid}/refund-requests", json={"amount": 1}).status_code == 409


def test_a_changed_price_blocks_confirmation(env):
    shoe = item(env)
    oid = draft(env, shoe["id"])["id"]
    env.patch(f"/catalog/items/{shoe['id']}", json={"price": 50000})
    res = env.post(f"/orders/{oid}/confirm", json={"customer_accepted": True})
    assert res.status_code == 409 and "price changed" in res.json()["detail"]


def test_quote_items_need_an_approved_price_before_confirming(env):
    quote = item(env, name="Bespoke", sku="B", pricing_model="quote", price=None, item_type="made_to_order", initial_quantity=None)
    order = env.post("/orders", json={"items": [{"catalog_item_id": quote["id"], "quantity": 1}]}).json()
    oid, line = order["id"], order["lines"][0]["id"]
    assert env.post(f"/orders/{oid}/confirm", json={"customer_accepted": True}).status_code == 409
    assert env.post(f"/orders/{oid}/quote", json={}).status_code == 409
    sent = env.post(f"/orders/{oid}/quote", json={"line_id": line, "unit_price": 120000}).json()
    assert sent["status"] == "awaiting_customer" and sent["total"] == 12_000_000


def test_orders_are_isolated_and_the_public_link_is_minimal(env):
    shoe = item(env)
    order = draft(env, shoe["id"], internal_notes="VIP, watch for fraud")
    pub = env.get(f"/orders/public/{order['public_token']}").json()
    assert "internal_notes" not in pub and "business_id" not in pub and pub["customer_name"] == "Ada"
    assert env.get("/orders/public/nope").status_code == 404
    env.who["brand_id"] = "b2"
    assert env.get(f"/orders/{order['id']}").status_code == 404
    assert env.get("/orders").json()["total"] == 0 and env.post(f"/orders/{order['id']}/cancel").status_code == 404


def test_staff_cannot_record_payments_or_export(env):
    shoe = item(env)
    oid = draft(env, shoe["id"])["id"]
    env.post(f"/orders/{oid}/confirm", json={"customer_accepted": True})
    env.who["agency_id"] = "a1"
    from unittest.mock import AsyncMock, patch
    with patch("app.services.AgencyService.AgencyService.is_agency_admin", new=AsyncMock(return_value=False)):
        assert env.post(f"/orders/{oid}/payments", json={"amount": 100}).status_code == 403
        assert env.get("/orders/export").status_code == 403


def test_list_filters_search_and_csv(env):
    shoe = item(env, initial_quantity=5)
    a, b = draft(env, shoe["id"])["id"], draft(env, shoe["id"])["id"]
    env.post(f"/orders/{a}/confirm", json={"customer_accepted": True})
    assert env.get("/orders?status=confirmed").json()["total"] == 1
    assert env.get("/orders?q=air").json()["total"] == 2 and env.get("/orders?q=zzz").json()["total"] == 0
    csv = env.get("/orders/export")
    assert csv.status_code == 200 and "ORD-00001" in csv.text and csv.text.splitlines()[0].startswith("order_number")
    assert b


def test_adjustments_need_a_reason_leave_a_revision_and_never_undercut_payments(env):
    shoe = item(env)
    oid = draft(env, shoe["id"])["id"]
    env.post(f"/orders/{oid}/confirm", json={"customer_accepted": True})
    assert env.post(f"/orders/{oid}/adjustments", json={"kind": "discount", "amount": 500_000}).status_code == 400
    out = env.post(f"/orders/{oid}/adjustments", json={"kind": "discount", "amount": 500_000, "reason": "loyal customer"}).json()
    assert out["total"] == 4_000_000 and out["due_amount"] == 4_000_000
    rev = out["revisions"][0]
    assert rev["before"]["total"] == 4_500_000 and rev["after"]["total"] == 4_000_000 and rev["reason"] == "loyal customer"
    env.post(f"/orders/{oid}/payments", json={"amount": 3_900_000})
    assert env.post(f"/orders/{oid}/adjustments", json={"kind": "discount", "amount": 200_000, "reason": "x"}).status_code == 409
    paid = env.post(f"/orders/{oid}/adjustments", json={"kind": "discount", "amount": 100_000, "reason": "sorry for delay"}).json()
    assert paid["payment_status"] == "paid"
    kinds = [e["type"] for e in env.get(f"/orders/{oid}/events").json()["events"]]
    assert kinds.count("adjusted") == 2


def _paid_order(env, qty=1, stock_qty=3):
    shoe = item(env, initial_quantity=stock_qty)
    order = draft(env, shoe["id"], qty)
    oid, line = order["id"], order["lines"][0]["id"]
    env.post(f"/orders/{oid}/confirm", json={"customer_accepted": True})
    env.post(f"/orders/{oid}/payments", json={"amount": 4_500_000 * qty})
    return shoe, oid, line


def _hook(env, event, secret="s3cret", sign=True):
    import hashlib
    import hmac
    import json
    raw = json.dumps(event).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest() if sign else "sha256=bad"
    return env.post("/orders/webhooks/payments", content=raw, headers={"x-uri-signature": sig, "content-type": "application/json"})


def test_the_payment_webhook_needs_a_valid_signature_and_counts_a_transaction_once(env, monkeypatch):
    shoe = item(env)
    oid = draft(env, shoe["id"])["id"]
    env.post(f"/orders/{oid}/confirm", json={"customer_accepted": True})
    ev = {"type": "payment.succeeded", "business_id": "b1", "order_id": oid, "amount": 4_500_000, "transaction_id": "t1", "provider": "psp"}
    assert _hook(env, ev).status_code == 503, "not configured: refuse rather than trust"
    monkeypatch.setenv("ORDERS_PAYMENT_WEBHOOK_SECRET", "s3cret")
    assert _hook(env, ev, sign=False).status_code == 401
    assert env.get(f"/orders/{oid}").json()["paid_amount"] == 0
    assert _hook(env, ev).json()["payment_status"] == "paid"
    assert _hook(env, ev).status_code == 200
    got = env.get(f"/orders/{oid}").json()
    assert got["paid_amount"] == 4_500_000 and len(got["payments"]) == 1 and got["payments"][0]["source"] == "provider"
    assert _hook(env, {"type": "nope", "business_id": "b1", "order_id": oid}).status_code == 400


def test_a_refund_completes_only_on_confirmation_and_moves_the_payment_status(env, monkeypatch):
    monkeypatch.setenv("ORDERS_PAYMENT_WEBHOOK_SECRET", "s3cret")
    shoe, oid, line = _paid_order(env)
    env.post(f"/orders/{oid}/cancel", json={"reason": "x"})
    refund = env.post(f"/orders/{oid}/refund-requests", json={"amount": 1_000_000, "reason": "part"}).json()
    assert env.get(f"/orders/{oid}").json()["payment_status"] == "paid", "a request changes nothing"
    ev = {"type": "refund.completed", "business_id": "b1", "order_id": oid, "refund_id": refund["id"], "provider_reference": "rf1"}
    _hook(env, ev)
    _hook(env, ev)
    got = env.get(f"/orders/{oid}").json()
    assert got["payment_status"] == "partially_refunded" and got["refunded_amount"] == 1_000_000
    second = env.post(f"/orders/{oid}/refund-requests", json={"amount": 3_500_000}).json()
    out = env.post(f"/orders/{oid}/refunds/{second['id']}/complete", json={"reference": "cash"}).json()
    assert out["payment_status"] == "refunded" and out["refund_due"] is False


def test_returns_go_back_on_sale_only_when_inspected_resellable(env):
    shoe, oid, line = _paid_order(env, qty=2, stock_qty=3)
    assert env.post(f"/orders/{oid}/returns", json={"line_id": line, "quantity": 1}).status_code == 409, "not delivered yet"
    env.post(f"/orders/{oid}/fulfilments", json={"line_id": line, "status": "fulfilled"})
    base = stock_of(env, shoe["id"])["available"]
    assert env.post(f"/orders/{oid}/returns", json={"line_id": line, "quantity": 5}).status_code == 409
    r1 = env.post(f"/orders/{oid}/returns", json={"line_id": line, "quantity": 1, "reason": "too small"}).json()
    assert env.post(f"/orders/returns/{r1['id']}/receive").status_code == 409, "must be approved first"
    env.post(f"/orders/returns/{r1['id']}/approve")
    env.post(f"/orders/returns/{r1['id']}/receive")
    assert stock_of(env, shoe["id"])["available"] == base, "received but not inspected: not resellable yet"
    assert env.post(f"/orders/returns/{r1['id']}/resolve", json={"disposition": "bogus"}).status_code == 400
    env.post(f"/orders/returns/{r1['id']}/resolve", json={"disposition": "restock"})
    assert stock_of(env, shoe["id"])["available"] == base + 1
    r2 = env.post(f"/orders/{oid}/returns", json={"line_id": line, "quantity": 1}).json()
    for step in ("approve", "receive"):
        env.post(f"/orders/returns/{r2['id']}/{step}")
    env.post(f"/orders/returns/{r2['id']}/resolve", json={"disposition": "damaged"})
    assert stock_of(env, shoe["id"])["available"] == base + 1, "damaged goods never return to stock"
    assert env.get(f"/orders/{oid}").json()["lines"][0]["fulfilment_status"] == "returned"
    rejected = env.post(f"/orders/{oid}/returns", json={"line_id": line, "quantity": 1})
    assert rejected.status_code == 409


def test_customers_are_messaged_once_per_event_and_unreachable_ones_are_logged(env):
    shoe, oid, line = _paid_order(env)
    notes = env.get(f"/orders/{oid}/notifications").json()["notifications"]
    assert [n["kind"] for n in notes] == ["confirmed", "payment_status_changed"]
    assert all(n["result"] == "no_channel" for n in notes) and "received your payment" in notes[1]["text"]
    env.post(f"/orders/{oid}/fulfilments", json={"line_id": line, "status": "processing"})
    env.post(f"/orders/{oid}/fulfilments", json={"line_id": line, "status": "processing"})
    kinds = [n["text"] for n in env.get(f"/orders/{oid}/notifications").json()["notifications"]]
    assert sum("being prepared" in t for t in kinds) == 1


def test_a_chat_customer_gets_the_message_in_their_conversation_and_a_closed_window_is_logged(env, monkeypatch):
    import asyncio
    from datetime import timedelta
    from bson import ObjectId
    from app.agents.inbox.entities import CONVERSATIONS, now
    from app.agents.orders import service
    sent = []

    async def transport(conv, account, text):
        sent.append(text)
        return "mid1"
    monkeypatch.setattr(service, "_transport", transport)

    def convo(hours):
        doc = {"_id": ObjectId(), "workspace_id": "b1", "kind": "dm", "platform": "instagram", "channel_account_id": "a",
               "external_thread_id": "U1", "status": "open", "reply_window_expires_at": now() + timedelta(hours=hours)}
        asyncio.run(env.db[CONVERSATIONS].insert_one(doc))
        return str(doc["_id"])

    shoe = item(env, initial_quantity=5)
    for hours, expect_sent in ((6, True), (-1, False)):
        conv = convo(hours)
        order = env.post("/orders", json={"source": "onebox", "source_ref": conv, "items": [{"catalog_item_id": shoe["id"], "quantity": 1}]}).json()
        env.post(f"/orders/{order['id']}/confirm", json={"customer_accepted": True})
        env.post(f"/orders/{order['id']}/payments", json={"amount": 4_500_000})
        note = env.get(f"/orders/{order['id']}/notifications").json()["notifications"][-1]
        assert note["channel"] == "instagram"
        assert (note["result"] == "sent") is expect_sent, note
    assert len(sent) == 2 and "is confirmed" in sent[0] and "received your payment" in sent[1]
    from app.agents.inbox.agent import _activity
    from app.agents.inbox.entities import MESSAGES
    history = asyncio.run(env.db[MESSAGES].find({}).to_list(None))
    assert history and _activity(history, now())[1] is False, "an order update is not a team member taking over, so Jane keeps replying"


def test_two_businesses_with_the_same_order_number_can_both_ask_for_payment(env):
    refs = []
    for brand in ("b1", "b2"):
        env.who["brand_id"] = brand
        shoe = item(env)
        oid = draft(env, shoe["id"])["id"]
        env.post(f"/orders/{oid}/confirm", json={"customer_accepted": True})
        res = env.post(f"/orders/{oid}/payment-requests", json={})
        assert res.status_code == 201, res.text
        refs.append(res.json()["reference"])
    assert refs[0] != refs[1] and all(r.startswith("ORD-00001-P1-") for r in refs)

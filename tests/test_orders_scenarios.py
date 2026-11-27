"""Order lifecycle scenarios end to end: every way an order can go right, get cancelled, or go wrong."""
import asyncio
from datetime import timedelta

import pytest

from app.agents.orders import service
from app.agents.orders.models import ORDERS
from tests.test_orders import _hook, _paid_order, draft, env, item, stock_of  # noqa: F401


def run(c):
    return asyncio.run(c)


def confirmed(env, shoe, qty=1, **over):
    order = draft(env, shoe["id"], qty, **over)
    res = env.post(f"/orders/{order['id']}/confirm", json={"customer_accepted": True})
    assert res.status_code == 200, res.text
    return res.json()


def pay(env, oid, amount, **body):
    return env.post(f"/orders/{oid}/payments", json={"amount": amount, **body})


def get(env, oid):
    return env.get(f"/orders/{oid}").json()


# ── cancelling ───────────────────────────────────────────────────────────────

def test_cancelling_an_unpaid_order_returns_the_stock_and_owes_no_refund(env):
    shoe = item(env, initial_quantity=2)
    order = confirmed(env, shoe, 2)
    assert stock_of(env, shoe["id"])["available"] == 0
    out = env.post(f"/orders/{order['id']}/cancel", json={"reason": "changed mind"}).json()
    assert out["status"] == "cancelled" and out["refund_due"] is False
    assert stock_of(env, shoe["id"])["available"] == 2
    assert env.post(f"/orders/{order['id']}/cancel", json={}).json()["status"] == "cancelled", "cancelling twice is harmless"


def test_cancelling_a_draft_never_touched_stock(env):
    shoe = item(env, initial_quantity=2)
    oid = draft(env, shoe["id"])["id"]
    assert env.post(f"/orders/{oid}/cancel", json={}).json()["status"] == "cancelled"
    assert stock_of(env, shoe["id"])["available"] == 2


def test_cancelling_a_part_paid_order_returns_stock_and_flags_the_refund(env):
    shoe = item(env, initial_quantity=2)
    order = confirmed(env, shoe)
    pay(env, order["id"], 1_000_000)
    out = env.post(f"/orders/{order['id']}/cancel", json={"reason": "x"}).json()
    assert out["status"] == "cancelled" and out["refund_due"] is True and out["paid_amount"] == 1_000_000
    assert stock_of(env, shoe["id"])["available"] == 2


def test_cancelling_a_paid_unfulfilled_order_puts_the_item_back_on_sale(env):
    shoe, oid, line = _paid_order(env, qty=1, stock_qty=2)
    assert stock_of(env, shoe["id"])["available"] == 1
    out = env.post(f"/orders/{oid}/cancel", json={"reason": "x"}).json()
    assert out["status"] == "cancelled" and out["refund_due"] is True
    assert stock_of(env, shoe["id"])["available"] == 2


def test_a_completed_order_cannot_be_cancelled_only_refunded(env):
    shoe, oid, line = _paid_order(env)
    env.post(f"/orders/{oid}/fulfilments", json={"line_id": line, "status": "fulfilled"})
    assert get(env, oid)["status"] == "completed"
    assert env.post(f"/orders/{oid}/cancel", json={}).status_code == 409


def test_a_cancelled_order_cannot_be_confirmed_paid_fulfilled_or_adjusted(env):
    shoe = item(env, initial_quantity=3)
    order = confirmed(env, shoe)
    oid, line = order["id"], order["lines"][0]["id"]
    env.post(f"/orders/{oid}/cancel", json={})
    assert env.post(f"/orders/{oid}/confirm", json={"customer_accepted": True}).status_code == 409
    assert env.post(f"/orders/{oid}/quote", json={}).status_code == 409
    assert pay(env, oid, 100).status_code == 409
    assert env.post(f"/orders/{oid}/fulfilments", json={"line_id": line, "status": "processing"}).status_code == 409
    assert env.post(f"/orders/{oid}/adjustments", json={"kind": "discount", "amount": 100, "reason": "x"}).status_code == 409
    assert env.post(f"/orders/{oid}/payment-requests", json={}).status_code == 409
    assert stock_of(env, shoe["id"])["available"] == 3, "none of that touched stock"


# ── payments ─────────────────────────────────────────────────────────────────

def test_part_payments_add_up_and_the_last_one_commits_stock(env):
    shoe = item(env, initial_quantity=2)
    order = confirmed(env, shoe)
    oid = order["id"]
    assert pay(env, oid, 1_000_000).json()["payment_status"] == "partially_paid"
    assert pay(env, oid, 1_500_000).json()["payment_status"] == "partially_paid"
    done = pay(env, oid, 2_000_000).json()
    assert done["payment_status"] == "paid" and done["paid_amount"] == 4_500_000 and done["due_amount"] == 0
    row = env.db.get_collection("catalog_stock")
    stock = run(row.find_one({"item_id": shoe["id"]}))
    assert stock["on_hand"] == 1 and stock["reserved"] == 0


def test_overpaying_and_zero_or_negative_payments_are_refused(env):
    shoe = item(env)
    oid = confirmed(env, shoe)["id"]
    assert pay(env, oid, 4_500_001).status_code == 409
    for bad in (0, -5, "abc", None, 1.5):
        assert pay(env, oid, bad).status_code in (400, 422), bad
    assert get(env, oid)["paid_amount"] == 0


def test_an_unpaid_order_cannot_be_paid_before_it_is_confirmed(env):
    shoe = item(env)
    oid = draft(env, shoe["id"])["id"]
    assert pay(env, oid, 100).status_code == 409


def test_a_payment_that_arrives_after_the_order_was_cancelled_is_recorded_and_flagged_for_refund(env, monkeypatch):
    monkeypatch.setenv("ORDERS_PAYMENT_WEBHOOK_SECRET", "s3cret")
    shoe = item(env, initial_quantity=2)
    oid = confirmed(env, shoe)["id"]
    env.post(f"/orders/{oid}/cancel", json={})
    ev = {"type": "payment.succeeded", "business_id": "b1", "order_id": oid, "amount": 4_500_000, "transaction_id": "late1", "provider": "psp"}
    assert _hook(env, ev).status_code == 200, "the customer's money arrived; it must be recorded, not dropped"
    got = get(env, oid)
    assert got["status"] == "cancelled" and got["paid_amount"] == 4_500_000 and got["refund_due"] is True
    assert stock_of(env, shoe["id"])["available"] == 2, "a late payment never re-takes stock"
    assert _hook(env, ev).status_code == 200 and get(env, oid)["paid_amount"] == 4_500_000, "and counts once"


def test_a_late_payment_by_hand_on_a_cancelled_order_is_refused(env):
    shoe = item(env)
    oid = confirmed(env, shoe)["id"]
    env.post(f"/orders/{oid}/cancel", json={})
    assert pay(env, oid, 100).status_code == 409


def test_a_failed_payment_leaves_the_order_unpaid_and_payable(env):
    shoe = item(env)
    oid = confirmed(env, shoe)["id"]
    ref = env.post(f"/orders/{oid}/payment-requests", json={}).json()["reference"]
    assert get(env, oid)["payment_status"] == "pending"
    run(service.mark_payment_failed(env.db, "b1", oid, ref, "psp"))
    assert get(env, oid)["payment_status"] == "unpaid"
    assert pay(env, oid, 4_500_000).json()["payment_status"] == "paid"


def test_a_payment_reference_can_be_asked_for_the_balance_only(env):
    shoe = item(env)
    oid = confirmed(env, shoe)["id"]
    assert env.post(f"/orders/{oid}/payment-requests", json={"amount": 9_999_999}).status_code == 409
    pay(env, oid, 4_500_000)
    assert env.post(f"/orders/{oid}/payment-requests", json={}).status_code == 409, "nothing is owed"


# ── stock ────────────────────────────────────────────────────────────────────

def test_two_customers_cannot_both_get_the_last_unit(env):
    shoe = item(env, initial_quantity=1)
    first, second = draft(env, shoe["id"])["id"], draft(env, shoe["id"])["id"]
    assert env.post(f"/orders/{first}/confirm", json={"customer_accepted": True}).status_code == 200
    lost = env.post(f"/orders/{second}/confirm", json={"customer_accepted": True})
    assert lost.status_code == 409 and get(env, second)["status"] == "draft"
    assert stock_of(env, shoe["id"])["available"] == 0
    env.post(f"/orders/{first}/cancel", json={})
    assert env.post(f"/orders/{second}/confirm", json={"customer_accepted": True}).status_code == 200, "freed stock goes to the next"


def test_confirming_twice_holds_stock_once(env):
    shoe = item(env, initial_quantity=3)
    oid = draft(env, shoe["id"], 2)["id"]
    for _ in range(3):
        assert env.post(f"/orders/{oid}/confirm", json={"customer_accepted": True}).status_code == 200
    assert stock_of(env, shoe["id"])["available"] == 1


def test_a_multi_line_order_that_cannot_be_filled_holds_nothing(env):
    a, b = item(env, initial_quantity=2, sku="A1", name="A"), item(env, initial_quantity=1, sku="B1", name="B")
    res = env.post("/orders", json={"source": "dashboard", "items": [{"catalog_item_id": a["id"], "quantity": 2},
                                                                    {"catalog_item_id": b["id"], "quantity": 5}]})
    oid = res.json()["id"]
    assert env.post(f"/orders/{oid}/confirm", json={"customer_accepted": True}).status_code == 409
    assert stock_of(env, a["id"])["available"] == 2 and stock_of(env, b["id"])["available"] == 1


def test_bad_order_input_is_refused_cleanly(env):
    shoe = item(env)
    post = lambda body: env.post("/orders", json=body)
    assert post({"source": "dashboard", "items": []}).status_code in (400, 422)
    assert post({"source": "dashboard", "items": [{"catalog_item_id": shoe["id"], "quantity": 0}]}).status_code in (400, 422)
    assert post({"source": "dashboard", "items": [{"catalog_item_id": shoe["id"], "quantity": -1}]}).status_code in (400, 422)
    assert post({"source": "dashboard", "items": [{"catalog_item_id": "nope", "quantity": 1}]}).status_code in (400, 404)
    assert post({"source": "dashboard", "items": [{"catalog_item_id": shoe["id"], "quantity": 1}] * 25}).status_code in (400, 422)
    assert env.get("/orders/does-not-exist").status_code == 404


def test_an_item_that_goes_inactive_before_confirmation_blocks_the_order(env):
    shoe = item(env, initial_quantity=3)
    oid = draft(env, shoe["id"])["id"]
    env.patch(f"/catalog/items/{shoe['id']}", json={"status": "archived"})
    assert env.post(f"/orders/{oid}/confirm", json={"customer_accepted": True}).status_code == 409
    assert get(env, oid)["status"] == "draft"


# ── expiry ───────────────────────────────────────────────────────────────────

def test_an_unpaid_order_past_its_window_expires_and_frees_stock_but_a_paid_one_never_does(env):
    shoe = item(env, initial_quantity=3)
    unpaid, paid = confirmed(env, shoe)["id"], confirmed(env, shoe)["id"]
    pay(env, paid, 4_500_000)
    later = service.utcnow() + timedelta(days=3)
    assert run(service.expire_due(env.db, "b1", later)) == 1
    assert get(env, unpaid)["status"] == "expired" and get(env, paid)["status"] == "confirmed"
    assert stock_of(env, shoe["id"])["available"] == 2


# ── fulfilment ───────────────────────────────────────────────────────────────

def test_a_multi_line_order_completes_only_when_every_line_is_delivered_and_paid(env):
    a, b = item(env, initial_quantity=2, sku="A1", name="A"), item(env, initial_quantity=2, sku="B1", name="B")
    order = env.post("/orders", json={"source": "dashboard", "items": [{"catalog_item_id": a["id"], "quantity": 1},
                                                                      {"catalog_item_id": b["id"], "quantity": 1}]}).json()
    oid, l1, l2 = order["id"], order["lines"][0]["id"], order["lines"][1]["id"]
    env.post(f"/orders/{oid}/confirm", json={"customer_accepted": True})
    pay(env, oid, order["total"])
    first = env.post(f"/orders/{oid}/fulfilments", json={"line_id": l1, "status": "fulfilled"}).json()
    assert first["status"] == "in_progress" and first["fulfilment_status"] == "partially_fulfilled"
    last = env.post(f"/orders/{oid}/fulfilments", json={"line_id": l2, "status": "fulfilled"}).json()
    assert last["status"] == "completed" and last["fulfilment_status"] == "fulfilled"


def test_a_failed_delivery_is_recorded_and_can_be_retried(env):
    shoe, oid, line = _paid_order(env)
    out = env.post(f"/orders/{oid}/fulfilments", json={"line_id": line, "status": "delivery_failed", "note": "no one home"}).json()
    assert out["status"] == "in_progress" and out["lines"][0]["fulfilment_status"] == "delivery_failed"
    done = env.post(f"/orders/{oid}/fulfilments", json={"line_id": line, "status": "fulfilled"}).json()
    assert done["status"] == "completed"


def test_a_service_with_no_stock_can_be_paid_and_fulfilled(env):
    plan = item(env, name="Starter Plan", item_type="service", initial_quantity=None, sku="SP", availability_type="manual")
    order = confirmed(env, plan)
    oid, line = order["id"], order["lines"][0]["id"]
    assert pay(env, oid, order["total"]).json()["payment_status"] == "paid"
    res = env.post(f"/orders/{oid}/fulfilments", json={"line_id": line, "status": "fulfilled"})
    assert res.status_code == 200 and res.json()["status"] == "completed", res.text


def test_unknown_line_and_bad_status_are_refused(env):
    shoe, oid, line = _paid_order(env)
    assert env.post(f"/orders/{oid}/fulfilments", json={"line_id": "zzz", "status": "processing"}).status_code == 404
    assert env.post(f"/orders/{oid}/fulfilments", json={"line_id": line, "status": "teleported"}).status_code == 400


# ── adjustments ──────────────────────────────────────────────────────────────

def test_a_discount_that_equals_what_was_paid_settles_the_order(env):
    shoe = item(env, initial_quantity=2)
    order = confirmed(env, shoe)
    oid = order["id"]
    pay(env, oid, 4_000_000)
    out = env.post(f"/orders/{oid}/adjustments", json={"kind": "discount", "amount": 500_000, "reason": "goodwill"}).json()
    assert out["total"] == 4_000_000 and out["payment_status"] == "paid" and out["due_amount"] == 0


def test_a_fee_added_after_full_payment_leaves_a_balance(env):
    shoe, oid, line = _paid_order(env)
    out = env.post(f"/orders/{oid}/adjustments", json={"kind": "fee", "amount": 200_000, "reason": "express"}).json()
    assert out["due_amount"] == 200_000 and out["revisions"][-1]["customer_acceptance_needed"] is True


# ── refunds ──────────────────────────────────────────────────────────────────

def test_refunds_cannot_exceed_what_was_paid_even_across_requests(env):
    shoe, oid, line = _paid_order(env)
    env.post(f"/orders/{oid}/cancel", json={})
    assert env.post(f"/orders/{oid}/refund-requests", json={"amount": 4_500_001, "reason": "x"}).status_code == 409
    assert env.post(f"/orders/{oid}/refund-requests", json={"amount": 3_000_000, "reason": "x"}).status_code == 201
    assert env.post(f"/orders/{oid}/refund-requests", json={"amount": 2_000_000, "reason": "x"}).status_code == 409
    assert env.post(f"/orders/{oid}/refund-requests", json={"amount": 0, "reason": "x"}).status_code in (400, 422)


def test_an_unpaid_order_has_nothing_to_refund(env):
    shoe = item(env)
    oid = confirmed(env, shoe)["id"]
    assert env.post(f"/orders/{oid}/refund-requests", json={"amount": 100, "reason": "x"}).status_code == 409


# ── returns ──────────────────────────────────────────────────────────────────

def test_a_rejected_return_changes_nothing_and_the_same_goods_can_be_returned_later(env):
    shoe, oid, line = _paid_order(env, qty=2, stock_qty=3)
    env.post(f"/orders/{oid}/fulfilments", json={"line_id": line, "status": "fulfilled"})
    base = stock_of(env, shoe["id"])["available"]
    r = env.post(f"/orders/{oid}/returns", json={"line_id": line, "quantity": 2}).json()
    assert env.post(f"/orders/returns/{r['id']}/reject", json={"note": "worn"}).status_code == 200
    assert stock_of(env, shoe["id"])["available"] == base
    assert env.post(f"/orders/{oid}/returns", json={"line_id": line, "quantity": 2}).status_code == 201


def test_the_same_goods_cannot_be_returned_twice(env):
    shoe, oid, line = _paid_order(env, qty=1, stock_qty=3)
    env.post(f"/orders/{oid}/fulfilments", json={"line_id": line, "status": "fulfilled"})
    first = env.post(f"/orders/{oid}/returns", json={"line_id": line, "quantity": 1}).json()
    assert env.post(f"/orders/{oid}/returns", json={"line_id": line, "quantity": 1}).status_code == 409, "already in a return"
    for step in ("approve", "receive"):
        env.post(f"/orders/returns/{first['id']}/{step}")
    env.post(f"/orders/returns/{first['id']}/resolve", json={"disposition": "restock"})
    assert env.post(f"/orders/{oid}/returns", json={"line_id": line, "quantity": 1}).status_code == 409


# ── dashboard views and permissions ──────────────────────────────────────────

def test_filters_show_each_order_where_the_team_expects_it(env):
    shoe = item(env, initial_quantity=9)
    d = draft(env, shoe["id"])["id"]
    c = confirmed(env, shoe)["id"]
    p = confirmed(env, shoe)
    pay(env, p["id"], p["total"])
    x = confirmed(env, shoe)["id"]
    env.post(f"/orders/{x}/cancel", json={})
    done = confirmed(env, shoe)
    pay(env, done["id"], done["total"])
    env.post(f"/orders/{done['id']}/fulfilments", json={"line_id": done["lines"][0]["id"], "status": "fulfilled"})

    def ids(status):
        return {o["id"] for o in env.get("/orders", params={"status": status}).json()["orders"]}
    assert ids("draft,awaiting_customer") == {d}
    assert ids("confirmed,in_progress") == {c, p["id"]}, "paid but not yet delivered is still open work"
    assert ids("completed") == {done["id"]}
    assert ids("cancelled,expired") == {x}
    assert env.get("/orders").json()["total"] == 5
    assert {o["id"] for o in env.get("/orders", params={"payment_status": "paid"}).json()["orders"]} == {p["id"], done["id"]}


def test_every_order_event_is_in_the_timeline_in_order(env):
    shoe, oid, line = _paid_order(env)
    env.post(f"/orders/{oid}/fulfilments", json={"line_id": line, "status": "fulfilled"})
    kinds = [e["type"] if "type" in e else e.get("kind") for e in env.get(f"/orders/{oid}/events").json()["events"]]
    assert kinds[0] == "created" and "confirmed" in kinds and "completed" in kinds and kinds.index("confirmed") < kinds.index("completed")


def test_staff_can_cancel_unpaid_but_not_paid_orders_or_take_manager_actions(env):
    from unittest.mock import AsyncMock, patch
    shoe, paid, line = _paid_order(env)
    unpaid = confirmed(env, shoe)["id"]
    env.who["agency_id"] = "a1"
    with patch("app.services.AgencyService.AgencyService.is_agency_admin", new=AsyncMock(return_value=False)):
        assert env.post(f"/orders/{paid}/cancel", json={}).status_code == 403, "a paid order needs a manager"
        assert env.post(f"/orders/{unpaid}/cancel", json={}).status_code == 200
        assert env.post(f"/orders/{paid}/refund-requests", json={"amount": 100, "reason": "x"}).status_code == 403
        assert env.post(f"/orders/{paid}/adjustments", json={"kind": "discount", "amount": 100, "reason": "x"}).status_code == 403
        assert env.patch(f"/orders/{paid}", json={"assignee": "someone"}).status_code == 403
        assert env.get(f"/orders/{paid}").status_code == 200, "staff can still read and work orders"
    assert get(env, paid)["status"] == "confirmed"

"""URI Onebox: billing by AI-handled conversation, the 150-message cap, the monthly
allowance, and the gate that enforces both atomically."""
import asyncio
from datetime import timedelta

import pytest
from bson import ObjectId

from app.agents.inbox import onebox
from app.agents.inbox.entities import CONVERSATIONS

mongomock_motor = pytest.importorskip("mongomock_motor")
WS = "ws1"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def make_db():
    db = mongomock_motor.AsyncMongoMockClient()["onebox"]

    async def setup():
        await db[onebox.EVENTS].create_index([("workspace_id", 1), ("event_id", 1)], unique=True)
        await db[onebox.LEDGER].create_index(
            [("workspace_id", 1), ("period_id", 1), ("billing_conversation_id", 1)], unique=True)
        await db[onebox.SUBSCRIPTIONS].create_index([("workspace_id", 1)], unique=True)
    run(setup())
    return db


def make_conv(db, platform="instagram", thread="U1"):
    doc = {"_id": ObjectId(), "workspace_id": WS, "platform": platform, "kind": "dm",
           "channel_account_id": "acct", "external_thread_id": thread}
    run(db[CONVERSATIONS].insert_one(dict(doc)))
    return doc


def subscribe(db, allowance=300, plan="starter"):
    plan_doc = {"plan_id": plan, "name": plan.title(), "conversations": allowance}
    return run(onebox.activate_plan(db, WS, plan_doc))


def inbound(db, conv, n, prefix="in"):
    for i in range(n):
        run(onebox.record_inbound(db, WS, conv, f"{prefix}{i}"))


def ai_reply(db, conv, mid):
    d = run(onebox.authorize(db, WS, conv, reserve=True))
    assert d.allowed, d.reason
    run(onebox.commit(db, WS, d.reservation, mid))
    return d


def bc(db, conv):
    return run(db[onebox.BILLING_CONVERSATIONS].find_one({"conversation_id": str(conv["_id"])}))


def sub(db):
    return run(db[onebox.SUBSCRIPTIONS].find_one({"workspace_id": WS}))


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(onebox, "ENFORCE", True)


def test_a_100_message_conversation_is_one_billable_conversation():
    db = make_db(); subscribe(db); conv = make_conv(db)
    for i in range(50):
        run(onebox.record_inbound(db, WS, conv, f"in{i}"))
        ai_reply(db, conv, f"out{i}")
    assert sub(db)["used"] == 1 and bc(db, conv)["message_count"] == 100 and bc(db, conv)["ai_replies"] == 50


def test_the_ai_stops_at_150_and_message_151_is_stored_without_a_reply():
    db = make_db(); subscribe(db); conv = make_conv(db)
    inbound(db, conv, 75)
    for i in range(75):
        ai_reply(db, conv, f"out{i}")
    assert bc(db, conv)["message_count"] == 150 and bc(db, conv)["status"] == "limit_reached"
    run(onebox.record_inbound(db, WS, conv, "in151"))
    assert bc(db, conv)["message_count"] == 151
    d = run(onebox.authorize(db, WS, conv, reserve=False))
    assert not d.allowed and d.code == "message_cap"
    assert sub(db)["used"] == 1                       # no further charge for the long chat


def test_a_human_can_continue_after_the_cap_and_it_stays_one_conversation():
    db = make_db(); subscribe(db); conv = make_conv(db)
    inbound(db, conv, 75)
    for i in range(75):
        ai_reply(db, conv, f"out{i}")
    run(onebox.record_outbound(db, WS, conv, "human1", "user_1"))
    assert bc(db, conv)["human_active"] and sub(db)["used"] == 1
    assert not run(onebox.authorize(db, WS, conv, reserve=False)).allowed      # AI stays off


def test_a_human_only_conversation_bills_nothing():
    db = make_db(); subscribe(db); conv = make_conv(db)
    inbound(db, conv, 5)
    for i in range(5):
        run(onebox.record_outbound(db, WS, conv, f"h{i}", "user_1"))
    assert sub(db)["used"] == 0


def test_ai_then_a_human_takeover_is_one_conversation():
    db = make_db(); subscribe(db); conv = make_conv(db)
    inbound(db, conv, 1); ai_reply(db, conv, "out0")
    run(onebox.record_outbound(db, WS, conv, "h1", "user_1"))
    assert sub(db)["used"] == 1


def test_a_redelivered_webhook_counts_once():
    db = make_db(); subscribe(db); conv = make_conv(db)
    for _ in range(3):
        run(onebox.record_inbound(db, WS, conv, "same-event"))
    assert bc(db, conv)["message_count"] == 1


def test_a_failed_reply_bills_nothing_and_returns_its_capacity():
    db = make_db(); subscribe(db); conv = make_conv(db)
    inbound(db, conv, 1)
    d = run(onebox.authorize(db, WS, conv, reserve=True))
    assert d.allowed
    run(onebox.release(db, WS, d.reservation))
    s = sub(db)
    assert s["used"] == 0 and s["consumed"] == 0 and s["reserved"] == 0
    assert bc(db, conv)["reserved"] == 0 and not bc(db, conv)["billed"]


def test_concurrent_jobs_cannot_pass_the_message_cap():
    db = make_db(); subscribe(db); conv = make_conv(db)
    inbound(db, conv, 148)

    async def race():
        return await asyncio.gather(*[onebox.authorize(db, WS, conv, reserve=True) for _ in range(6)])
    results = run(race())
    assert sum(d.allowed for d in results) == 2          # 148 + 2 = 150, never more
    assert bc(db, conv)["capacity_used"] == 150


def test_concurrent_jobs_cannot_oversubscribe_the_last_monthly_unit():
    db = make_db(); subscribe(db, allowance=1)
    convs = [make_conv(db, thread=f"U{i}") for i in range(5)]
    for c in convs:
        inbound(db, c, 1)

    async def race():
        return await asyncio.gather(*[onebox.authorize(db, WS, c, reserve=True) for c in convs])
    results = run(race())
    assert sum(d.allowed for d in results) == 1
    assert sub(db)["consumed"] == 1


def test_a_counted_conversation_keeps_replying_after_the_allowance_is_gone():
    db = make_db(); subscribe(db, allowance=1)
    a, b = make_conv(db, thread="A"), make_conv(db, thread="B")
    inbound(db, a, 1); ai_reply(db, a, "a0")
    inbound(db, b, 1)
    d = run(onebox.authorize(db, WS, b, reserve=True))
    assert not d.allowed and d.code == "quota"
    inbound(db, a, 1, prefix="again")
    assert ai_reply(db, a, "a1").allowed                   # already counted: no new unit


def test_usage_warns_at_80_percent_and_blocks_at_100():
    db = make_db(); subscribe(db, allowance=5)
    convs = [make_conv(db, thread=f"U{i}") for i in range(5)]
    for i, c in enumerate(convs):
        inbound(db, c, 1); ai_reply(db, c, f"o{i}")
        u = run(onebox.usage(db, WS))
        if i == 3:
            assert u["warning"] and u["percent_used"] == 80
    u = run(onebox.usage(db, WS))
    assert u["exhausted"] and u["remaining"] == 0
    assert run(onebox.threshold_crossed(db, WS)) == "exhausted"
    assert run(onebox.threshold_crossed(db, WS)) == "warning"
    assert run(onebox.threshold_crossed(db, WS)) is None     # each alert goes out once
    extra = make_conv(db, thread="X"); inbound(db, extra, 1)
    assert run(onebox.authorize(db, WS, extra, reserve=False)).code == "quota"


def test_a_conversation_inactive_for_a_day_starts_a_new_billing_conversation():
    db = make_db(); subscribe(db); conv = make_conv(db)
    inbound(db, conv, 1); ai_reply(db, conv, "o1")
    first = bc(db, conv)
    run(db[onebox.BILLING_CONVERSATIONS].update_one(
        {"_id": first["_id"]}, {"$set": {"last_activity_at": first["last_activity_at"] - timedelta(hours=25)}}))
    inbound(db, conv, 1, prefix="next"); ai_reply(db, conv, "o2")
    assert sub(db)["used"] == 2
    assert run(db[onebox.BILLING_CONVERSATIONS].count_documents({"conversation_id": str(conv["_id"])})) == 2


def test_the_window_and_cap_come_from_configuration_not_code():
    db = make_db(); subscribe(db); conv = make_conv(db)
    run(onebox.save_config(db, {"message_cap": 5, "inactivity_hours": 2}))
    inbound(db, conv, 2); ai_reply(db, conv, "o1")
    inbound(db, conv, 1, prefix="x")
    assert ai_reply(db, conv, "o2").allowed and not run(onebox.authorize(db, WS, conv, reserve=False)).allowed


def test_an_admin_can_change_a_plan_without_a_deploy():
    db = make_db()
    assert {p["plan_id"] for p in run(onebox.list_plans(db))} == {"starter", "growth", "business"}
    run(onebox.save_plan(db, {"plan_id": "growth", "name": "Growth", "price_ngn": 24900, "conversations": 1500}))
    growth = next(p for p in run(onebox.list_plans(db)) if p["plan_id"] == "growth")
    assert growth["price_ngn"] == 24900 and growth["conversations"] == 1500


def test_a_workspace_with_no_plan_starts_a_free_trial():
    db = make_db(); conv = make_conv(db); inbound(db, conv, 1)
    d = run(onebox.authorize(db, WS, conv, reserve=True))
    s = sub(db)
    assert d.allowed and s["status"] == "trial" and s["allowance"] == 100


def test_an_expired_plan_blocks_the_ai():
    db = make_db(); subscribe(db); conv = make_conv(db); inbound(db, conv, 1)
    run(db[onebox.SUBSCRIPTIONS].update_one({"workspace_id": WS}, {"$set": {
        "period_end": onebox._now() - timedelta(days=1)}}))
    d = run(onebox.authorize(db, WS, conv, reserve=False))
    assert not d.allowed and d.code == "no_subscription"


def test_paying_early_queues_the_next_period_and_renewal_resets_the_allowance():
    db = make_db(); subscribe(db, allowance=3)
    conv = make_conv(db); inbound(db, conv, 1); ai_reply(db, conv, "o1")
    run(onebox.activate_plan(db, WS, {"plan_id": "growth", "name": "Growth", "conversations": 1000}))
    s = sub(db)
    assert s["plan_id"] == "starter" and s["queued"]["plan_id"] == "growth" and s["used"] == 1
    run(db[onebox.SUBSCRIPTIONS].update_one({"workspace_id": WS}, {"$set": {
        "period_end": onebox._now() - timedelta(minutes=1)}}))
    s = run(onebox.get_subscription(db, WS))
    assert s["plan_id"] == "growth" and s["allowance"] == 1000 and s["used"] == 0 and s["queued"] is None


def test_the_same_conversation_is_never_ledgered_twice_in_a_period():
    db = make_db(); subscribe(db); conv = make_conv(db); inbound(db, conv, 1)
    d1 = run(onebox.authorize(db, WS, conv, reserve=True))
    d2 = run(onebox.authorize(db, WS, conv, reserve=True))      # a second job, same conversation
    run(onebox.commit(db, WS, d1.reservation, "m1"))
    run(onebox.commit(db, WS, d2.reservation, "m2"))
    s = sub(db)
    assert s["used"] == 1 and s["consumed"] == 1 and s["reserved"] == 0
    assert run(db[onebox.LEDGER].count_documents({})) == 1


def test_one_allowance_is_shared_across_channels():
    db = make_db(); subscribe(db, allowance=3)
    for i, platform in enumerate(("instagram", "facebook", "whatsapp")):
        c = make_conv(db, platform=platform, thread=f"T{i}"); inbound(db, c, 1); ai_reply(db, c, f"o{i}")
    assert sub(db)["used"] == 3
    extra = make_conv(db, platform="instagram", thread="T9"); inbound(db, extra, 1)
    assert not run(onebox.authorize(db, WS, extra, reserve=False)).allowed


def test_a_onebox_payment_activates_the_workspaces_plan_once(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from app.services.PaymentService import PaymentService
    db = make_db()
    txn = {"transaction_ref": "URI_OBX_1", "status": "pending", "purchase_type": "onebox",
           "workspace_id": WS, "onebox_plan_id": "growth", "user_id": "u1"}
    run(db["payment_transactions"].insert_one(dict(txn)))
    svc = PaymentService.__new__(PaymentService)
    monkeypatch.setattr(PaymentService, "payment_transactions_collection",
                        property(lambda self: db["payment_transactions"]))
    monkeypatch.setattr("app.database.get_db", lambda: db)
    run(svc._complete_onebox_purchase("URI_OBX_1", {"ok": True}))
    run(svc._complete_onebox_purchase("URI_OBX_1", {"ok": True}))      # the webhook after the poll
    s = sub(db)
    assert s["plan_id"] == "growth" and s["allowance"] == 1000 and s["status"] == "active"
    assert run(db["payment_transactions"].find_one({"transaction_ref": "URI_OBX_1"}))["status"] == "completed"


def test_usage_still_loads_when_the_new_trial_row_cannot_be_read_back():
    db = make_db()

    class Lagging:
        """Reads of subscriptions come back empty, as when a read lands before the write."""
        def __init__(self, real):
            self.real = real

        def __getitem__(self, name):
            coll = self.real[name]
            if name != onebox.SUBSCRIPTIONS:
                return coll

            class Coll:
                insert_one = staticmethod(coll.insert_one)

                async def find_one(self, *a, **k):
                    return None
            return Coll()

    out = run(onebox.usage(Lagging(db), "fresh-ws"))
    assert out["plan_id"] == "trial" and out["allowance"] > 0 and out["used"] == 0

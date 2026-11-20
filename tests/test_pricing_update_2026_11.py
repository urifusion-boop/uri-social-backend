"""Plans per "URI Social Pricing Update" (Nov 2026)."""
import asyncio

import pytest

from app.services.SubscriptionService import SubscriptionService

EXPECTED = {  # tier: (monthly ₦, monthly credits, price per credit ₦) — straight from the document
    "starter": (30000, 45, 667),
    "growth": (45000, 75, 600),
    "pro": (75000, 130, 577),
    "agency": (120000, 220, 545),
}


def _tiers():
    return {t.tier_id: t for t in SubscriptionService().default_tiers()}


@pytest.mark.parametrize("tier_id", list(EXPECTED))
def test_each_plan_has_the_documents_price_credits_and_price_per_credit(tier_id):
    t = _tiers()[tier_id]
    monthly, credits, per_credit = EXPECTED[tier_id]
    assert (t.price_ngn_monthly, t.price_ngn, t.credits_monthly, t.credits, t.price_per_credit) == (
        monthly, monthly, credits, credits, per_credit)


def test_multi_month_prices_keep_the_5_percent_discount_on_the_new_monthly_price():
    t = _tiers()["starter"]
    assert (t.price_ngn_3months, t.price_ngn_6months, t.price_ngn_12months) == (85500, 171000, 342000)


def test_no_plan_calls_its_allowance_campaigns():
    for t in _tiers().values():
        assert not any("campaign" in f.lower() for f in t.features), t.tier_id
    assert "45 credits/month" in _tiers()["starter"].features


class _Col:
    def __init__(self, docs):
        self.docs = {d["tier_id"]: dict(d) for d in docs}

    async def find_one(self, q):
        d = self.docs.get(q["tier_id"])
        return dict(d) if d else None

    async def update_one(self, q, upd, upsert=False):
        self.docs.setdefault(q["tier_id"], {"tier_id": q["tier_id"]}).update(upd["$set"])


def test_sync_updates_stored_plans_once_and_is_idempotent(monkeypatch):
    col = _Col([{"tier_id": "starter", "price_ngn_monthly": 15000, "credits_monthly": 20, "price_per_credit": 750}])
    svc = SubscriptionService()
    monkeypatch.setattr(SubscriptionService, "subscription_tiers_collection", property(lambda self: col))
    run = asyncio.get_event_loop().run_until_complete
    changed = run(svc.sync_tiers_to_defaults())
    starter = next(c for c in changed if c["tier_id"] == "starter")
    assert starter["from"]["price_ngn_monthly"] == 15000 and starter["to"]["price_ngn_monthly"] == 30000
    assert col.docs["agency"]["credits_monthly"] == 220
    assert run(svc.sync_tiers_to_defaults()) == []


# ── Low-balance and out-of-credits notifications ──────────────────────────────

def _sent(monkeypatch):
    from app.services import brand_notifier
    sent = []

    async def fake_notify(db, user_id, brand_id, event, title, message, **kw):
        sent.append(event)
        return {}

    monkeypatch.setattr(brand_notifier, "notify", fake_notify)
    return sent


@pytest.mark.parametrize("before,after,expected", [
    (5, 4, []),                       # still above the low level
    (4, 3, ["credits_low"]),          # reaches the low level: told once
    (3, 2, []),                       # already low: not told again on every deduction
    (1, 0, ["credits_exhausted"]),    # runs out
    (6, 0, ["credits_exhausted"]),    # a 6-credit video taking the last six: out, not "low"
])
def test_customers_are_told_once_when_credits_run_low_and_when_they_run_out(monkeypatch, before, after, expected):
    from app.services.CreditService import notify_balance_crossing
    sent = _sent(monkeypatch)
    asyncio.get_event_loop().run_until_complete(notify_balance_crossing(None, "u1", before, after))
    assert sent == expected


def test_both_alerts_are_always_on():
    from app.services import brand_notifier
    assert {"credits_low", "credits_exhausted"} <= brand_notifier._ALWAYS_EVENTS

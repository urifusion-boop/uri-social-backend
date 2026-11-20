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


@pytest.mark.parametrize("tier,before,after,expected", [
    ("starter", 6, 5, ["credits_low"]),       # Starter 45 → low at 5 (10%, rounded up)
    ("starter", 7, 6, []),                    # still above 5
    ("starter", 5, 4, []),                    # already low: not told again
    ("agency", 23, 22, ["credits_low"]),      # Agency 220 → low at 22
    ("growth", 9, 8, ["credits_low"]),        # Growth 75 → 8
    ("pro", 14, 13, ["credits_low"]),         # Pro 130 → 13
    ("starter", 1, 0, ["credits_exhausted"]), # runs out
    ("starter", 9, 0, ["credits_exhausted"]), # a big charge taking the last credits: out, not "low"
])
def test_customers_are_told_once_at_10_percent_of_their_plan_and_when_they_run_out(monkeypatch, tier, before, after, expected):
    from app.services.CreditService import monthly_credits_for, notify_balance_crossing
    sent = _sent(monkeypatch)
    asyncio.get_event_loop().run_until_complete(
        notify_balance_crossing(None, "u1", before, after, monthly_credits=monthly_credits_for(tier)))
    assert sent == expected


def test_the_trial_is_told_when_its_last_credit_is_left():
    from app.services.CreditService import low_balance_level
    assert low_balance_level(10) == 1


def test_custom_credits_cost_the_starter_rate_and_the_rate_is_not_shown():
    from app.services.PaymentService import PaymentService
    tiers = _tiers()
    assert PaymentService.CUSTOM_CREDIT_PRICE_NGN == tiers["starter"].price_per_credit == 667
    assert tiers["custom"].price_per_credit == 667
    assert not any("₦" in f for f in tiers["custom"].features)


def test_existing_subscribers_get_the_new_credits_at_renewal_not_before():
    # The plan sync only edits the stored plans; renewal reads the plan as it is then.
    import inspect
    from app.services.SubscriptionService import SubscriptionService as S
    assert "user_credits" not in inspect.getsource(S.sync_tiers_to_defaults)
    assert "get_tier_by_id" in inspect.getsource(S.renew_subscription)


def test_both_alerts_are_always_on():
    from app.services import brand_notifier
    assert {"credits_low", "credits_exhausted"} <= brand_notifier._ALWAYS_EVENTS

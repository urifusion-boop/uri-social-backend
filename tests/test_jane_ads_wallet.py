"""
Unit tests for the Jane + Ads wallet + ledger (split-doc 1.4).

Prepaid-first, dynamic pricing, and an auditable ledger — all against the in-memory
store, no DB. Deterministic `now` is injected so trailing-cost windows are stable.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.agents.jane_ads import constants as C
from app.agents.jane_ads.entities import TransactionType
from app.agents.jane_ads.store import InMemoryWalletStore
from app.agents.jane_ads.wallet import (
    InsufficientFundsError,
    MinimumTopUpError,
    WalletService,
)

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _svc() -> WalletService:
    return WalletService(InMemoryWalletStore())


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── Top-up ────────────────────────────────────────────────────────────────────

def test_topup_credits_balance():
    svc = _svc()
    _run(svc.top_up("b1", 10_000, reference="squad_ref_1", now=T0))
    assert _run(svc.get_balance("b1")) == 10_000


def test_topup_below_minimum_rejected():
    svc = _svc()
    with pytest.raises(MinimumTopUpError):
        _run(svc.top_up("b1", C.MIN_TOPUP_NGN - 1, now=T0))


def test_topup_records_transaction():
    svc = _svc()
    _run(svc.top_up("b1", 5_000, reference="ref", now=T0))
    txns = _run(svc.list_transactions("b1"))
    assert len(txns) == 1
    assert txns[0].type == TransactionType.TOPUP
    assert txns[0].amount_ngn == 5_000
    assert txns[0].balance_after_ngn == 5_000


def test_topup_is_idempotent_by_reference():
    # A Squad webhook can fire twice with the same reference — credit only once.
    svc = _svc()
    _run(svc.top_up("b1", 10_000, reference="squad_ref_X", now=T0))
    _run(svc.top_up("b1", 10_000, reference="squad_ref_X", now=T0))   # duplicate
    assert _run(svc.get_balance("b1")) == 10_000                       # not 20,000
    assert len(_run(svc.list_transactions("b1"))) == 1


def test_topup_without_reference_not_deduplicated():
    # Distinct manual top-ups (no ref) should both count.
    svc = _svc()
    _run(svc.top_up("b1", 5_000, now=T0))
    _run(svc.top_up("b1", 5_000, now=T0))
    assert _run(svc.get_balance("b1")) == 10_000


# ── Prepaid-first ───────────────────────────────────────────────────────────

def test_charge_on_empty_wallet_raises():
    svc = _svc()
    with pytest.raises(InsufficientFundsError):
        _run(svc.charge_conversation("b1", now=T0))


def test_charge_deducts_and_records():
    svc = _svc()
    _run(svc.top_up("b1", 5_000, now=T0))
    txn = _run(svc.charge_conversation("b1", campaign_id="c1", ad_id="a1", now=T0))
    # No trailing data yet → price floor ₦400.
    assert txn.amount_ngn == -C.CONVERSATION_PRICE_FLOOR_NGN
    assert _run(svc.get_balance("b1")) == 5_000 - C.CONVERSATION_PRICE_FLOOR_NGN


def test_charge_stops_when_balance_exhausted():
    svc = _svc()
    _run(svc.top_up("b1", 5_000, now=T0))          # ₦5,000 / ₦400 = 12 charges, then stop
    charged = 0
    while True:
        try:
            _run(svc.charge_conversation("b1", now=T0))
            charged += 1
        except InsufficientFundsError:
            break
    assert charged == 12
    assert _run(svc.get_balance("b1")) < C.CONVERSATION_PRICE_FLOOR_NGN


# ── Dynamic pricing (PRD B3) ─────────────────────────────────────────────────

def test_price_floor_when_no_trailing_data():
    assert WalletService.price_conversation(None) == C.CONVERSATION_PRICE_FLOOR_NGN
    assert WalletService.price_conversation(0) == C.CONVERSATION_PRICE_FLOOR_NGN


def test_price_is_trailing_times_multiplier_when_above_floor():
    # trailing ₦500 × 1.5 = ₦750 > ₦400 floor
    assert WalletService.price_conversation(500) == 750.0


def test_price_uses_floor_when_trailing_low():
    # trailing ₦100 × 1.5 = ₦150 < ₦400 floor → floor
    assert WalletService.price_conversation(100) == C.CONVERSATION_PRICE_FLOOR_NGN


def test_trailing_cost_drives_next_charge():
    svc = _svc()
    _run(svc.top_up("b1", 10_000, now=T0))
    # Record a charge that captured a high actual platform cost (₦600).
    _run(svc.charge_conversation("b1", actual_platform_cost_ngn=600, now=T0))
    # Next charge prices off trailing ₦600 × 1.5 = ₦900.
    txn = _run(svc.charge_conversation("b1", now=T0 + timedelta(hours=1)))
    assert txn.amount_ngn == -900.0


def test_trailing_cost_ignores_data_outside_window():
    svc = _svc()
    _run(svc.top_up("b1", 10_000, now=T0))
    _run(svc.charge_conversation("b1", actual_platform_cost_ngn=600, now=T0))
    # 8 days later — the old cost is outside the 7-day window → back to floor.
    later = T0 + timedelta(days=8)
    txn = _run(svc.charge_conversation("b1", now=later))
    assert txn.amount_ngn == -C.CONVERSATION_PRICE_FLOOR_NGN


# ── Ledger invariant ──────────────────────────────────────────────────────────

def test_ledger_sums_to_balance():
    svc = _svc()
    _run(svc.top_up("b1", 10_000, now=T0))
    for _ in range(5):
        _run(svc.charge_conversation("b1", now=T0))
    txns = _run(svc.list_transactions("b1"))
    assert abs(sum(t.amount_ngn for t in txns) - _run(svc.get_balance("b1"))) < 0.01


# ── Bridge to decision engine ─────────────────────────────────────────────────

def test_authorization_uses_wallet_balance_as_cap():
    svc = _svc()
    _run(svc.top_up("b1", 15_000, now=T0))
    auth = _run(svc.authorization_for("b1", total_funded_wallets_ngn=250_000))
    assert auth.funded_amount_ngn == 15_000
    assert auth.account_cap_ngn == 250_000


# ── URI's fee comes OUT of the client's stated budget (not added on top) ──

def test_fee_and_ad_spend_always_sum_to_the_stated_budget():
    """The client's stated budget is the whole of what leaves their wallet. ₦20,000
    stated = ₦2,000 fee + ₦18,000 of ads, and the duration is planned off the ₦18,000.
    The previous model added the fee on top ("₦20,000 ad spend + ₦2,000 service fee =
    ₦22,000 from your wallet"), asking clients to fund more than the figure they gave."""
    from app.agents.jane_ads import constants as C

    for budget in (5_000, 10_000, 20_000, 25_000, 137_500):
        spend = C.ad_spend_from_budget(budget)
        fee = C.service_fee_from_budget(budget)
        assert round(spend + fee, 2) == float(budget), budget
        assert spend < budget


def test_twenty_thousand_splits_into_eighteen_and_two():
    from app.agents.jane_ads import constants as C
    assert C.ad_spend_from_budget(20_000) == 18_000.0
    assert C.service_fee_from_budget(20_000) == 2_000.0


def test_the_billing_meter_recoups_exactly_the_stated_budget():
    """AD_SPEND_MARKUP is DERIVED from the fee rate so the wallet lands at zero when
    Meta finishes spending. Hard-coding 1.10 alongside a 10%-of-total fee would have
    collected ₦19,800 of every ₦20,000 and left ₦200 uncollected, every campaign."""
    from app.agents.jane_ads import constants as C

    for budget in (5_000, 10_000, 20_000, 25_000):
        spend = C.ad_spend_from_budget(budget)
        assert abs(round(spend * C.AD_SPEND_MARKUP, 2) - budget) < 0.01, budget


# ── confirm_topup: what counts as a failed payment ────────────────────────────
#
# Squad's checkout can charge a card and still report failure. Live-confirmed
# 2026-09-09: their ValidateOTP endpoint returned 504 on a payment that had ALREADY
# succeeded — card debited, receipt emailed, webhook delivered, and their own
# dashboard showing "Success" — while the popup told the customer "Payment Failed".
# Writing an inconclusive verify off as "failed" would have marked a real, paid
# top-up dead and stopped a later verify or webhook from ever crediting it.

class _FakeTopups:
    def __init__(self, docs):
        self.docs = docs

    async def find_one(self, query):
        return next((dict(d) for d in self.docs if d["reference"] == query["reference"]), None)

    async def update_one(self, query, update):
        for d in self.docs:
            if d["reference"] == query["reference"]:
                d.update(update.get("$set", {}))


class _FakeDb:
    def __init__(self, topups):
        self.jane_ads_topups = topups

    def __getitem__(self, name):
        return self.jane_ads_topups


def _payments(record, verify_status_code, verify_body):
    from unittest.mock import AsyncMock, patch
    from app.agents.jane_ads.payments import JaneAdsPayments

    topups = _FakeTopups([record])
    pay = JaneAdsPayments.__new__(JaneAdsPayments)
    pay._db = _FakeDb(topups)
    pay._topups = topups
    pay._wallet = WalletService(InMemoryWalletStore())

    resp = type("R", (), {"status_code": verify_status_code, "json": lambda self: verify_body})()
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    ctx = patch("httpx.AsyncClient")
    creds = patch("app.agents.jane_ads.payments.payment_service._get_squad_credentials",
                  new=AsyncMock(return_value={"api_url": "https://x", "secret_key": "sk"}))
    return pay, topups, ctx, creds, client


def _rec(**kw):
    base = dict(reference="JANEADS_x", business_id="b1", amount_ngn=5_000.0,
                email="a@b.c", status="pending")
    base.update(kw)
    return base


def test_a_gateway_timeout_leaves_the_topup_pending_not_failed():
    rec = _rec()
    pay, topups, ctx, creds, client = _payments(rec, 504, {})
    with ctx as MockClient, creds:
        MockClient.return_value.__aenter__.return_value = client
        out = _run(pay.confirm_topup("JANEADS_x"))
    assert out["status"] == "pending"
    # Still pending, so a later verify or the webhook can still credit it.
    assert topups.docs[0]["status"] == "pending"


def test_a_transaction_still_in_flight_stays_pending():
    rec = _rec()
    pay, topups, ctx, creds, client = _payments(
        rec, 200, {"success": True, "data": {"transaction_status": "pending"}})
    with ctx as MockClient, creds:
        MockClient.return_value.__aenter__.return_value = client
        out = _run(pay.confirm_topup("JANEADS_x"))
    assert out["status"] == "pending"
    assert topups.docs[0]["status"] == "pending"


def test_an_explicit_failed_status_is_recorded_as_failed():
    rec = _rec()
    pay, topups, ctx, creds, client = _payments(
        rec, 200, {"success": True, "data": {"transaction_status": "failed"}})
    with ctx as MockClient, creds:
        MockClient.return_value.__aenter__.return_value = client
        out = _run(pay.confirm_topup("JANEADS_x"))
    assert out["status"] == "failed"
    assert topups.docs[0]["status"] == "failed"


def test_a_success_credits_the_wallet_once():
    rec = _rec()
    pay, topups, ctx, creds, client = _payments(
        rec, 200, {"success": True, "data": {"transaction_status": "success"}})
    with ctx as MockClient, creds:
        MockClient.return_value.__aenter__.return_value = client
        first = _run(pay.confirm_topup("JANEADS_x"))
        second = _run(pay.confirm_topup("JANEADS_x"))
    assert first["status"] == "completed"
    assert first["balance_ngn"] == 5_000
    # Idempotent: the second call must not credit again.
    assert second.get("already_credited") is True
    assert _run(pay._wallet.get_balance("b1")) == 5_000


# ── Webhook authentication ────────────────────────────────────────────────────
#
# POST /jane-ads/wallet/webhook has no JWT — Squad calls it directly — so the
# HMAC-SHA512 signature is the only thing standing between a stranger and a free
# wallet credit. "Only references we created are acted on" is not authentication:
# references are predictable in shape and handed to the client.
#
# Squad keys the digest on the MERCHANT SECRET KEY (not SQUAD_WEBHOOK_SECRET, which
# is unused for this and holds a placeholder) and sends it uppercase-hex in
# x-squad-encrypted-body.

def _sign(raw: bytes, secret: str) -> str:
    import hashlib
    import hmac
    return hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest().upper()


def _with_secret(secret):
    from unittest.mock import AsyncMock, patch
    return patch("app.agents.jane_ads.payments.payment_service._get_squad_credentials",
                 new=AsyncMock(return_value={"secret_key": secret, "api_url": "https://x"}))


def test_a_correctly_signed_body_is_accepted():
    from app.agents.jane_ads.payments import JaneAdsPayments
    raw = b'{"TransactionRef":"JANEADS_x","Body":{"transaction_status":"success"}}'
    with _with_secret("sk_test"):
        assert _run(JaneAdsPayments.verify_webhook_signature(raw, _sign(raw, "sk_test"))) is True


def test_a_forged_body_is_rejected():
    """The attack this blocks: a stranger POSTing a fake success for a guessed
    reference to credit a real wallet with money nobody paid."""
    from app.agents.jane_ads.payments import JaneAdsPayments
    raw = b'{"TransactionRef":"JANEADS_x","Body":{"transaction_status":"success"}}'
    forged = b'{"TransactionRef":"JANEADS_x","Body":{"transaction_status":"success"} }'
    with _with_secret("sk_test"):
        assert _run(JaneAdsPayments.verify_webhook_signature(forged, _sign(raw, "sk_test"))) is False


def test_a_missing_signature_is_rejected():
    from app.agents.jane_ads.payments import JaneAdsPayments
    with _with_secret("sk_test"):
        assert _run(JaneAdsPayments.verify_webhook_signature(b'{}', "")) is False


def test_a_wrong_key_is_rejected():
    from app.agents.jane_ads.payments import JaneAdsPayments
    raw = b'{"TransactionRef":"JANEADS_x"}'
    with _with_secret("sk_real"):
        assert _run(JaneAdsPayments.verify_webhook_signature(raw, _sign(raw, "sk_attacker"))) is False


def test_signature_is_computed_over_raw_bytes_not_a_reserialised_dict():
    """Re-encoding the parsed body changes key order and separators, so the digest
    would never match what Squad actually signed — the raw bytes are the payload."""
    import json as _json
    from app.agents.jane_ads.payments import JaneAdsPayments
    raw = b'{"b": 2, "a": 1}'
    reserialised = _json.dumps(_json.loads(raw), separators=(',', ':')).encode()
    assert raw != reserialised
    with _with_secret("sk_test"):
        # Signed as Squad sent it → accepted.
        assert _run(JaneAdsPayments.verify_webhook_signature(raw, _sign(raw, "sk_test"))) is True
        # A digest over the re-serialised form → rejected, proving raw bytes are used.
        assert _run(JaneAdsPayments.verify_webhook_signature(raw, _sign(reserialised, "sk_test"))) is False


def test_no_configured_key_rejects_rather_than_letting_anything_through():
    from app.agents.jane_ads.payments import JaneAdsPayments
    with _with_secret(""):
        assert _run(JaneAdsPayments.verify_webhook_signature(b'{}', "ANYTHING")) is False

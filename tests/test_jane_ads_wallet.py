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

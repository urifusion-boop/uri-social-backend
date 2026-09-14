"""
Unit tests for the two-layer spend caps (split-doc 1.5, PRD §C3).

Wallets are constructed directly in the store so the cap logic is tested in isolation
from the pricing/charging path. No DB.
"""
import asyncio

from app.agents.jane_ads.caps import CapsService
from app.agents.jane_ads.entities import Wallet
from app.agents.jane_ads.store import InMemoryWalletStore


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _store(*wallets: Wallet) -> InMemoryWalletStore:
    s = InMemoryWalletStore()
    for w in wallets:
        _run(s.upsert_wallet(w))
    return s


def _wallet(bid: str, funded: float, spent: float) -> Wallet:
    return Wallet(
        business_id=bid,
        balance_ngn=funded - spent,
        total_topped_up_ngn=funded,
        total_spent_ngn=spent,
    )


# ── Layer 1: per-business ──────────────────────────────────────────────────────

def test_per_business_remaining():
    caps = CapsService(_store(_wallet("a", 10_000, 6_000)))
    assert _run(caps.per_business_remaining("a")) == 4_000


def test_business_spend_within_cap_allowed():
    caps = CapsService(_store(_wallet("a", 10_000, 6_000)))
    d = _run(caps.authorize_business_spend("a", 3_000))
    assert d.allowed is True


def test_business_spend_over_cap_denied():
    caps = CapsService(_store(_wallet("a", 10_000, 6_000)))
    d = _run(caps.authorize_business_spend("a", 5_000))   # only ₦4,000 left
    assert d.allowed is False
    assert "per-business cap" in d.reason


def test_businesses_to_pause_when_contribution_exhausted():
    caps = CapsService(_store(
        _wallet("a", 10_000, 10_000),   # exhausted → pause
        _wallet("b", 5_000, 2_000),     # still has room
    ))
    assert _run(caps.businesses_to_pause(["a", "b"])) == ["a"]


# ── Layer 2: per-account ───────────────────────────────────────────────────────

def test_account_status_sums_the_pool():
    caps = CapsService(_store(_wallet("a", 10_000, 3_000), _wallet("b", 5_000, 1_000)))
    st = _run(caps.account_status(["a", "b"]))
    assert st.funded_ngn == 15_000 and st.spent_ngn == 4_000 and st.remaining_ngn == 11_000


def test_account_budget_within_total_funded_allowed():
    caps = CapsService(_store(_wallet("a", 10_000, 0), _wallet("b", 5_000, 0)))
    d = _run(caps.authorize_account_budget(["a", "b"], 15_000))
    assert d.allowed is True


def test_account_budget_over_total_funded_denied():
    caps = CapsService(_store(_wallet("a", 10_000, 0), _wallet("b", 5_000, 0)))
    d = _run(caps.authorize_account_budget(["a", "b"], 16_000))   # only ₦15,000 funded
    assert d.allowed is False
    assert "per-account cap" in d.reason


def test_unknown_business_has_zero_remaining():
    caps = CapsService(InMemoryWalletStore())
    assert _run(caps.per_business_remaining("ghost")) == 0.0
    assert _run(caps.authorize_business_spend("ghost", 1)).allowed is False

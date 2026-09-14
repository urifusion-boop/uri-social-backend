"""
Unit tests for the live budget-fairness engine (split-doc "Live Budget Fairness").

`MockAdPlatformAdapter` only simulates one business per campaign (pooling isn't
modeled there yet), so a small pooled fake adapter stands in here to exercise the
multi-business case fetch_per_ad_spend()/pause_ad() are built for. No DB.
"""
import asyncio
from datetime import datetime, timezone

from app.agents.jane_ads.adapters.base import AdPlatformAdapter
from app.agents.jane_ads.entities import Wallet
from app.agents.jane_ads.fairness import BudgetFairnessEngine
from app.agents.jane_ads.models import PerAdSpend, Platform
from app.agents.jane_ads.store import InMemoryWalletStore

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _PooledFakeAdapter(AdPlatformAdapter):
    """A fake adapter whose fetch_per_ad_spend returns a fixed, multi-business list —
    standing in for a pooled Meta ad set until the real adapter exists."""

    def __init__(self, spends: list[PerAdSpend]) -> None:
        self._spends = spends
        self.paused: list[tuple[str, str]] = []

    async def launch_campaign(self, plan, auth):
        raise NotImplementedError

    async def fetch_per_ad_spend(self, campaign_id: str) -> list[PerAdSpend]:
        return [s for s in self._spends if s.campaign_id == campaign_id]

    async def poll_conversations(self, campaign_id: str):
        raise NotImplementedError

    async def pause_ad(self, campaign_id: str, ad_id: str) -> bool:
        self.paused.append((campaign_id, ad_id))
        return True


def _store(*wallets: Wallet) -> InMemoryWalletStore:
    s = InMemoryWalletStore()
    for w in wallets:
        _run(s.upsert_wallet(w))
    return s


def _wallet(bid: str, funded: float) -> Wallet:
    return Wallet(business_id=bid, balance_ngn=funded, total_topped_up_ngn=funded)


def _spend(bid: str, ad_id: str, amount: float, campaign_id: str = "cmp1") -> PerAdSpend:
    return PerAdSpend(business_id=bid, ad_id=ad_id, campaign_id=campaign_id,
                      platform=Platform.META, spend_ngn=amount, at=T0)


def test_business_paused_when_its_own_funding_is_exhausted():
    # b1 funded 5,000 and has spent all of it; b2 funded 5,000 and has spent 2,000.
    # Pool total funded 10,000, total spend 7,000 — the pool itself is fine, but b1
    # must not keep spending on b2's remaining share.
    store = _store(_wallet("b1", 5_000), _wallet("b2", 5_000))
    adapter = _PooledFakeAdapter([_spend("b1", "ad1", 5_000), _spend("b2", "ad2", 2_000)])
    report = _run(BudgetFairnessEngine(store, adapter).enforce("cmp1"))

    by_business = {a.business_id: a for a in report.actions}
    assert by_business["b1"].paused is True
    assert by_business["b2"].paused is False
    assert adapter.paused == [("cmp1", "ad1")]
    assert report.account_exhausted is False


def test_business_not_paused_when_within_budget():
    store = _store(_wallet("b1", 10_000))
    adapter = _PooledFakeAdapter([_spend("b1", "ad1", 4_000)])
    report = _run(BudgetFairnessEngine(store, adapter).enforce("cmp1"))
    assert report.actions[0].paused is False
    assert adapter.paused == []


def test_account_level_pauses_everyone_even_if_one_business_still_has_headroom():
    # b1 funded 5,000 spent only 4,000 (individually fine) but b2 funded 5,000 spent
    # 6,000 (over its own share) — combined spend 10,000 >= combined funded 10,000, so
    # the pool as a whole has cleared total funding: pause EVERYONE, including b1.
    store = _store(_wallet("b1", 5_000), _wallet("b2", 5_000))
    adapter = _PooledFakeAdapter([_spend("b1", "ad1", 4_000), _spend("b2", "ad2", 6_000)])
    report = _run(BudgetFairnessEngine(store, adapter).enforce("cmp1"))

    assert report.account_exhausted is True
    assert all(a.paused for a in report.actions)
    assert set(adapter.paused) == {("cmp1", "ad1"), ("cmp1", "ad2")}


def test_unfunded_business_is_paused_defensively():
    # No wallet on record at all for b1 — funded defaults to 0, so any spend pauses it.
    store = _store()
    adapter = _PooledFakeAdapter([_spend("b1", "ad1", 1.0)])
    report = _run(BudgetFairnessEngine(store, adapter).enforce("cmp1"))
    assert report.actions[0].paused is True
    assert report.actions[0].funded_ngn == 0.0


def test_only_events_for_the_requested_campaign_are_considered():
    store = _store(_wallet("b1", 10_000), _wallet("b2", 10_000))
    adapter = _PooledFakeAdapter([
        _spend("b1", "ad1", 1_000, campaign_id="cmp1"),
        _spend("b2", "ad2", 9_999_999, campaign_id="cmp_other"),
    ])
    report = _run(BudgetFairnessEngine(store, adapter).enforce("cmp1"))
    assert len(report.actions) == 1
    assert report.actions[0].business_id == "b1"
    assert adapter.paused == []

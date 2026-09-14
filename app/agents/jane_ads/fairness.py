"""
Jane + Ads — live budget-fairness engine ("Live Budget Fairness" in the engineering
work split — originally scoped to Ibukun, taken up here since the actual pause
decision is pure logic over interfaces Shore already owns).

Consumes live `per_ad_spend` events for a pooled campaign and pauses any business's ad
the moment ITS OWN funded contribution is spent — so no business eats another's share
of the pool (caps.py Layer 1) — and pauses the whole pool as a last-resort safety net
if total spend somehow clears total funded (caps.py Layer 2: URI never fronts more
than it holds). Pure orchestration over `AdPlatformAdapter.fetch_per_ad_spend()` /
`.pause_ad()` and `WalletStore` — all of which already exist — so it's fully testable
today against the mock adapter. Ibukun's real Meta adapter satisfies the same
interface, so this needs no changes when the live feed replaces the mock (PRD Part 4
— the seam). The one piece that genuinely stays Ibukun's: tuning the polling cadence
against Meta's real feed latency.
"""
from __future__ import annotations

from pydantic import BaseModel

from .adapters.base import AdPlatformAdapter
from .store import WalletStore


class FairnessAction(BaseModel):
    business_id: str
    ad_id: str
    spend_ngn: float
    funded_ngn: float
    paused: bool
    reason: str


class FairnessReport(BaseModel):
    campaign_id: str
    actions: list[FairnessAction] = []
    account_exhausted: bool = False


class BudgetFairnessEngine:
    def __init__(self, store: WalletStore, adapter: AdPlatformAdapter) -> None:
        self._store = store
        self._adapter = adapter

    async def enforce(self, campaign_id: str) -> FairnessReport:
        """Fetch live spend for every business sharing this campaign and pause
        whoever has exhausted their own funded contribution — or everyone, if the
        pool's combined spend has cleared its combined funding."""
        events = await self._adapter.fetch_per_ad_spend(campaign_id)

        funded: dict[str, float] = {}
        for ev in events:
            if ev.business_id not in funded:
                wallet = await self._store.get_wallet(ev.business_id)
                funded[ev.business_id] = wallet.total_topped_up_ngn if wallet else 0.0

        total_funded = sum(funded.values())
        total_spend = sum(ev.spend_ngn for ev in events)
        account_exhausted = total_funded > 0 and total_spend >= total_funded

        actions: list[FairnessAction] = []
        for ev in events:
            business_funded = funded[ev.business_id]
            if account_exhausted:
                await self._adapter.pause_ad(campaign_id, ev.ad_id)
                actions.append(FairnessAction(
                    business_id=ev.business_id, ad_id=ev.ad_id,
                    spend_ngn=ev.spend_ngn, funded_ngn=business_funded, paused=True,
                    reason="pool spend has reached total funded across the pool",
                ))
            elif ev.spend_ngn >= business_funded:
                await self._adapter.pause_ad(campaign_id, ev.ad_id)
                actions.append(FairnessAction(
                    business_id=ev.business_id, ad_id=ev.ad_id,
                    spend_ngn=ev.spend_ngn, funded_ngn=business_funded, paused=True,
                    reason="this business's funded contribution is spent",
                ))
            else:
                actions.append(FairnessAction(
                    business_id=ev.business_id, ad_id=ev.ad_id,
                    spend_ngn=ev.spend_ngn, funded_ngn=business_funded, paused=False,
                    reason=f"₦{business_funded - ev.spend_ngn:,.2f} remaining",
                ))
        return FairnessReport(campaign_id=campaign_id, actions=actions, account_exhausted=account_exhausted)

"""
Jane + Ads — spend caps (PRD §C3; split-doc 1.5).

Two deterministic pre-authorization layers — build BOTH before any pooled campaign:

  LAYER 1  per-business cap  — one business can't spend past its own funded
           contribution (so it can never eat another business's share of a pool).
  LAYER 2  per-account cap   — URI never authorizes a pooled ad-set budget larger
           than the total funded across the pool (never fronts more than it holds).

Golden rule: every Naira traces to a business that funded it, and total spend never
exceeds total funded. This is the deterministic authorize/deny + who-to-pause logic;
the live near-real-time throttle loop against Meta's spend feed is Ibukun's runtime
piece that consumes these same rules.

Pure logic over a WalletStore — unit-testable with the in-memory store, no DB.
"""
from __future__ import annotations

from pydantic import BaseModel

from .store import WalletStore


class CapDecision(BaseModel):
    allowed: bool
    reason: str
    remaining_ngn: float


class AccountStatus(BaseModel):
    funded_ngn: float
    spent_ngn: float
    remaining_ngn: float


class CapsService:
    def __init__(self, store: WalletStore) -> None:
        self._store = store

    async def _funded_and_spent(self, business_id: str) -> tuple[float, float]:
        w = await self._store.get_wallet(business_id)
        if w is None:
            return 0.0, 0.0
        return w.total_topped_up_ngn, w.total_spent_ngn

    # ── Layer 1: per-business ──────────────────────────────────────────────────
    async def per_business_remaining(self, business_id: str) -> float:
        funded, spent = await self._funded_and_spent(business_id)
        return round(funded - spent, 2)

    async def authorize_business_spend(self, business_id: str, amount_ngn: float) -> CapDecision:
        """Allow a spend only if it stays within the business's funded contribution."""
        remaining = await self.per_business_remaining(business_id)
        if amount_ngn > remaining:
            return CapDecision(
                allowed=False,
                reason=(f"per-business cap: ₦{amount_ngn:,.2f} exceeds remaining "
                        f"contribution ₦{remaining:,.2f}"),
                remaining_ngn=remaining,
            )
        return CapDecision(allowed=True, reason="within per-business cap", remaining_ngn=remaining)

    async def businesses_to_pause(self, pool_business_ids: list[str]) -> list[str]:
        """Businesses whose contribution is exhausted → pause their ad (fairness)."""
        out: list[str] = []
        for bid in pool_business_ids:
            if await self.per_business_remaining(bid) <= 0:
                out.append(bid)
        return out

    # ── Layer 2: per-account ───────────────────────────────────────────────────
    async def account_status(self, pool_business_ids: list[str]) -> AccountStatus:
        funded = spent = 0.0
        for bid in pool_business_ids:
            f, s = await self._funded_and_spent(bid)
            funded += f
            spent += s
        return AccountStatus(
            funded_ngn=round(funded, 2),
            spent_ngn=round(spent, 2),
            remaining_ngn=round(funded - spent, 2),
        )

    async def authorize_account_budget(
        self, pool_business_ids: list[str], requested_budget_ngn: float
    ) -> CapDecision:
        """A pooled ad-set budget must never exceed the total funded across the pool —
        URI never fronts more than it holds."""
        status = await self.account_status(pool_business_ids)
        if requested_budget_ngn > status.funded_ngn:
            return CapDecision(
                allowed=False,
                reason=(f"per-account cap: budget ₦{requested_budget_ngn:,.2f} exceeds "
                        f"total funded ₦{status.funded_ngn:,.2f}"),
                remaining_ngn=status.funded_ngn,
            )
        return CapDecision(
            allowed=True, reason="within per-account cap", remaining_ngn=status.funded_ngn
        )

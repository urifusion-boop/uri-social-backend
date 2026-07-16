"""
Jane + Ads — wallet + ledger service (PRD §A4, §B3; split-doc 1.4).

Two money flows, never confused:
  FLOW A = URI's service fee (revenue).
  FLOW B = the customer's ad spend passing THROUGH to the platform (custodial, not
           revenue). This service governs Flow B: prepaid-first, dynamic pricing,
           and a fully auditable ledger where balance == sum(signed transactions).

Pure logic + a WalletStore — unit-testable with the in-memory store, no DB.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import constants as C
from .entities import (
    Transaction,
    TransactionStatus,
    TransactionType,
    Wallet,
    WalletStatus,
)
from .models import SpendAuthorization
from .store import WalletStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InsufficientFundsError(Exception):
    """Raised when a charge would exceed the prepaid balance (prepaid-first rule)."""


class MinimumTopUpError(Exception):
    """Raised when a top-up is below the PRD minimum (₦5,000)."""


class WalletService:
    def __init__(self, store: WalletStore) -> None:
        self._store = store

    # ── Balance ───────────────────────────────────────────────────────────────
    async def get_or_create(self, business_id: str) -> Wallet:
        wallet = await self._store.get_wallet(business_id)
        if wallet is None:
            wallet = Wallet(business_id=business_id)
            await self._store.upsert_wallet(wallet)
        return wallet

    async def get_balance(self, business_id: str) -> float:
        wallet = await self._store.get_wallet(business_id)
        return wallet.balance_ngn if wallet else 0.0

    # ── Top-up (Flow B funding) ────────────────────────────────────────────────
    async def top_up(
        self, business_id: str, amount_ngn: float, reference: str = "", now: Optional[datetime] = None
    ) -> Transaction:
        """Credit the wallet. Enforces the ₦5,000 minimum. `reference` is the
        Squad/Paystack payment ref."""
        if amount_ngn < C.MIN_TOPUP_NGN:
            raise MinimumTopUpError(
                f"Minimum top-up is ₦{C.MIN_TOPUP_NGN:,.0f}; got ₦{amount_ngn:,.0f}."
            )
        now = now or _now()

        # Idempotency: a payment webhook can fire more than once. If this reference has
        # already credited the wallet, return that transaction — never double-credit.
        if reference:
            for t in await self._store.list_transactions(business_id):
                if t.type == TransactionType.TOPUP and t.reference == reference:
                    return t

        wallet = await self.get_or_create(business_id)
        wallet.balance_ngn = round(wallet.balance_ngn + amount_ngn, 2)
        wallet.total_topped_up_ngn = round(wallet.total_topped_up_ngn + amount_ngn, 2)
        wallet.updated_at = now
        await self._store.upsert_wallet(wallet)

        txn = Transaction(
            transaction_id=f"txn_{uuid.uuid4().hex[:16]}",
            business_id=business_id,
            type=TransactionType.TOPUP,
            amount_ngn=round(amount_ngn, 2),
            balance_after_ngn=wallet.balance_ngn,
            reference=reference,
            created_at=now,
        )
        await self._store.add_transaction(txn)
        return txn

    # ── Dynamic pricing (PRD B3) ────────────────────────────────────────────────
    @staticmethod
    def price_conversation(trailing_cost_ngn: Optional[float]) -> float:
        """Charge per conversation = MAX(₦400, trailing-7-day platform cost × 1.5).
        Falls back to the floor when there's no trailing data yet."""
        floor = C.CONVERSATION_PRICE_FLOOR_NGN
        if not trailing_cost_ngn or trailing_cost_ngn <= 0:
            return floor
        return round(max(floor, trailing_cost_ngn * C.CONVERSATION_PRICE_MULTIPLIER), 2)

    async def trailing_cost_per_conversation(
        self, business_id: str, now: Optional[datetime] = None
    ) -> Optional[float]:
        """Average ACTUAL platform cost per conversation over the trailing 7 days,
        from the ledger. None if there's no data yet."""
        now = now or _now()
        since = now - timedelta(days=C.TRAILING_COST_WINDOW_DAYS)
        txns = await self._store.list_transactions(business_id, since=since)
        costs = [
            t.actual_platform_cost_ngn
            for t in txns
            if t.type == TransactionType.CONVERSATION_CHARGE
            and t.actual_platform_cost_ngn is not None
        ]
        if not costs:
            return None
        return sum(costs) / len(costs)

    # ── Charge a conversation (prepaid-first) ────────────────────────────────────
    async def charge_conversation(
        self,
        business_id: str,
        campaign_id: str = "",
        ad_id: str = "",
        actual_platform_cost_ngn: Optional[float] = None,
        now: Optional[datetime] = None,
    ) -> Transaction:
        """Deduct one dynamically-priced conversation. Prepaid-first: raises
        InsufficientFundsError if the balance can't cover the charge — nothing runs on
        an empty wallet."""
        now = now or _now()
        # Ensures a wallet doc exists before the debit attempt; the debit itself
        # doesn't rely on this read for its balance decision.
        await self.get_or_create(business_id)
        trailing = await self.trailing_cost_per_conversation(business_id, now=now)
        price = self.price_conversation(trailing)

        # Atomic guard-and-decrement: the ACTIVE/balance check and the deduction
        # happen as one store operation, so two concurrent charges against the
        # same wallet can't both read a sufficient balance and both succeed
        # (the previous read -> compute -> upsert pattern allowed exactly that).
        wallet = await self._store.try_debit(business_id, price, now)
        if wallet is None:
            current = await self._store.get_wallet(business_id)
            if current and current.status != WalletStatus.ACTIVE:
                raise InsufficientFundsError(f"Wallet {business_id} is {current.status.value}.")
            balance = current.balance_ngn if current else 0.0
            raise InsufficientFundsError(
                f"Balance ₦{balance:,.2f} < charge ₦{price:,.2f}."
            )

        txn = Transaction(
            transaction_id=f"txn_{uuid.uuid4().hex[:16]}",
            business_id=business_id,
            type=TransactionType.CONVERSATION_CHARGE,
            amount_ngn=round(-price, 2),
            balance_after_ngn=wallet.balance_ngn,
            campaign_id=campaign_id,
            ad_id=ad_id,
            actual_platform_cost_ngn=actual_platform_cost_ngn,
            created_at=now,
        )
        await self._store.add_transaction(txn)
        return txn

    async def can_afford(self, business_id: str, price_ngn: float) -> bool:
        return (await self.get_balance(business_id)) >= price_ngn

    async def list_transactions(self, business_id: str) -> list[Transaction]:
        return await self._store.list_transactions(business_id)

    # ── Bridge to the decision engine ───────────────────────────────────────────
    async def authorization_for(
        self, business_id: str, total_funded_wallets_ngn: float
    ) -> SpendAuthorization:
        """The per-business cap IS the wallet balance. Ties the wallet to the engine's
        SpendAuthorization so a plan can never be authorized beyond funded money."""
        balance = await self.get_balance(business_id)
        return SpendAuthorization(
            business_id=business_id,
            funded_amount_ngn=balance,
            account_cap_ngn=total_funded_wallets_ngn,
        )

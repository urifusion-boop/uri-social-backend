"""
Jane + Ads — wallet store (persistence abstraction, split-doc 1.3/1.4).

`WalletStore` is the interface the WalletService talks to. `InMemoryWalletStore`
backs the unit tests (no DB). `MongoWalletStore` is the production impl over the
existing Motor `db` handle. Swapping one for the other requires no service change.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from .entities import (
    Strategy,
    StrategyCategory,
    StrategyStatus,
    Transaction,
    Wallet,
    WalletStatus,
)


class WalletStore(ABC):
    @abstractmethod
    async def get_wallet(self, business_id: str) -> Optional[Wallet]: ...

    @abstractmethod
    async def upsert_wallet(self, wallet: Wallet) -> None: ...

    @abstractmethod
    async def try_debit(
        self, business_id: str, amount_ngn: float, now: datetime
    ) -> Optional[Wallet]:
        """Atomically debit `amount_ngn` if (and only if) the wallet is ACTIVE and
        balance_ngn >= amount_ngn — the balance check and the decrement happen as one
        operation. Returns the updated wallet, or None if the debit was refused
        (insufficient funds, suspended, or no wallet)."""

    @abstractmethod
    async def add_transaction(self, txn: Transaction) -> None: ...

    @abstractmethod
    async def list_transactions(
        self, business_id: str, since: Optional[datetime] = None
    ) -> list[Transaction]: ...


class InMemoryWalletStore(WalletStore):
    """Dict-backed store for tests and the mock end-to-end."""

    def __init__(self) -> None:
        self._wallets: dict[str, Wallet] = {}
        self._txns: list[Transaction] = []

    async def get_wallet(self, business_id: str) -> Optional[Wallet]:
        w = self._wallets.get(business_id)
        return w.model_copy(deep=True) if w else None

    async def upsert_wallet(self, wallet: Wallet) -> None:
        self._wallets[wallet.business_id] = wallet.model_copy(deep=True)

    async def try_debit(
        self, business_id: str, amount_ngn: float, now: datetime
    ) -> Optional[Wallet]:
        # asyncio is single-threaded so there's no real race here, but the
        # guard-then-mutate happens with no `await` in between, keeping the
        # semantics identical to the Mongo implementation.
        wallet = self._wallets.get(business_id)
        if (
            wallet is None
            or wallet.status != WalletStatus.ACTIVE
            or wallet.balance_ngn < amount_ngn
        ):
            return None
        wallet.balance_ngn = round(wallet.balance_ngn - amount_ngn, 2)
        wallet.total_spent_ngn = round(wallet.total_spent_ngn + amount_ngn, 2)
        wallet.updated_at = now
        self._wallets[business_id] = wallet.model_copy(deep=True)
        return wallet.model_copy(deep=True)

    async def add_transaction(self, txn: Transaction) -> None:
        self._txns.append(txn.model_copy(deep=True))

    async def list_transactions(
        self, business_id: str, since: Optional[datetime] = None
    ) -> list[Transaction]:
        out = [t for t in self._txns if t.business_id == business_id]
        if since is not None:
            out = [t for t in out if t.created_at >= since]
        return [t.model_copy(deep=True) for t in out]


class MongoWalletStore(WalletStore):
    """Production store. Collections:
      jane_ads_wallets       — one doc per business (keyed by business_id)
      jane_ads_transactions  — append-only ledger
    """

    def __init__(self, db) -> None:
        self._db = db

    async def get_wallet(self, business_id: str) -> Optional[Wallet]:
        doc = await self._db.jane_ads_wallets.find_one({"business_id": business_id}, {"_id": 0})
        return Wallet(**doc) if doc else None

    async def upsert_wallet(self, wallet: Wallet) -> None:
        await self._db.jane_ads_wallets.update_one(
            {"business_id": wallet.business_id},
            {"$set": wallet.model_dump()},
            upsert=True,
        )

    async def try_debit(
        self, business_id: str, amount_ngn: float, now: datetime
    ) -> Optional[Wallet]:
        from pymongo import ReturnDocument

        doc = await self._db.jane_ads_wallets.find_one_and_update(
            {
                "business_id": business_id,
                "status": WalletStatus.ACTIVE.value,
                "balance_ngn": {"$gte": amount_ngn},
            },
            {
                "$inc": {
                    "balance_ngn": -amount_ngn,
                    "total_spent_ngn": amount_ngn,
                },
                "$set": {"updated_at": now},
            },
            projection={"_id": 0},
            return_document=ReturnDocument.AFTER,
        )
        if not doc:
            return None
        # Guard against float drift accumulating in $inc over many charges.
        doc["balance_ngn"] = round(doc["balance_ngn"], 2)
        doc["total_spent_ngn"] = round(doc["total_spent_ngn"], 2)
        return Wallet(**doc)

    async def add_transaction(self, txn: Transaction) -> None:
        await self._db.jane_ads_transactions.insert_one(txn.model_dump())

    async def list_transactions(
        self, business_id: str, since: Optional[datetime] = None
    ) -> list[Transaction]:
        query: dict = {"business_id": business_id}
        if since is not None:
            query["created_at"] = {"$gte": since}
        docs = await self._db.jane_ads_transactions.find(query, {"_id": 0}).to_list(length=1000)
        return [Transaction(**d) for d in docs]


# ── Ad strategy corpus ────────────────────────────────────────────────────────

# Ingestion stores every row so the Draft -> In review -> Approved workflow has
# something to work on, but retrieval defaults to Approved only: an unreviewed
# draft must never shape a live campaign plan. Pass `statuses=None` to search the
# whole corpus (review tooling, dedupe checks), never for user-facing planning.
RETRIEVABLE_STATUSES: set[StrategyStatus] = {StrategyStatus.APPROVED}


class StrategyStore(ABC):
    """Interface the StrategyCorpusService talks to. Same split as WalletStore:
    InMemory backs unit tests, Mongo is production."""

    @abstractmethod
    async def upsert_strategy(self, strategy: Strategy) -> None: ...

    @abstractmethod
    async def get_strategy(self, strategy_id: str) -> Optional[Strategy]: ...

    @abstractmethod
    async def find_strategies(
        self,
        *,
        category: Optional[StrategyCategory] = None,
        max_budget_floor_ngn: Optional[float] = None,
        statuses: Optional[set[StrategyStatus]] = RETRIEVABLE_STATUSES,
        limit: int = 50,
    ) -> list[Strategy]: ...

    @abstractmethod
    async def count(self) -> int: ...


class InMemoryStrategyStore(StrategyStore):
    def __init__(self) -> None:
        self._items: dict[str, Strategy] = {}

    async def upsert_strategy(self, strategy: Strategy) -> None:
        self._items[strategy.strategy_id] = strategy

    async def get_strategy(self, strategy_id: str) -> Optional[Strategy]:
        return self._items.get(strategy_id)

    async def find_strategies(
        self,
        *,
        category: Optional[StrategyCategory] = None,
        max_budget_floor_ngn: Optional[float] = None,
        statuses: Optional[set[StrategyStatus]] = RETRIEVABLE_STATUSES,
        limit: int = 50,
    ) -> list[Strategy]:
        out = list(self._items.values())
        if statuses is not None:
            # A record with no status has not been through review either.
            out = [s for s in out if s.status in statuses]
        if category is not None:
            out = [s for s in out if s.category is category]
        if max_budget_floor_ngn is not None:
            # A record with no floor is a "does not transfer" rejection — it carries no
            # budget precondition, so an affordability filter must not surface it.
            out = [
                s for s in out
                if s.budget_floor_ngn_per_day is not None
                and s.budget_floor_ngn_per_day <= max_budget_floor_ngn
            ]
        out.sort(key=lambda s: (-s.evidence_grade.rank, s.strategy_id))
        return out[:limit]

    async def count(self) -> int:
        return len(self._items)


class MongoStrategyStore(StrategyStore):
    """Production store. Collection:
      jane_ads_strategies — one doc per tactic (keyed by strategy_id)
    """

    def __init__(self, db) -> None:
        self._db = db

    async def ensure_indexes(self) -> None:
        await self._db.jane_ads_strategies.create_index("strategy_id", unique=True)
        await self._db.jane_ads_strategies.create_index(
            [("category", 1), ("budget_floor_ngn_per_day", 1)]
        )
        await self._db.jane_ads_strategies.create_index("transfer_verdict")
        await self._db.jane_ads_strategies.create_index("status")

    async def upsert_strategy(self, strategy: Strategy) -> None:
        await self._db.jane_ads_strategies.update_one(
            {"strategy_id": strategy.strategy_id},
            {"$set": strategy.model_dump()},
            upsert=True,
        )

    async def get_strategy(self, strategy_id: str) -> Optional[Strategy]:
        doc = await self._db.jane_ads_strategies.find_one(
            {"strategy_id": strategy_id}, {"_id": 0}
        )
        return Strategy(**doc) if doc else None

    async def find_strategies(
        self,
        *,
        category: Optional[StrategyCategory] = None,
        max_budget_floor_ngn: Optional[float] = None,
        statuses: Optional[set[StrategyStatus]] = RETRIEVABLE_STATUSES,
        limit: int = 50,
    ) -> list[Strategy]:
        query: dict = {}
        if statuses is not None:
            query["status"] = {"$in": sorted(st.value for st in statuses)}
        if category is not None:
            query["category"] = category.value
        if max_budget_floor_ngn is not None:
            query["budget_floor_ngn_per_day"] = {
                "$ne": None, "$lte": max_budget_floor_ngn,
            }
        docs = await self._db.jane_ads_strategies.find(query, {"_id": 0}).to_list(length=limit)
        out = [Strategy(**d) for d in docs]
        out.sort(key=lambda s: (-s.evidence_grade.rank, s.strategy_id))
        return out

    async def count(self) -> int:
        return await self._db.jane_ads_strategies.count_documents({})

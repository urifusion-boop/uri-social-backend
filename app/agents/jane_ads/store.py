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
    ConsumedBy,
    PooledAccountSafety,
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


# ── Ad strategy corpus (ASC-SPEC-01 v2 / ASC-ENG-01 v1) ──────────────────────

class IngestionCannotApprove(Exception):
    """Spec §1.1: only a human moves draft -> approved, no exceptions, including for
    records that look obviously correct. ENG §8 fixture 12 requires this be refused
    at the DATA layer, not merely in a service — so the write path itself raises."""


class ImmutableRecordError(Exception):
    """Spec §3.3: records are immutable once approved. An edit creates a new version;
    overwriting in place would make an October plan unexplainable in January."""


class StrategyStore(ABC):
    """Interface the corpus service talks to. Records are keyed by
    (strategy_id, version) — never strategy_id alone."""

    @abstractmethod
    async def ingest(self, strategy: Strategy) -> None:
        """Write a draft/rejected record. MUST raise IngestionCannotApprove if the
        record arrives already approved."""

    @abstractmethod
    async def approve(self, strategy_id: str, version: int, approved_by: str) -> Strategy: ...

    @abstractmethod
    async def get(self, strategy_id: str, version: Optional[int] = None) -> Optional[Strategy]:
        """`version=None` returns the live (approved) version, else the latest."""

    @abstractmethod
    async def fetch_approved(self) -> list[Strategy]:
        """Retrieval candidate set. Exclusion and scoring happen above the store —
        ENG §4 requires exclusion before scoring, so the store does not pre-filter
        on preconditions."""

    @abstractmethod
    async def count(self, *, status: Optional[StrategyStatus] = None) -> int: ...


def _guard_ingest(strategy: Strategy) -> None:
    if strategy.status is StrategyStatus.APPROVED:
        raise IngestionCannotApprove(
            f"{strategy.strategy_id} arrived as 'approved'; ingestion may only write "
            "draft/in_review/rejected (spec §1.1)"
        )


class InMemoryStrategyStore(StrategyStore):
    def __init__(self) -> None:
        self._items: dict[tuple[str, int], Strategy] = {}

    async def ingest(self, strategy: Strategy) -> None:
        _guard_ingest(strategy)
        key = (strategy.strategy_id, strategy.version)
        existing = self._items.get(key)
        if existing is not None and existing.status is StrategyStatus.APPROVED:
            raise ImmutableRecordError(
                f"{strategy.strategy_id} v{strategy.version} is approved; "
                "create a new version instead of overwriting"
            )
        self._items[key] = strategy

    async def approve(self, strategy_id: str, version: int, approved_by: str) -> Strategy:
        rec = self._items.get((strategy_id, version))
        if rec is None:
            raise KeyError(f"{strategy_id} v{version} not found")
        live = [
            s for (sid, _v), s in self._items.items()
            if sid == strategy_id and s.status is StrategyStatus.APPROVED
        ]
        for prev in live:                       # exactly one live version per record
            prev.status = StrategyStatus.IN_REVIEW
        rec.status = StrategyStatus.APPROVED
        rec.ingested_by = approved_by
        return rec

    async def get(self, strategy_id: str, version: Optional[int] = None) -> Optional[Strategy]:
        if version is not None:
            return self._items.get((strategy_id, version))
        versions = [s for (sid, _v), s in self._items.items() if sid == strategy_id]
        if not versions:
            return None
        approved = [s for s in versions if s.status is StrategyStatus.APPROVED]
        return approved[0] if approved else max(versions, key=lambda s: s.version)

    async def fetch_approved(self) -> list[Strategy]:
        return [s for s in self._items.values() if s.status is StrategyStatus.APPROVED]

    async def count(self, *, status: Optional[StrategyStatus] = None) -> int:
        if status is None:
            return len(self._items)
        return sum(1 for s in self._items.values() if s.status is status)


class MongoStrategyStore(StrategyStore):
    """Production store. Collection:
      jane_ads_strategies — one doc per (strategy_id, version)
    """

    def __init__(self, db) -> None:
        self._db = db

    # v1 shipped a plain unique index on strategy_id and a compound index on a
    # since-renamed field. The former makes a second version of any record
    # unwritable — it defeats the whole versioning model — so ensure_indexes must
    # remove it, not merely add the correct one alongside.
    STALE_V1_INDEXES = ("strategy_id_1", "category_1_budget_floor_ngn_per_day_1")

    async def ensure_indexes(self) -> None:
        existing = await self._db.jane_ads_strategies.index_information()
        for name in self.STALE_V1_INDEXES:
            if name in existing:
                await self._db.jane_ads_strategies.drop_index(name)

        await self._db.jane_ads_strategies.create_index(
            [("strategy_id", 1), ("version", 1)], unique=True
        )
        # Exactly one live version per record (ENG §3 one_live_version).
        await self._db.jane_ads_strategies.create_index(
            "strategy_id", unique=True,
            partialFilterExpression={"status": StrategyStatus.APPROVED.value},
            name="one_live_version",
        )
        await self._db.jane_ads_strategies.create_index(
            [("status", 1), ("pooled_account_safe", 1), ("executable_via", 1)]
        )
        await self._db.jane_ads_strategies.create_index("consumed_by")
        await self._db.jane_ads_strategies.create_index("platforms")
        await self._db.jane_ads_strategies.create_index("conversion_location")

    async def ingest(self, strategy: Strategy) -> None:
        _guard_ingest(strategy)
        existing = await self._db.jane_ads_strategies.find_one(
            {"strategy_id": strategy.strategy_id, "version": strategy.version},
            {"_id": 0, "status": 1},
        )
        if existing and existing.get("status") == StrategyStatus.APPROVED.value:
            raise ImmutableRecordError(
                f"{strategy.strategy_id} v{strategy.version} is approved; "
                "create a new version instead of overwriting"
            )
        await self._db.jane_ads_strategies.update_one(
            {"strategy_id": strategy.strategy_id, "version": strategy.version},
            {"$set": strategy.model_dump(mode="json")},
            upsert=True,
        )

    async def approve(self, strategy_id: str, version: int, approved_by: str) -> Strategy:
        await self._db.jane_ads_strategies.update_many(
            {"strategy_id": strategy_id, "status": StrategyStatus.APPROVED.value},
            {"$set": {"status": StrategyStatus.IN_REVIEW.value}},
        )
        await self._db.jane_ads_strategies.update_one(
            {"strategy_id": strategy_id, "version": version},
            {"$set": {"status": StrategyStatus.APPROVED.value, "ingested_by": approved_by}},
        )
        return await self.get(strategy_id, version)

    async def get(self, strategy_id: str, version: Optional[int] = None) -> Optional[Strategy]:
        if version is not None:
            doc = await self._db.jane_ads_strategies.find_one(
                {"strategy_id": strategy_id, "version": version}, {"_id": 0}
            )
            return Strategy(**doc) if doc else None
        doc = await self._db.jane_ads_strategies.find_one(
            {"strategy_id": strategy_id, "status": StrategyStatus.APPROVED.value}, {"_id": 0}
        )
        if doc:
            return Strategy(**doc)
        docs = await self._db.jane_ads_strategies.find(
            {"strategy_id": strategy_id}, {"_id": 0}
        ).sort("version", -1).to_list(length=1)
        return Strategy(**docs[0]) if docs else None

    async def fetch_approved(self) -> list[Strategy]:
        docs = await self._db.jane_ads_strategies.find(
            {"status": StrategyStatus.APPROVED.value}, {"_id": 0}
        ).to_list(length=5000)
        return [Strategy(**d) for d in docs]

    async def count(self, *, status: Optional[StrategyStatus] = None) -> int:
        q = {} if status is None else {"status": status.value}
        return await self._db.jane_ads_strategies.count_documents(q)


# ── Coverage gaps (ENG §5.3) ─────────────────────────────────────────────────

class CoverageGapStore(ABC):
    """Empty retrievals are data, not errors. Which stage / tier / business-type /
    conversion-location combinations return nothing IS the seeding roadmap — before
    outcomes exist it is the most useful signal the system produces. Do not alert
    on these; do not swallow them."""

    @abstractmethod
    async def log_gap(self, gap: dict) -> None: ...

    @abstractmethod
    async def list_gaps(self, limit: int = 100) -> list[dict]: ...


class InMemoryCoverageGapStore(CoverageGapStore):
    def __init__(self) -> None:
        self.gaps: list[dict] = []

    async def log_gap(self, gap: dict) -> None:
        self.gaps.append(gap)

    async def list_gaps(self, limit: int = 100) -> list[dict]:
        return self.gaps[:limit]


class MongoCoverageGapStore(CoverageGapStore):
    """Collection: jane_ads_corpus_coverage_gaps"""

    def __init__(self, db) -> None:
        self._db = db

    async def ensure_indexes(self) -> None:
        await self._db.jane_ads_corpus_coverage_gaps.create_index(
            [("stage", 1), ("budget_tier", 1), ("conversion_location", 1)]
        )
        await self._db.jane_ads_corpus_coverage_gaps.create_index("occurred_at")

    async def log_gap(self, gap: dict) -> None:
        await self._db.jane_ads_corpus_coverage_gaps.insert_one(dict(gap))

    async def list_gaps(self, limit: int = 100) -> list[dict]:
        return await self._db.jane_ads_corpus_coverage_gaps.find(
            {}, {"_id": 0}
        ).sort("occurred_at", -1).to_list(length=limit)

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

from .entities import Transaction, Wallet


class WalletStore(ABC):
    @abstractmethod
    async def get_wallet(self, business_id: str) -> Optional[Wallet]: ...

    @abstractmethod
    async def upsert_wallet(self, wallet: Wallet) -> None: ...

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

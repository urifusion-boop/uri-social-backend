"""
Isolation usage cap — VSG-01 v3 §6/§2.8 (SEED-079).

"`requires_isolation` — cap usage across the book before scaling" (§2.8).
§6: "Formats marked requires_isolation (SEED-077, SEED-078, SEED-083)
additionally require the usage cap check." "Across the book" — this caps
usage across every business on the shared/pooled account, not per
business: The Censored Item's own docstring explains why — "withholding
information is a low-quality attribute that reduces distribution... on a
pooled account is everyone's problem." A handful of businesses each using
News Headline/Day 1 → Day 30/The Censored Item is a normal creative mix;
hundreds of them doing it at once reads as a pattern to whatever's
watching distribution quality, and the penalty lands on the whole pool,
not just the businesses that triggered it.

Same architecture as caps.py's spend-cap layers (an ABC store + an
in-memory implementation for pure unit testing + a Mongo implementation
for production) — this is the identical shape of problem (a shared,
book-wide resource with a hard ceiling) applied to format usage instead
of Naira.

**The actual cap number and time window are not specified anywhere in
VSG-01** — §2.8/§6 establish that a cap must exist, not what it is. Both
are constructor parameters with a documented, clearly-labelled default
rather than a value invented here and presented as spec-mandated; setting
the real threshold is a policy decision for whoever owns SEED-079, not
something this module should decide unilaterally.

Not yet wired into a live call path: there is no ad-generation
orchestrator in this codebase yet that would call check()/record() around
an actual render (VSG-01 steps 7-9 predate one — see every format
module's own "not yet wired into a live call path" note). This is the
primitive that step calls before generating with an isolation-capped
format, and after successfully doing so.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import List

from pydantic import BaseModel

from .ad_formats.tokens import AdFormatDef

DEFAULT_CAP = 50
DEFAULT_WINDOW = timedelta(days=30)


class IsolationCapDecision(BaseModel):
    allowed: bool
    reason: str
    current_usage: int
    cap: int


class IsolationUsageStore(ABC):
    @abstractmethod
    async def record_usage(self, format_id: str, business_id: str, occurred_at: datetime) -> None: ...

    @abstractmethod
    async def count_usage(self, format_id: str, since: datetime) -> int: ...


class InMemoryIsolationUsageStore(IsolationUsageStore):
    def __init__(self) -> None:
        self.records: List[dict] = []

    async def record_usage(self, format_id: str, business_id: str, occurred_at: datetime) -> None:
        self.records.append({
            "format_id": format_id, "business_id": business_id, "occurred_at": occurred_at,
        })

    async def count_usage(self, format_id: str, since: datetime) -> int:
        return sum(
            1 for r in self.records
            if r["format_id"] == format_id and r["occurred_at"] >= since
        )


class MongoIsolationUsageStore(IsolationUsageStore):
    """Collection: jane_ads_isolation_usage"""

    def __init__(self, db) -> None:
        self._db = db

    async def ensure_indexes(self) -> None:
        await self._db.jane_ads_isolation_usage.create_index([("format_id", 1), ("occurred_at", -1)])

    async def record_usage(self, format_id: str, business_id: str, occurred_at: datetime) -> None:
        await self._db.jane_ads_isolation_usage.insert_one({
            "format_id": format_id, "business_id": business_id, "occurred_at": occurred_at,
        })

    async def count_usage(self, format_id: str, since: datetime) -> int:
        return await self._db.jane_ads_isolation_usage.count_documents({
            "format_id": format_id, "occurred_at": {"$gte": since},
        })


class IsolationCapService:
    def __init__(
        self,
        store: IsolationUsageStore,
        cap: int = DEFAULT_CAP,
        window: timedelta = DEFAULT_WINDOW,
    ) -> None:
        self._store = store
        self._cap = cap
        self._window = window

    async def check(self, format_def: AdFormatDef) -> IsolationCapDecision:
        """§6's retrieval-time gate: call before generating with a format
        marked requires_isolation. Formats that aren't isolation-capped
        always pass — this check is a no-op for the other 9 formats."""
        if not format_def.requires_isolation:
            return IsolationCapDecision(
                allowed=True, reason="not an isolation-capped format", current_usage=0, cap=self._cap,
            )
        since = datetime.now(timezone.utc) - self._window
        current_usage = await self._store.count_usage(format_def.format_id, since)
        if current_usage >= self._cap:
            return IsolationCapDecision(
                allowed=False,
                reason=(
                    f"isolation cap reached: {format_def.format_id} used {current_usage} "
                    f"times across the book in the last {self._window.days} days (cap {self._cap})"
                ),
                current_usage=current_usage, cap=self._cap,
            )
        return IsolationCapDecision(
            allowed=True,
            reason=f"within isolation cap ({current_usage}/{self._cap})",
            current_usage=current_usage, cap=self._cap,
        )

    async def record(self, format_def: AdFormatDef, business_id: str) -> None:
        """Call after successfully generating with an isolation-capped
        format. A no-op for the other 9 formats — nothing to record."""
        if format_def.requires_isolation:
            await self._store.record_usage(format_def.format_id, business_id, datetime.now(timezone.utc))

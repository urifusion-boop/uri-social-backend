"""
Jane + Ads — instrumentation (PRD §1.8): log every Jane decision and every user
override, so the decision engine can be measured and improved over time.

Two append-only logs, same pattern as WalletStore (split-doc 1.3/1.4):
`InstrumentationStore` is the interface the service talks to, `InMemoryInstrumentationStore`
backs unit tests, `MongoInstrumentationStore` is the production impl. A decision is
logged for every /plan and /understand call (PLAN or ADVISE); an override is logged
only when the caller supplies platforms different from what Jane recommended.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from .models import Goal, Platform, PlanDecision, PlanResult, PurchaseBehaviour


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class DecisionLogEntry(BaseModel):
    """One /plan or /understand call. `jane_platforms` is what the engine recommended;
    `final_platforms` is what actually ran (equal to jane_platforms unless overridden)."""
    log_id: str = Field(default_factory=lambda: _id("dec"))
    business_id: str
    decision: PlanDecision
    goal: Optional[Goal] = None
    behaviour: Optional[PurchaseBehaviour] = None
    jane_platforms: list[Platform] = Field(default_factory=list)
    final_platforms: list[Platform] = Field(default_factory=list)
    overridden: bool = False
    explanation: str = ""
    trace: list[str] = Field(default_factory=list)
    at: datetime = Field(default_factory=_now)


class OverrideLogEntry(BaseModel):
    """A user rejecting Jane's platform recommendation in favour of their own choice."""
    log_id: str = Field(default_factory=lambda: _id("ovr"))
    business_id: str
    jane_platforms: list[Platform]
    user_platforms: list[Platform]
    reason: str = ""
    at: datetime = Field(default_factory=_now)


class InstrumentationStore(ABC):
    @abstractmethod
    async def add_decision(self, entry: DecisionLogEntry) -> None: ...

    @abstractmethod
    async def add_override(self, entry: OverrideLogEntry) -> None: ...

    @abstractmethod
    async def list_decisions(self, business_id: str, limit: int = 100) -> list[DecisionLogEntry]: ...

    @abstractmethod
    async def list_overrides(self, business_id: str, limit: int = 100) -> list[OverrideLogEntry]: ...


class InMemoryInstrumentationStore(InstrumentationStore):
    """List-backed store for tests — no DB."""

    def __init__(self) -> None:
        self._decisions: list[DecisionLogEntry] = []
        self._overrides: list[OverrideLogEntry] = []

    async def add_decision(self, entry: DecisionLogEntry) -> None:
        self._decisions.append(entry.model_copy(deep=True))

    async def add_override(self, entry: OverrideLogEntry) -> None:
        self._overrides.append(entry.model_copy(deep=True))

    async def list_decisions(self, business_id: str, limit: int = 100) -> list[DecisionLogEntry]:
        out = [d for d in self._decisions if d.business_id == business_id]
        return [d.model_copy(deep=True) for d in out[-limit:]]

    async def list_overrides(self, business_id: str, limit: int = 100) -> list[OverrideLogEntry]:
        out = [o for o in self._overrides if o.business_id == business_id]
        return [o.model_copy(deep=True) for o in out[-limit:]]


class MongoInstrumentationStore(InstrumentationStore):
    """Production store. Collections:
      jane_ads_decision_log  — one doc per /plan or /understand call
      jane_ads_override_log  — one doc per user override of Jane's recommendation
    """

    def __init__(self, db) -> None:
        self._db = db

    async def add_decision(self, entry: DecisionLogEntry) -> None:
        await self._db.jane_ads_decision_log.insert_one(entry.model_dump(mode="json"))

    async def add_override(self, entry: OverrideLogEntry) -> None:
        await self._db.jane_ads_override_log.insert_one(entry.model_dump(mode="json"))

    async def list_decisions(self, business_id: str, limit: int = 100) -> list[DecisionLogEntry]:
        docs = await (self._db.jane_ads_decision_log
                      .find({"business_id": business_id}, {"_id": 0})
                      .sort("at", -1).to_list(length=limit))
        return [DecisionLogEntry(**d) for d in docs]

    async def list_overrides(self, business_id: str, limit: int = 100) -> list[OverrideLogEntry]:
        docs = await (self._db.jane_ads_override_log
                      .find({"business_id": business_id}, {"_id": 0})
                      .sort("at", -1).to_list(length=limit))
        return [OverrideLogEntry(**d) for d in docs]


class InstrumentationService:
    def __init__(self, store: InstrumentationStore) -> None:
        self._store = store

    async def record_decision(
        self,
        business_id: str,
        result: PlanResult,
        final_platforms: Optional[list[Platform]] = None,
    ) -> DecisionLogEntry:
        """Log a PLAN or ADVISE result. `final_platforms` differs from Jane's own
        recommendation only when the caller overrode it before this call."""
        if result.decision == PlanDecision.ADVISE:
            entry = DecisionLogEntry(
                business_id=business_id,
                decision=PlanDecision.ADVISE,
                trace=result.advice.trace if result.advice else [],
                explanation=result.advice.reason if result.advice else "",
            )
        else:
            plan = result.plan
            jane_platforms = [p.platform for p in plan.platforms]
            resolved_final = final_platforms if final_platforms is not None else jane_platforms
            entry = DecisionLogEntry(
                business_id=business_id,
                decision=PlanDecision.PLAN,
                goal=plan.goal,
                behaviour=plan.behaviour,
                jane_platforms=jane_platforms,
                final_platforms=resolved_final,
                overridden=resolved_final != jane_platforms,
                explanation=plan.explanation,
                trace=plan.trace,
            )
        await self._store.add_decision(entry)
        return entry

    async def record_override(
        self,
        business_id: str,
        jane_platforms: list[Platform],
        user_platforms: list[Platform],
        reason: str = "",
    ) -> OverrideLogEntry:
        entry = OverrideLogEntry(
            business_id=business_id,
            jane_platforms=jane_platforms,
            user_platforms=user_platforms,
            reason=reason,
        )
        await self._store.add_override(entry)
        return entry

    async def decisions_for(self, business_id: str, limit: int = 100) -> list[DecisionLogEntry]:
        return await self._store.list_decisions(business_id, limit)

    async def overrides_for(self, business_id: str, limit: int = 100) -> list[OverrideLogEntry]:
        return await self._store.list_overrides(business_id, limit)

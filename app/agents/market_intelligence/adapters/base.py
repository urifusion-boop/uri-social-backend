"""
Uri Market Intelligence — the source adapter interface (PRD §17).

Collection code talks ONLY to this interface. MockSourceAdapter (this package)
lets the whole classify → score → cluster → insight pipeline be built and tested
today; a real Apify-backed adapter (e.g. XSourceAdapter) implements the same
methods later and drops in with no change to the scan orchestration code.

Every method reports unsupported capabilities explicitly rather than raising —
PRD §8: "Unsupported controls must be disabled with a useful explanation," never
a silent no-op that looks like zero results.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from ..models import RawEvidence, ScanStatus


class AdapterCapabilities(BaseModel):
    """PRD §8 capability-gate record, trimmed to what's checked before a scan
    starts. `provider`/`platform` name which adapter this describes; everything
    else is what the setup UI needs to show real limits instead of a platform
    logo standing in for a coverage promise."""
    provider: str
    platform: str
    verified_lookback_days: int
    supports_date_filters: bool
    supports_keyword_search: bool
    accessible_languages: list[str]
    refresh_cadence_hours: int
    notes: str = ""  # human-readable caveats shown in the setup UI


class CollectionPage(BaseModel):
    """One page of adapter output. `has_more` + `next_cursor` let the caller
    paginate; `gaps` carries any partial-coverage note for this page specifically
    (PRD §18: "retain the gap and retry rather than silently marking the period
    complete")."""
    evidence: list[RawEvidence]
    has_more: bool = False
    next_cursor: Optional[str] = None
    gaps: list[str] = []


class SourceAdapter(ABC):
    """One adapter per provider+platform pair (X, Instagram, news, or a mock)."""

    @abstractmethod
    def capabilities(self) -> AdapterCapabilities:
        """Static, synchronous — no network call. Used to render setup-UI limits
        and to gate a scan before any cost is incurred."""
        ...

    @abstractmethod
    async def estimate_cost(self, keywords: list[str], days: int) -> float:
        """Best-effort USD estimate for a scan of this shape. Used for the PRD
        §23 budget reservation check before enqueueing."""
        ...

    @abstractmethod
    async def start_collection(
        self, keywords: list[str], excluded_keywords: list[str], since: datetime, until: datetime
    ) -> str:
        """Kick off collection for the given window. Returns a provider-run id
        the caller persists before ever fetching results (PRD §17: 'save the
        provider run ID before fetching results')."""
        ...

    @abstractmethod
    async def get_status(self, run_id: str) -> ScanStatus:
        ...

    @abstractmethod
    async def fetch_page(self, run_id: str, cursor: Optional[str] = None) -> CollectionPage:
        ...

    @abstractmethod
    async def cancel_if_supported(self, run_id: str) -> bool:
        """Returns False (not True-with-no-effect) when the provider has no
        cancel capability — callers must not assume cancellation happened."""
        ...

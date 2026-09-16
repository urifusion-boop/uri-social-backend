"""
Uri Market Intelligence — mock source adapter.

Deterministic fixture data so the classify → score → cluster → insight pipeline
runs and is tested end-to-end WITHOUT any live provider or API key. No
`datetime.utcnow()` baked into stored timestamps at import time — offsets are
computed from `start_time` (or now, if unset) so the same fixture always sits
inside "latest complete 7 days" no matter when a test runs.

The fixture is deliberately shaped to cross PRD §12's real eligibility bars, not
just to "have some data": 6 concern posts from 6 distinct authors across 3
distinct threads (clears the "5 independent accounts across 3 threads" concern
gate), 1 fresh purchase inquiry (clears the 48h freshness gate) plus 1 stale one
(to prove the freshness filter actually excludes something), and 2 noise/
promotional posts (to prove clustering/classification don't just wave everything
through).

A real adapter (e.g. XSourceAdapter, Apify-backed) implements the exact same
`SourceAdapter` methods; nothing above the adapter boundary changes when one is
swapped in.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from .base import AdapterCapabilities, CollectionPage, SourceAdapter
from ..models import Geography, RawEvidence, ScanStatus


class MockSourceAdapter(SourceAdapter):
    def __init__(self, start_time: Optional[datetime] = None) -> None:
        # Fixed reference clock so "how many days ago" is deterministic per
        # instance, without hardcoding an absolute date that goes stale.
        self._now = start_time or datetime.now(timezone.utc)
        self._runs: dict[str, dict] = {}

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            provider="mock",
            platform="mock",
            verified_lookback_days=90,
            supports_date_filters=True,
            supports_keyword_search=True,
            accessible_languages=["en"],
            refresh_cadence_hours=1,
            notes="Fixture data for development/testing — not a real source.",
        )

    async def estimate_cost(self, keywords: list[str], days: int) -> float:
        return 0.0

    def _ago(self, **kwargs) -> datetime:
        return self._now - timedelta(**kwargs)

    def _fixture_evidence(self) -> list[RawEvidence]:
        def ev(
            source_id: str,
            text: str,
            author: str,
            parent_id: Optional[str],
            published_at: datetime,
            location: Optional[str] = None,
        ) -> RawEvidence:
            return RawEvidence(
                provider="mock",
                platform="mock",
                source_id=source_id,
                url=f"https://example.com/posts/{source_id}",
                content_type="post",
                parent_id=parent_id,
                text=text,
                language="en",
                author_handle=author,
                published_at=published_at,
                collected_at=self._now,
                geography=Geography(explicit_location=location),
                raw_metrics={"likes": 3, "replies": 1},
            )

        thread_a, thread_b, thread_c = "thread_a", "thread_b", "thread_c"

        concerns = [
            ev("c1", "Delivery to Lekki has been taking over a week, is anyone else experiencing this?", "@amaka_o", thread_a, self._ago(days=1), "Lekki, Lagos"),
            ev("c2", "Same here, my order to Lekki has been delayed for 6 days now.", "@tobi_fashion", thread_a, self._ago(days=1, hours=2)),
            ev("c3", "Why does delivery to Lekki take so long compared to Ikeja?", "@chidinma_b", thread_b, self._ago(days=2)),
            ev("c4", "Delivery delays to Lekki are getting worse this month.", "@femi_lagos", thread_b, self._ago(days=2, hours=5)),
            ev("c5", "Anyone know a faster alternative for Lekki deliveries?", "@ngozi_k", thread_c, self._ago(days=3)),
            ev("c6", "I waited 8 days for a delivery to Lekki, that's too long.", "@dami_ade", thread_c, self._ago(days=3, hours=3)),
        ]

        inquiries = [
            ev("i1", "Does anyone sell ready-to-wear ankara dresses that deliver same-day in Lagos? Need one by Friday.", "@zainab_style", None, self._ago(hours=6)),
            ev("i2", "Looking for a caterer for an event that already happened last month.", "@old_request", None, self._ago(days=90)),  # stale — must NOT qualify as active inquiry
        ]

        noise = [
            ev("n1", "FOLLOW FOR FOLLOW check my page for the best deals!!! #promo #follow", "@spamacct1", None, self._ago(hours=4)),
            ev("n2", "Apple just announced a new iPhone event next week.", "@technews_bot", None, self._ago(hours=8)),
        ]

        return concerns + inquiries + noise

    async def start_collection(
        self, keywords: list[str], excluded_keywords: list[str], since: datetime, until: datetime
    ) -> str:
        run_id = f"mockrun_{uuid.uuid4().hex[:8]}"
        self._runs[run_id] = {"status": ScanStatus.COMPLETED, "fetched": False}
        return run_id

    async def get_status(self, run_id: str) -> ScanStatus:
        return self._runs.get(run_id, {}).get("status", ScanStatus.FAILED)

    async def fetch_page(self, run_id: str, cursor: Optional[str] = None) -> CollectionPage:
        run = self._runs.get(run_id)
        if run is None:
            return CollectionPage(evidence=[], has_more=False, gaps=["unknown run_id"])
        # Single-page fixture — real adapters paginate, this doesn't need to.
        return CollectionPage(evidence=self._fixture_evidence(), has_more=False)

    async def cancel_if_supported(self, run_id: str) -> bool:
        return False

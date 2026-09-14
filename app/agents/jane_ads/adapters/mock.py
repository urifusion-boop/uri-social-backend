"""
Jane + Ads — mock ad-platform adapter.

Deterministic simulation of a platform so Shore's brain (decision engine → wallet →
caps → conversation flow) runs and is tested end-to-end WITHOUT any live platform.
No randomness — same inputs always yield the same events, so tests are stable.

Ibukun's real Meta adapter implements `AdPlatformAdapter` with the same signatures;
swapping mock → real requires no change to Shore's code.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .base import AdPlatformAdapter
from ..models import (
    CampaignPlan,
    ConversationDelivered,
    LaunchResult,
    PerAdSpend,
    Platform,
    SpendAuthorization,
)


class MockAdPlatformAdapter(AdPlatformAdapter):
    """A fake platform that 'spends' and 'delivers conversations' on a fixed schedule.

    `conversation_cost_ngn` sets the simulated cost per delivered conversation; the
    number of conversations a campaign yields is `floor(budget / cost)` per ad — so a
    caller can predict outcomes exactly in tests.
    """

    def __init__(
        self,
        conversation_cost_ngn: float = 500.0,
        start_time: datetime | None = None,
    ) -> None:
        self.conversation_cost_ngn = conversation_cost_ngn
        # Fixed clock so event timestamps are deterministic (no datetime.now()).
        self._t0 = start_time or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._campaigns: dict[str, dict] = {}
        self._paused: set[tuple[str, str]] = set()
        self._seq = 0

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq:04d}"

    async def launch_campaign(
        self, plan: CampaignPlan, auth: SpendAuthorization
    ) -> LaunchResult:
        campaign_id = self._next_id("mockcmp")
        primary = plan.platforms[0].platform if plan.platforms else Platform.META
        # One ad for this business (pooling adds more ads to the same campaign later).
        ad_id = self._next_id("mockad")
        self._campaigns[campaign_id] = {
            "business_id": plan.business_id,
            "platform": primary,
            "ad_id": ad_id,
            "budget": min(
                sum(p.budget_ngn for p in plan.platforms),
                auth.funded_amount_ngn,
            ),
        }
        return LaunchResult(
            campaign_id=campaign_id,
            ad_ids={plan.business_id: ad_id},
            platforms=[p.platform for p in plan.platforms],
        )

    def _conversation_count(self, campaign_id: str) -> int:
        c = self._campaigns[campaign_id]
        if (campaign_id, c["ad_id"]) in self._paused:
            return 0
        return int(c["budget"] // self.conversation_cost_ngn)

    async def poll_conversations(self, campaign_id: str) -> list[ConversationDelivered]:
        c = self._campaigns[campaign_id]
        out: list[ConversationDelivered] = []
        for i in range(self._conversation_count(campaign_id)):
            out.append(
                ConversationDelivered(
                    business_id=c["business_id"],
                    ad_id=c["ad_id"],
                    campaign_id=campaign_id,
                    platform=c["platform"],
                    at=self._t0 + timedelta(minutes=i),
                    charge_ngn=self.conversation_cost_ngn,
                )
            )
        return out

    async def fetch_per_ad_spend(self, campaign_id: str) -> list[PerAdSpend]:
        c = self._campaigns[campaign_id]
        spend = self._conversation_count(campaign_id) * self.conversation_cost_ngn
        return [
            PerAdSpend(
                business_id=c["business_id"],
                ad_id=c["ad_id"],
                campaign_id=campaign_id,
                platform=c["platform"],
                spend_ngn=spend,
                at=self._t0,
            )
        ]

    async def pause_ad(self, campaign_id: str, ad_id: str) -> bool:
        self._paused.add((campaign_id, ad_id))
        return True

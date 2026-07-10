"""
Jane + Ads — the ad-platform adapter interface.

Shore's side talks ONLY to this interface. The mock adapter (this package) lets the
whole brain run in tests today; Ibukun's real Meta adapter implements the same
methods later and drops in with no change to Shore's code.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import (
    CampaignPlan,
    ConversationDelivered,
    LaunchResult,
    PerAdSpend,
    SpendAuthorization,
)


class AdPlatformAdapter(ABC):
    """One adapter per platform family (Meta, Google, TikTok) — or a mock."""

    @abstractmethod
    async def launch_campaign(
        self, plan: CampaignPlan, auth: SpendAuthorization
    ) -> LaunchResult:
        """Create the campaign/ad-set/ads for `plan`, respecting `auth` caps.
        Returns the platform ids so Shore can map ad_id → business."""
        ...

    @abstractmethod
    async def fetch_per_ad_spend(self, campaign_id: str) -> list[PerAdSpend]:
        """Current cumulative spend per ad — feeds cap checks / the fairness loop."""
        ...

    @abstractmethod
    async def poll_conversations(self, campaign_id: str) -> list[ConversationDelivered]:
        """Conversations delivered since the last poll. (Real Meta uses a webhook;
        the mock simulates. Either way Shore consumes the same event type.)"""
        ...

    @abstractmethod
    async def pause_ad(self, campaign_id: str, ad_id: str) -> bool:
        """Pause a single ad — used by the fairness/cap enforcement to stop a runaway."""
        ...

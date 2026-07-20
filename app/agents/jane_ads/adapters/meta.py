"""
Jane + Ads — the real Meta Marketing API adapter (engineering work-split item 2.2,
"Meta Marketing API integration"). Implements AdPlatformAdapter against Meta's live
Graph API — creating an actual campaign -> ad set -> ad, reading real per-ad spend,
and pausing runaway ads. Drop-in replacement for MockAdPlatformAdapter; the decision
engine, wallet, caps, and fairness engine need no changes to use this instead.

Verified end-to-end against a real Ad Account (PAUSED, zero-spend — no payment method
was attached when the campaign/ad-set/ad chain was first proven out, so nothing could
actually deliver): campaign -> ad set -> ad creative -> ad all created successfully,
plus fetch_per_ad_spend/pause_ad against the live objects. Several real, Meta-specific
requirements only surfaced this way (not guessable from docs alone) and are now
encoded here:
  - Campaign creation requires `is_adset_budget_sharing_enabled` explicitly (we set it
    False — budget lives on the ad set, isolated per business via caps.py/fairness.py,
    not shared automatically across ad sets by Meta).
  - Ad set creation requires an explicit `bid_strategy` (LOWEST_COST_WITHOUT_CAP).
  - Meta enforces its own per-currency daily-budget floor (not pre-validated here —
    a too-small budget comes back as a descriptive API error, not a silent failure).
  - Ad creation requires REAL media — a text-only link creative is rejected ("Please
    specify the media to run with this ad"). CampaignPlan.creative (an AdCreative from
    creative.py) is required for this reason; there's no text-only fallback.

New campaigns/ad sets/ads are created PAUSED, not ACTIVE — a deliberate extra safety
margin for the first real integration; a human should review and activate in Ads
Manager rather than this code going live the instant it runs. Real-time, per-
conversation delivery is webhook-based on Meta's side (work-split item 2.4 — the
WhatsApp Cloud API migration, separate and not yet built); poll_conversations() here
falls back to Meta's own aggregate "messaging_conversation_started" insight metric,
returning only the DELTA since the last poll (tracked per campaign in Mongo) so
callers never double-charge a conversation that was already reported.

Video creatives aren't supported yet (Meta needs a video_data creative shape via
/advideos, not the link_data+picture shape used here) — launch_campaign raises
NotImplementedError for plan.creative.is_video rather than silently building a broken
payload.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from .base import AdPlatformAdapter
from ..models import (
    CampaignPlan,
    ConversationDelivered,
    LaunchResult,
    PerAdSpend,
    Platform,
    SpendAuthorization,
)

COLLECTION = "jane_ads_meta_campaigns"

# Standard Meta insight action type for Click-to-WhatsApp conversation starts.
_CONVERSATION_ACTION_TYPE = "onsite_conversion.messaging_conversation_started_7d"


class MetaAPIError(Exception):
    """A Graph API call returned an error payload, or the adapter is misconfigured."""


def _raise_for_error(data: dict, context: str) -> None:
    if "error" in data:
        err = data["error"]
        raise MetaAPIError(
            f"{context}: {err.get('message')} (code={err.get('code')}, "
            f"subcode={err.get('error_subcode')})"
        )


class MetaAdPlatformAdapter(AdPlatformAdapter):
    """One instance per request/job. Backed by Mongo so the campaign_id ->
    business_id/ad_id mapping (and the conversation-count watermark) survives
    restarts — unlike the mock's in-memory dict, this adapter's state must."""

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        ad_account_id: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> None:
        self._db = db
        self._ad_account_id = ad_account_id or settings.META_AD_ACCOUNT_ID
        self._access_token = access_token or settings.META_SYSTEM_TOKEN
        self._graph_base = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"
        if not self._ad_account_id:
            raise MetaAPIError("META_AD_ACCOUNT_ID is not configured")
        if not self._access_token:
            raise MetaAPIError("META_SYSTEM_TOKEN is not configured")

    async def launch_campaign(
        self, plan: CampaignPlan, auth: SpendAuthorization
    ) -> LaunchResult:
        meta_plans = [p for p in plan.platforms if p.platform == Platform.META]
        if not meta_plans:
            raise ValueError("MetaAdPlatformAdapter only handles Platform.META plans")
        if not plan.page_id:
            raise ValueError("CampaignPlan.page_id is required to launch a real Meta campaign")
        if not plan.creative or not plan.creative.image_url:
            # Confirmed live: Meta rejects link-ad creation with no real media
            # attached ("Please specify the media to run with this ad"), so this
            # can't fall back to a text-only placeholder.
            raise ValueError("CampaignPlan.creative.image_url is required to launch a real Meta campaign")
        if plan.creative.is_video:
            raise NotImplementedError(
                "Video creatives need Meta's video_data creative shape (upload via "
                "/advideos, reference the resulting video_id) — not yet implemented; "
                "only image creatives (link_data + picture) are supported."
            )
        platform_plan = meta_plans[0]

        total_budget_ngn = min(platform_plan.budget_ngn, auth.funded_amount_ngn)
        days = max(platform_plan.days, 1)
        # Meta wants budgets in the account currency's minor unit (kobo for NGN).
        # Meta also enforces its own daily-budget floor per currency; a too-small
        # budget will come back as a descriptive error from the API, not a silent
        # failure, so this is intentionally not pre-validated here.
        daily_budget_minor = max(int((total_budget_ngn / days) * 100), 100)

        async with httpx.AsyncClient(timeout=30) as client:
            campaign_resp = await client.post(
                f"{self._graph_base}/act_{self._ad_account_id}/campaigns",
                params={"access_token": self._access_token},
                json={
                    "name": f"JaneAds-{plan.business_id}-{plan.goal.value}",
                    "objective": "OUTCOME_ENGAGEMENT",
                    "status": "PAUSED",
                    "special_ad_categories": [],
                    # Budget lives on the ad set (per-business isolation via caps.py/
                    # fairness.py), not shared automatically across ad sets by Meta.
                    "is_adset_budget_sharing_enabled": False,
                },
            )
            campaign_data = campaign_resp.json()
            _raise_for_error(campaign_data, "campaign creation")
            campaign_id = campaign_data["id"]

            adset_resp = await client.post(
                f"{self._graph_base}/act_{self._ad_account_id}/adsets",
                params={"access_token": self._access_token},
                json={
                    "name": f"JaneAds-{plan.business_id}-adset",
                    "campaign_id": campaign_id,
                    "daily_budget": daily_budget_minor,
                    "billing_event": "IMPRESSIONS",
                    "optimization_goal": "CONVERSATIONS",
                    "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                    "destination_type": "WHATSAPP",
                    "promoted_object": {"page_id": plan.page_id},
                    "targeting": {"geo_locations": {"countries": ["NG"]}},
                    "status": "PAUSED",
                },
            )
            adset_data = adset_resp.json()
            _raise_for_error(adset_data, "ad set creation")
            adset_id = adset_data["id"]

            creative_resp = await client.post(
                f"{self._graph_base}/act_{self._ad_account_id}/adcreatives",
                params={"access_token": self._access_token},
                json={
                    "name": f"JaneAds-{plan.business_id}-creative",
                    "object_story_spec": {
                        "page_id": plan.page_id,
                        "link_data": {
                            "message": plan.creative.primary_text or plan.creative.headline or "Chat with us on WhatsApp!",
                            "link": "https://wa.me/",
                            "picture": plan.creative.image_url,
                            "call_to_action": {"type": "WHATSAPP_MESSAGE"},
                        },
                    },
                },
            )
            creative_data = creative_resp.json()
            _raise_for_error(creative_data, "ad creative creation")
            creative_id = creative_data["id"]

            ad_resp = await client.post(
                f"{self._graph_base}/act_{self._ad_account_id}/ads",
                params={"access_token": self._access_token},
                json={
                    "name": f"JaneAds-{plan.business_id}-ad",
                    "adset_id": adset_id,
                    "creative": {"creative_id": creative_id},
                    "status": "PAUSED",
                },
            )
            ad_data = ad_resp.json()
            _raise_for_error(ad_data, "ad creation")
            ad_id = ad_data["id"]

        await self._db[COLLECTION].update_one(
            {"campaign_id": campaign_id},
            {"$set": {
                "campaign_id": campaign_id,
                "adset_id": adset_id,
                "ad_id": ad_id,
                "business_id": plan.business_id,
                "platform": "meta",
                "last_conversation_count": 0,
                "created_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )

        return LaunchResult(
            campaign_id=campaign_id,
            ad_ids={plan.business_id: ad_id},
            platforms=[Platform.META],
            launched=True,
        )

    async def _get_campaign_record(self, campaign_id: str) -> dict:
        record = await self._db[COLLECTION].find_one({"campaign_id": campaign_id})
        if not record:
            raise MetaAPIError(
                f"No stored record for campaign_id={campaign_id} — was it launched via this adapter?"
            )
        return record

    async def fetch_per_ad_spend(self, campaign_id: str) -> list[PerAdSpend]:
        """Current CUMULATIVE spend per ad (matches the interface contract) — feeds
        cap checks / the fairness loop, which already expect a running total."""
        record = await self._get_campaign_record(campaign_id)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._graph_base}/{campaign_id}/insights",
                params={
                    "access_token": self._access_token,
                    "level": "ad",
                    "fields": "ad_id,spend",
                },
            )
            data = resp.json()
            _raise_for_error(data, "insights fetch")

        now = datetime.now(timezone.utc)
        rows = data.get("data", [])
        if not rows:
            # No delivery yet (e.g. still PAUSED, or too new for insights to report).
            return [PerAdSpend(
                business_id=record["business_id"], ad_id=record["ad_id"],
                campaign_id=campaign_id, platform=Platform.META, spend_ngn=0.0, at=now,
            )]
        return [
            PerAdSpend(
                business_id=record["business_id"],
                ad_id=row.get("ad_id", record["ad_id"]),
                campaign_id=campaign_id,
                platform=Platform.META,
                spend_ngn=float(row.get("spend", 0.0)),
                at=now,
            )
            for row in rows
        ]

    async def poll_conversations(self, campaign_id: str) -> list[ConversationDelivered]:
        """Conversations delivered SINCE THE LAST POLL. No live webhook yet (that's
        work-split item 2.4, a separate WhatsApp Cloud API migration) — falls back to
        Meta's own aggregate conversation-started metric, returning only the growth
        since the last call (tracked via last_conversation_count in Mongo) so a
        caller charging the wallet per event never double-charges an already-seen
        conversation. Gives a correct count and total cost, not individual
        per-conversation timestamps."""
        record = await self._get_campaign_record(campaign_id)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._graph_base}/{campaign_id}/insights",
                params={
                    "access_token": self._access_token,
                    "fields": "spend,actions",
                },
            )
            data = resp.json()
            _raise_for_error(data, "insights fetch (conversations)")

        rows = data.get("data", [])
        if not rows:
            return []
        row = rows[0]
        spend = float(row.get("spend", 0.0))
        total_conversations = 0
        for action in row.get("actions", []):
            if action.get("action_type") == _CONVERSATION_ACTION_TYPE:
                total_conversations = int(float(action.get("value", 0)))
                break

        already_seen = int(record.get("last_conversation_count", 0))
        new_count = max(total_conversations - already_seen, 0)
        if new_count == 0:
            return []

        await self._db[COLLECTION].update_one(
            {"campaign_id": campaign_id},
            {"$set": {"last_conversation_count": total_conversations}},
        )

        cost_per_conversation = (spend / total_conversations) if total_conversations else 0.0
        now = datetime.now(timezone.utc)
        return [
            ConversationDelivered(
                business_id=record["business_id"],
                ad_id=record["ad_id"],
                campaign_id=campaign_id,
                platform=Platform.META,
                at=now,
                charge_ngn=cost_per_conversation,
            )
            for _ in range(new_count)
        ]

    async def pause_ad(self, campaign_id: str, ad_id: str) -> bool:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._graph_base}/{ad_id}",
                params={"access_token": self._access_token},
                json={"status": "PAUSED"},
            )
            data = resp.json()
            _raise_for_error(data, "pause ad")
        return bool(data.get("success"))

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

Video creatives (campaign roadmap Tier 3b) upload the hosted video to Meta via
`/advideos` (file_url — Meta fetches it directly, no re-streaming needed here),
poll until processing finishes, fetch a thumbnail, then build the ad creative's
`video_data` shape instead of `link_data`+picture. Verified live end-to-end
against the real Ad Account with a real (tiny, generated) test clip.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from .base import AdPlatformAdapter
from ..models import (
    CampaignPlan,
    ConversationDelivered,
    Goal,
    LaunchResult,
    PerAdSpend,
    Platform,
    SpendAuthorization,
)

COLLECTION = "jane_ads_meta_campaigns"

# Standard Meta insight action type for Click-to-WhatsApp conversation starts.
_CONVERSATION_ACTION_TYPE = "onsite_conversion.messaging_conversation_started_7d"

# Meta processes uploaded video asynchronously (verified live: a short vertical
# clip is typically ready in well under a minute) — poll rather than assume.
_VIDEO_POLL_ATTEMPTS = 20
_VIDEO_POLL_INTERVAL_SECONDS = 5

# Meta's `effective_status` values, translated to plain language for the campaign-
# list view — callers never see raw Ads-Manager jargon.
_DELIVERY_LABELS = {
    "ACTIVE": "Active",
    "PAUSED": "Paused",
    "CAMPAIGN_PAUSED": "Paused",
    "ADSET_PAUSED": "Paused",
    "PENDING_REVIEW": "In review",
    "DISAPPROVED": "Needs changes",
    "PREAPPROVED": "Scheduled",
    "PENDING_BILLING_INFO": "Needs billing info",
    "ARCHIVED": "Archived",
    "DELETED": "Deleted",
    "IN_PROCESS": "Processing",
    "WITH_ISSUES": "Needs attention",
}


class MetaAPIError(Exception):
    """A Graph API call returned an error payload, or the adapter is misconfigured."""

    def __init__(self, message: str, code: Optional[int] = None, subcode: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code
        self.subcode = subcode

    # Meta's ad-account-level throttle ("There have been too many calls to this
    # ad-account") — code 80004 is the specific ads-management rate limit; codes
    # 4/17/32/613 are the general API-wide rate limits Meta also uses. Distinct
    # from a real failure: it's temporary and clears on its own, so callers
    # should tell the user to wait rather than treating it as a hard error.
    _RATE_LIMIT_CODES = {80004, 4, 17, 32, 613}

    @property
    def is_rate_limited(self) -> bool:
        return self.code in self._RATE_LIMIT_CODES


def _raise_for_error(data: dict, context: str) -> None:
    if "error" in data:
        err = data["error"]
        # Meta's `error_user_msg`/`error_user_title` are the human-readable, actionable
        # reason (e.g. "Budget is too low — Your ad set budget must be more than
        # NGN1,400.00"); the generic `message` is usually just "Invalid parameter".
        # Prefer the human copy so the reason reaches the user instead of a code.
        user_title = err.get("error_user_title")
        user_msg = err.get("error_user_msg")
        human = " — ".join(p for p in (user_title, user_msg) if p)
        detail = human or err.get("message") or "unknown error"
        raise MetaAPIError(
            f"{context}: {detail} (code={err.get('code')}, subcode={err.get('error_subcode')})",
            code=err.get("code"),
            subcode=err.get("error_subcode"),
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

    async def _upload_video_to_meta(self, video_url: str) -> tuple[str, str]:
        """Upload an already-hosted video (Cloudinary) to Meta via `file_url` (Meta
        fetches it directly — no need to re-stream the bytes ourselves), poll until
        processing finishes, and grab a thumbnail for the ad creative's required
        `image_url`. Returns (video_id, thumbnail_url). Raises MetaAPIError if
        processing fails, produces no thumbnail, or doesn't finish in time."""
        async with httpx.AsyncClient(timeout=60) as client:
            upload_resp = await client.post(
                f"{self._graph_base}/act_{self._ad_account_id}/advideos",
                params={"access_token": self._access_token},
                data={"file_url": video_url, "name": f"JaneAds-video-{uuid.uuid4().hex[:8]}"},
            )
            upload_data = upload_resp.json()
            _raise_for_error(upload_data, "video upload")
            video_id = upload_data["id"]

            for _ in range(_VIDEO_POLL_ATTEMPTS):
                await asyncio.sleep(_VIDEO_POLL_INTERVAL_SECONDS)
                status_resp = await client.get(
                    f"{self._graph_base}/{video_id}",
                    params={"access_token": self._access_token, "fields": "status"},
                )
                status_data = status_resp.json()
                _raise_for_error(status_data, "video status fetch")
                video_status = status_data.get("status", {}).get("video_status")
                if video_status == "ready":
                    break
                if video_status == "error":
                    raise MetaAPIError(f"Meta failed to process the uploaded video ({video_id})")
            else:
                raise MetaAPIError(f"Video {video_id} did not finish processing in time")

            thumb_resp = await client.get(
                f"{self._graph_base}/{video_id}/thumbnails",
                params={"access_token": self._access_token},
            )
            thumb_data = thumb_resp.json()
            _raise_for_error(thumb_data, "video thumbnail fetch")
            thumbs = thumb_data.get("data", [])
            if not thumbs:
                raise MetaAPIError(f"No thumbnail available for video {video_id}")
            thumbnail_url = next((t["uri"] for t in thumbs if t.get("is_preferred")), thumbs[0]["uri"])

        return video_id, thumbnail_url

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
        # A followers/engagement campaign never routes anywhere via WhatsApp — every
        # other goal does. Without the brand's number there's nowhere for a
        # Click-to-WhatsApp ad to route to. Blocked here too (the router's
        # ads_connection gate checks first) so a missing number can never silently
        # ship a dead-end ad.
        is_followers_goal = plan.goal == Goal.FOLLOWERS
        if not is_followers_goal and not plan.whatsapp_number:
            raise ValueError("CampaignPlan.whatsapp_number is required to launch a Click-to-WhatsApp campaign")
        # The real ad destination for every non-followers goal: a chat with the brand's
        # own number. normalize_wa_number (whatsapp.py) already stored it digits-only
        # and country-coded, which is exactly the form wa.me expects.
        wa_link = f"https://wa.me/{plan.whatsapp_number}" if plan.whatsapp_number else ""
        platform_plan = meta_plans[0]

        total_budget_ngn = min(platform_plan.budget_ngn, auth.funded_amount_ngn)
        days = max(platform_plan.days, 1)
        # Meta wants budgets in the account currency's minor unit (kobo for NGN).
        # Meta also enforces its own daily-budget floor per currency; a too-small
        # budget will come back as a descriptive error from the API, not a silent
        # failure, so this is intentionally not pre-validated here.
        daily_budget_minor = max(int((total_budget_ngn / days) * 100), 100)
        # A real end date, not just a `days` number in copy — without this the ad
        # set has no natural stop and would run (and spend) indefinitely once
        # activated. Measured from creation, not activation: if a campaign sits
        # PAUSED for a while before someone activates it, its window is simply
        # shorter (or already elapsed, in which case it safely won't deliver) —
        # that's a minor UX gap, not a spend-safety one, since PAUSED never spends.
        start_time = datetime.now(timezone.utc)
        end_time = start_time + timedelta(days=days)

        # Video needs its own upload + processing step BEFORE anything is created
        # on Meta — if the video fails to process, nothing (campaign/ad set) gets
        # created only to be orphaned.
        video_id: Optional[str] = None
        video_thumbnail_url: Optional[str] = None
        if plan.creative.is_video:
            video_id, video_thumbnail_url = await self._upload_video_to_meta(plan.creative.image_url)

        # Meta has no transaction across these four creates. Live-confirmed: a launch
        # that failed at the ad-set step (Meta rejecting an unlinked WhatsApp number)
        # left the already-created campaign behind on the ad account with NO row in
        # our DB — invisible in 'My Campaigns' and unmanageable from the app. Six such
        # orphans were found on the real account. Track what we created and undo it on
        # any failure, so a partial launch never leaves anything behind.
        campaign_id = ""
        adset_id = ""
        creative_id = ""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                campaign_resp = await client.post(
                    f"{self._graph_base}/act_{self._ad_account_id}/campaigns",
                    params={"access_token": self._access_token},
                    json={
                        "name": f"JaneAds-{plan.business_id}-{plan.goal.value}",
                        # A followers campaign is real Page engagement; every other goal
                        # sends the tap to a wa.me link, which is traffic off-platform —
                        # OUTCOME_TRAFFIC is the objective that allows LINK_CLICKS
                        # optimisation (confirmed live against the real ad account).
                        "objective": "OUTCOME_ENGAGEMENT" if is_followers_goal else "OUTCOME_TRAFFIC",
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

                adset_payload = {
                    "name": f"JaneAds-{plan.business_id}-adset",
                    "campaign_id": campaign_id,
                    "daily_budget": daily_budget_minor,
                    "billing_event": "IMPRESSIONS",
                    "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
                    "targeting": {"geo_locations": {"countries": ["NG"]}},
                    "status": "PAUSED",
                    "start_time": start_time.isoformat(),
                    "end_time": end_time.isoformat(),
                }
                if is_followers_goal:
                    # Grow the Page's own following — confirmed live shape (Meta Marketing
                    # API docs): same OUTCOME_ENGAGEMENT campaign objective as a Click-to-
                    # WhatsApp ad, but POST_ENGAGEMENT optimization and no WhatsApp routing
                    # at all (no destination_type, no promoted_object.whatsapp_phone_number).
                    adset_payload["optimization_goal"] = "POST_ENGAGEMENT"
                else:
                    # Optimize for started WhatsApp conversations, routed to the brand's
                    # own number. `promoted_object.whatsapp_phone_number` is confirmed live:
                    # Meta validates it's actually linked to this page/account and rejects
                    # with a specific, clear error if not — a free pre-flight check.
                    # wa.me LINK ad, not Meta's native Click-to-WhatsApp. The native
                    # form (destination_type=WHATSAPP + promoted_object.
                    # whatsapp_phone_number) requires that exact number to be linked to
                    # the Page inside Meta — a manual, per-number step with no partner
                    # API — and it rejected every launch with "This WhatsApp phone
                    # number is not linked to your account" (code=100, subcode=1487246),
                    # live-confirmed on a real brand number. Since every brand now runs
                    # from URI's ONE shared Page, that would mean hand-linking each
                    # client's number to it, which does not scale.
                    #
                    # A plain https://wa.me/<number> link needs no linking at all and is
                    # what whatsapp.py's own design already assumed ("no per-brand
                    # Facebook Page required, just their number"). Optimising for
                    # LINK_CLICKS is the matching goal — CONVERSATIONS only counts
                    # native WhatsApp threads Meta can see.
                    adset_payload["optimization_goal"] = "LINK_CLICKS"
                adset_resp = await client.post(
                    f"{self._graph_base}/act_{self._ad_account_id}/adsets",
                    params={"access_token": self._access_token},
                    json=adset_payload,
                )
                adset_data = adset_resp.json()
                _raise_for_error(adset_data, "ad set creation")
                adset_id = adset_data["id"]

                if is_followers_goal:
                    message = plan.creative.primary_text or plan.creative.headline or "Follow us for more!"
                    # LIKE_PAGE drives the tap straight to following the Page — confirmed
                    # live shape (Meta Marketing API docs), no link/WhatsApp routing at all.
                    cta = {"type": "LIKE_PAGE", "value": {"page": plan.page_id}}
                else:
                    message = plan.creative.primary_text or plan.creative.headline or "Chat with us on WhatsApp!"
                    # The tap opens a WhatsApp chat with the brand's OWN number through a
                    # plain wa.me link (see the ad set above for why this is a link ad and
                    # not Meta's native Click-to-WhatsApp). A video creative carries no
                    # link_data.link of its own, so the destination has to ride on the CTA.
                    cta = {"type": "WHATSAPP_MESSAGE"}
                    if plan.creative.is_video and wa_link:
                        cta = {"type": "WHATSAPP_MESSAGE", "value": {"link": wa_link}}
                if plan.creative.is_video:
                    object_story_spec = {
                        "page_id": plan.page_id,
                        "video_data": {
                            "video_id": video_id,
                            "image_url": video_thumbnail_url,
                            "message": message,
                            "call_to_action": cta,
                        },
                    }
                else:
                    object_story_spec = {
                        "page_id": plan.page_id,
                        "link_data": {
                            "message": message,
                            # LIKE_PAGE ignores this link (the button does the follow action
                            # itself), so the Page's own URL is the sensible placeholder
                            # there. For every other goal this is the REAL destination:
                            # wa.me/<the brand's number>. It used to be a bare
                            # "https://wa.me/" with no number, which went nowhere.
                            "link": f"https://www.facebook.com/{plan.page_id}" if is_followers_goal else wa_link,
                            "picture": plan.creative.image_url,
                            "call_to_action": cta,
                        },
                    }
                creative_resp = await client.post(
                    f"{self._graph_base}/act_{self._ad_account_id}/adcreatives",
                    params={"access_token": self._access_token},
                    json={
                        "name": f"JaneAds-{plan.business_id}-creative",
                        "object_story_spec": object_story_spec,
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
        except Exception:
            await self._rollback_partial_launch(campaign_id, creative_id)
            raise

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

    async def _rollback_partial_launch(self, campaign_id: str, creative_id: str = "") -> None:
        """Undo a launch that failed midway, so Meta is never left holding a campaign
        our DB doesn't know about (see launch_campaign). Deleting the campaign cascades
        to its ad set and ad — same as delete_campaign — so only the campaign and the
        account-level ad creative need removing.

        Strictly best-effort and never raises: the caller is already unwinding a real
        failure, and the ORIGINAL error is what the user needs to see (e.g. "this
        WhatsApp number is not linked"). A cleanup problem must never mask it. Anything
        that can't be removed is logged loudly so it can be reconciled by hand.
        """
        if not (campaign_id or creative_id):
            return
        for node_id, label in ((campaign_id, "campaign"), (creative_id, "ad creative")):
            if not node_id:
                continue
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    resp = await client.delete(
                        f"{self._graph_base}/{node_id}",
                        params={"access_token": self._access_token},
                    )
                data = resp.json()
                if "error" in data:
                    print(f"[MetaAdapter] ORPHANED {label} {node_id} — rollback rejected: "
                          f"{data['error'].get('message')}", flush=True)
                else:
                    print(f"[MetaAdapter] rolled back partial launch: deleted {label} {node_id}", flush=True)
            except Exception as e:
                print(f"[MetaAdapter] ORPHANED {label} {node_id} — rollback failed: {e}", flush=True)

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

    async def get_delivery_estimate(self, targeting: Optional[dict] = None) -> Optional[dict]:
        """Meta's projected audience size for a targeting spec + optimization goal, used
        to show a real reach range on the campaign summary (Tier C). Best-effort: returns
        None on any failure (the summary just omits reach rather than blocking the plan).
        Matched to what a real Click-to-WhatsApp ad optimizes for (CONVERSATIONS). Pass the
        campaign's real `targeting` (geo pins) so the reach reflects the actual audience,
        not all of Nigeria — falls back to country-level if none is given."""
        import json as _json
        targeting = targeting or {"geo_locations": {"countries": ["NG"]}}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(
                    f"{self._graph_base}/act_{self._ad_account_id}/delivery_estimate",
                    params={
                        "access_token": self._access_token,
                        "optimization_goal": "CONVERSATIONS",
                        "targeting_spec": _json.dumps(targeting),
                    },
                )
            data = resp.json()
            if "error" in data:
                print(f"[meta] delivery_estimate error: {data['error'].get('message')}", flush=True)
                return None
            return data
        except Exception as e:
            print(f"[meta] delivery_estimate failed: {e}", flush=True)
            return None

    async def fetch_campaign_summary(self, campaign_id: str) -> dict:
        """One combined snapshot for the campaign-list (management) view — real
        delivery status, reach/impressions, spend, and conversation results/cost,
        pulled from Meta's read API. Uses field expansion to get the campaign's
        status, its ad set's end_time, AND its insights in a SINGLE Graph API
        call instead of three separate round trips — the ad-account-level rate
        limit (Meta's own cap on calls per account, not something we control) is
        shared across every caller of this account, so cutting our call volume
        3x directly reduces how often the list view runs into it. Raises
        MetaAPIError (callers should treat `is_rate_limited` on it as a
        transient "try again shortly", not a hard failure)."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._graph_base}/{campaign_id}",
                params={
                    "access_token": self._access_token,
                    "fields": (
                        "effective_status,"
                        "adsets{end_time},"
                        "insights{impressions,reach,spend,actions,cost_per_action_type}"
                    ),
                },
            )
            data = resp.json()
            _raise_for_error(data, "campaign summary fetch")

        raw_status = data.get("effective_status", "")
        delivery = _DELIVERY_LABELS.get(raw_status, raw_status.replace("_", " ").title() or "Paused")

        adsets = data.get("adsets", {}).get("data", [])
        ends_at = adsets[0].get("end_time") if adsets else None

        insight_rows = data.get("insights", {}).get("data", [])
        row = insight_rows[0] if insight_rows else {}

        def _action_value(rows: list, action_type: str):
            for a in rows:
                if a.get("action_type") == action_type:
                    return float(a.get("value", 0))
            return None

        actions = row.get("actions", [])
        cost_actions = row.get("cost_per_action_type", [])
        conversations_val = _action_value(actions, _CONVERSATION_ACTION_TYPE)
        cost_per_conversation = _action_value(cost_actions, _CONVERSATION_ACTION_TYPE)

        return {
            "delivery": delivery,
            "spend_ngn": float(row.get("spend", 0.0)),
            "impressions": int(row.get("impressions", 0)),
            "reach": int(row.get("reach", 0)),
            "conversations": int(conversations_val) if conversations_val is not None else 0,
            "cost_per_conversation_ngn": cost_per_conversation,
            "ends_at": ends_at,
        }

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

    async def set_delivery(self, campaign_id: str, active: bool) -> dict:
        """Turn a campaign on or off, from the caller's own campaign-management UI —
        not via Ads Manager. Cascades the SAME status to the campaign, its ad set, and
        its ad, since Meta only actually delivers when all three levels are ACTIVE;
        pausing any one of them stops delivery. Going active is the one genuinely
        consequential action in this whole feature — real money can start being
        spent from that point on."""
        record = await self._get_campaign_record(campaign_id)
        status = "ACTIVE" if active else "PAUSED"
        updated: dict[str, bool] = {}
        async with httpx.AsyncClient(timeout=30) as client:
            for node_id, label in (
                (campaign_id, "campaign"),
                (record.get("adset_id"), "adset"),
                (record.get("ad_id"), "ad"),
            ):
                if not node_id:
                    continue
                resp = await client.post(
                    f"{self._graph_base}/{node_id}",
                    params={"access_token": self._access_token},
                    json={"status": status},
                )
                data = resp.json()
                _raise_for_error(data, f"{label} status update")
                updated[label] = bool(data.get("success"))
        return {"status": status, "updated": updated}

    async def delete_campaign(self, campaign_id: str) -> bool:
        """Permanently delete the campaign on Meta's side (cascades to its ad set and
        ad automatically — Meta doesn't require deleting those separately). The Mongo
        record is left in place so it still shows up (as DELETED) if the caller lists
        it again right after; the router removes it from OUR list."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.delete(
                f"{self._graph_base}/{campaign_id}",
                params={"access_token": self._access_token},
            )
            data = resp.json()
            _raise_for_error(data, "campaign delete")
        return bool(data.get("success"))

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
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from .. import constants as C
from .. import messaging_ad
from .base import AdPlatformAdapter
from .. import constants as C
from ..destination import CTA_CHOICES, DestinationType, link_for_plan
from ..geo import meta_targeting_from_geo_named
from ..objectives import meta_objective, optimization_goal
from ..models import (
    CampaignObjective,
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


class WhatsAppNotLinked(MetaAPIError):
    """Meta refused a WhatsApp ad because the number isn't linked to the Page it runs
    on. Linking is a manual step in Meta that only the Page's owner can do, so the
    launch stops and says how — it never quietly becomes a lesser link ad."""

    def __init__(self, message: str, page_id: str, whatsapp_number: str,
                 code: Optional[int] = None, subcode: Optional[int] = None) -> None:
        super().__init__(message, code=code, subcode=subcode)
        self.page_id = page_id
        self.whatsapp_number = whatsapp_number


# How Meta says "this Page can't send people to WhatsApp": the number isn't linked
# (1487246), review fails on it (2446880), or the ad is refused outright with
# "Invalid ad campaign destination type" (2446509, live 2026-10-21 on a client Page
# with no WhatsApp linked).
_WHATSAPP_REFUSALS = {1487246, 2446880, 2446509}


def _whatsapp_refusal(data: dict) -> bool:
    return (data.get("error") or {}).get("error_subcode") in _WHATSAPP_REFUSALS


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


def _wants_multi_destination(destination_type, is_followers_goal: bool) -> bool:
    """Whether this campaign gets the manual campaign's multi-inbox messaging format.

    Any campaign whose tap opens a chat. A followers campaign opens nothing, and a
    website or custom link is not a conversation.
    """
    return (not is_followers_goal
            and destination_type in (DestinationType.WHATSAPP, DestinationType.INSTAGRAM_DM))


# Words Meta uses when it refuses a messaging DESTINATION rather than the ad set.
_ROUTING_WORDS = ("whatsapp", "instagram", "messenger", "destination", "messaging")


def _is_routing_refusal(error: dict) -> bool:
    text = " ".join(str(error.get(k) or "") for k in
                    ("message", "error_user_title", "error_user_msg")).lower()
    return any(w in text for w in _ROUTING_WORDS)


# The manual campaign targets every city with a 25-mile radius around it. A city key
# with no radius is Meta's administrative boundary, which for most Nigerian cities is
# far tighter than where the people who live and shop there actually are — part of why
# Jane's audiences priced 3-6x the manual one's per thousand impressions.
_CITY_RADIUS = {"radius": 25, "distance_unit": "mile"}


def _advantage_age(targeting: dict) -> dict:
    """The client's age range, expressed the way Advantage+ audience accepts it.

    With Advantage+ audience on, Meta rejects a hard maximum age below 65 ("You can add
    a lower maximum age as a suggestion instead", subcode 1870189) and caps a hard
    minimum at 25. The manual campaign on the account shows the shape it wants: hard
    age_min/age_max as the outer fence, and the intended range in age_range, which
    Meta treats as the suggestion it starts from. The client's choice is kept intact
    there — it still drives delivery, it just is not a wall.
    """
    # An age_range already present IS the client's choice — a live ad set read back
    # from Meta, or an age edit that set it explicitly (live_edit). Only the hard
    # fences are derived from it; re-deriving the range from the fences would replace
    # "23-55" with "23-65" every time an unrelated field was edited.
    if targeting.get("age_range"):
        lo, hi = (int(v) for v in targeting["age_range"])
    else:
        lo, hi = targeting.get("age_min"), targeting.get("age_max")
        if lo is None and hi is None:
            return targeting
        lo, hi = int(lo or 18), int(hi or 65)
    return {**targeting,
            "age_min": min(lo, 25),
            "age_max": 65,
            "age_range": [lo, hi]}


def _with_city_radius(targeting: dict) -> dict:
    geo = (targeting or {}).get("geo_locations") or {}
    cities = geo.get("cities")
    if not cities:
        return targeting
    return {**targeting, "geo_locations": {
        **geo,
        "cities": [{**_CITY_RADIUS, **c} if isinstance(c, dict) and "radius" not in c else c
                   for c in cities],
    }}


def _raise_not_linked(data: dict, plan) -> None:
    err = data.get("error") or {}
    raise WhatsAppNotLinked(
        f"WhatsApp {plan.whatsapp_number} isn't linked to Page {plan.page_id}: "
        f"{err.get('error_user_msg') or err.get('message')}",
        page_id=plan.page_id, whatsapp_number=plan.whatsapp_number,
        code=err.get("code"), subcode=err.get("error_subcode"))


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
        # A followers/engagement campaign routes nowhere off-platform — every other
        # goal sends the tap to the brand's chosen destination, so without a usable
        # one there's nothing for the ad to open. Blocked here too (the router's
        # ads_connection gate checks first) so a missing destination can never
        # silently ship a dead-end ad.
        # The OBJECTIVE counts as much as the goal now that the client picks it.
        # A Followers campaign is Page engagement with no WhatsApp routing at all;
        # leaving destination_type=WHATSAPP on it while optimising for POST_ENGAGEMENT
        # is a pair Meta rejects outright — "Performance goal isn't available … with
        # your campaign objective" (code=100, subcode=2446286). Live-caught by
        # launching each objective against the real ad account.
        is_followers_goal = (
            plan.goal == Goal.FOLLOWERS or plan.objective == CampaignObjective.FOLLOWERS
        )
        destination = link_for_plan(plan)
        dest_link = destination.link
        if not is_followers_goal and not dest_link:
            raise ValueError(
                f"CampaignPlan has no usable destination link for destination_type="
                f"'{destination.type.value}' — the ad would link nowhere"
            )
        # The real ad destination for every non-followers goal, resolved above by
        # destination.link_for_plan: the brand's own WhatsApp chat (wa.me/<number>,
        # pre-filled — a bare wa.me link opens an EMPTY chat with a stranger's number,
        # live-confirmed at 186 link clicks and zero messages), their website, or their
        # Instagram DMs (ig.me/m/<handle>). All three are plain link ads, so none of
        # them needs Meta's native per-brand messaging linking.
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
        # Meta's NATIVE Click-to-WhatsApp, or a plain wa.me link ad?
        #
        # Native is the better ad by some distance: Meta optimises for conversations
        # actually started rather than link clicks, and it is the only form that fires
        # messaging_conversation_started — a wa.me link ad can never report a single
        # conversation, which is why those campaigns showed "WhatsApp conversations 0"
        # and "cost per conversation N/A" while genuinely running and delivering clicks.
        #
        # It has one hard requirement: the number must be LINKED to the Page inside
        # Meta. Without that, create fails with "This WhatsApp phone number is not
        # linked to your account" (code=100, subcode=1487246) and review fails with
        # #2446880 — both live-confirmed, and the reason this was originally abandoned.
        # Whether the number is linked is decided by DRY-RUNNING the real ad set
        # payload, not by reading a Page field. GET /{page}?fields=whatsapp_number,
        # has_whatsapp_number is not a usable oracle: for a Page that Meta will
        # happily accept a native CTWA ad set on, both fields come back ABSENT, so
        # the old check returned False and silently downgraded every such campaign
        # to a wa.me link ad (and 409'd the launch gate). Live-verified 2026-09-09
        # on Page 203213912878798, whose fields are empty while
        # destination_type=WHATSAPP + promoted_object={page_id} validates clean.
        #
        # execution_options=['validate_only'] runs Meta's full validation and
        # creates nothing, so asking is free and the answer is the same one the
        # real create would give: valid means native works, subcode 1487246 means
        # that number is not linked to this Page. It runs after the campaign
        # exists because validation needs a real campaign_id.
        want_native_whatsapp = bool(
            not is_followers_goal
            and destination.type == DestinationType.WHATSAPP
            and plan.whatsapp_number
        )
        use_native_whatsapp = want_native_whatsapp
        # Any campaign whose tap opens a chat gets the manual campaign's
        # multi-destination format first (see the ad set create below).
        use_multi_destination = _wants_multi_destination(destination.type, is_followers_goal)

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
                        # OUTCOME_TRAFFIC is what allows LINK_CLICKS optimisation for a
                        # link ad. A followers campaign and a native Click-to-WhatsApp
                        # one are both real on-platform engagement, so both take
                        # OUTCOME_ENGAGEMENT — CONVERSATIONS optimisation is not
                        # available under OUTCOME_TRAFFIC.
                        # THE CLIENT'S choice, not one Jane derived from the goal.
                        # Every campaign used to come out OUTCOME_ENGAGEMENT or
                        # OUTCOME_TRAFFIC whatever the client actually wanted, so Ads
                        # Manager showed them "Objective: Engagement" for a campaign
                        # they had asked to be about sales. objectives.py holds the
                        # mapping, validated against the live ad account.
                        "objective": meta_objective(plan.objective),
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
                    # Targeting matched setting-for-setting to the one campaign on this
                    # account that was set up by hand in Ads Manager — which delivered
                    # cheaper conversations from better people than any Jane campaign.
                    # The geography, age and interests are still the client's; what is
                    # copied is how Ads Manager asks Meta to deliver against them.
                    #
                    # advantage_audience 1, with geo as an individual Advantage+
                    # setting, exactly as the manual ad set has it. This used to be 0
                    # deliberately, so the plan card's named interests and areas would
                    # be the literal audience. Ads Manager's default of 1 lets Meta reach
                    # beyond them where it predicts results, and that is the setup that
                    # performed; the card now says so (plan_fields).
                    #
                    # Placements and language are left to Meta, as on the manual ad set.
                    # They were briefly forced to Facebook+Instagram and English, which
                    # moved Jane AWAY from the campaign that worked — it has neither.
                    "targeting": _advantage_age(_with_city_radius({
                        **(await meta_targeting_from_geo_named(
                            plan.geo,
                            region=(plan.geo.city if plan.geo else ""),
                            access_token=self._access_token,
                        )),
                        **plan.audience_targeting,
                        "brand_safety_content_filter_levels": ["FACEBOOK_RELAXED", "AN_RELAXED"],
                        "user_age_unknown": False,
                        "targeting_automation": {"advantage_audience": 1,
                                                 "individual_setting": {"geo": 1}},
                    })),
                    "regional_regulated_categories": ["VOLUNTARY_VERIFICATION"],
                    "status": "PAUSED",
                    "start_time": start_time.isoformat(),
                    "end_time": end_time.isoformat(),
                }
                # The optimisation the CLIENT's objective implies, given where the tap
                # goes. A Click-to-WhatsApp ad under ENGAGEMENT optimises for
                # CONVERSATIONS; the same objective on a link ad has no conversation to
                # count and optimises for POST_ENGAGEMENT instead. The branches below
                # still own the WhatsApp ROUTING (destination_type, promoted_object) —
                # only the goal itself comes from the objective now.
                adset_payload["optimization_goal"] = optimization_goal(
                    plan.objective, is_whatsapp=use_native_whatsapp)

                if is_followers_goal:
                    # Grow the Page's own following — confirmed live shape (Meta Marketing
                    # API docs): same OUTCOME_ENGAGEMENT campaign objective as a Click-to-
                    # WhatsApp ad, but POST_ENGAGEMENT optimization and no WhatsApp routing
                    # at all (no destination_type, no promoted_object.whatsapp_phone_number).
                    #
                    # NO promoted_object either, despite the ad-level error asking for
                    # one: adding it makes Meta reject the AD SET with "Performance goal
                    # isn't available" (subcode 2446286), whichever optimisation is
                    # used. Probed directly — PAGE_LIKES and POST_ENGAGEMENT both
                    # validate without it and both fail with it.
                    pass
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
                    # native WhatsApp threads Meta can see, which is precisely why the
                    # native form above is used whenever the Page actually allows it.
                    if use_native_whatsapp:
                        adset_payload["destination_type"] = "WHATSAPP"
                        # Meta validates this pair at create time and rejects a number
                        # that isn't linked to the Page — the check before the campaign
                        # was created exists so that rejection can't reach a client.
                        adset_payload["promoted_object"] = {
                            "page_id": plan.page_id,
                            "whatsapp_phone_number": plan.whatsapp_number,
                        }
                    else:
                        adset_payload["optimization_goal"] = "LINK_CLICKS"

                # The manual campaign's routing: ONE ad set whose destination covers
                # Messenger, Instagram DMs and WhatsApp, with Meta opening whichever
                # inbox each person actually uses. Tried first on any messaging
                # campaign; if Meta refuses it for this Page (an inbox not connected,
                # say) the WhatsApp-only path below runs exactly as before, so a Page
                # that worked yesterday cannot stop working today.
                adset_data = None
                if use_multi_destination:
                    multi_payload = {
                        **{k: v for k, v in adset_payload.items()
                           if k not in ("destination_type", "promoted_object")},
                        **messaging_ad.adset_messaging_fields(plan.page_id),
                        "optimization_goal": "CONVERSATIONS",
                    }
                    multi_resp = await client.post(
                        f"{self._graph_base}/act_{self._ad_account_id}/adsets",
                        params={"access_token": self._access_token},
                        json=multi_payload,
                    )
                    multi_data = multi_resp.json()
                    multi_err = multi_data.get("error") or {}
                    # Only a refusal about the ROUTING falls back — an inbox this Page
                    # hasn't connected. Anything else (a bad bid, a budget floor) would
                    # fail the WhatsApp-only create identically, so it surfaces as
                    # itself, here, rather than costing a second create and arriving
                    # under a misleading "multi-destination refused" log line. Same
                    # rule the 1487246 fallback below already follows.
                    if multi_err and _is_routing_refusal(multi_err):
                        print(f"[MetaAds] multi-destination messaging refused for page "
                              f"{plan.page_id} ({multi_err.get('message')}) — "
                              f"using the WhatsApp-only ad set", flush=True)
                        use_multi_destination = False
                    else:
                        adset_payload, adset_data = multi_payload, multi_data
                if adset_data is None:
                    adset_resp = await client.post(
                        f"{self._graph_base}/act_{self._ad_account_id}/adsets",
                        params={"access_token": self._access_token},
                        json=adset_payload,
                    )
                    adset_data = adset_resp.json()

                # Meta is the only reliable oracle for "is this number linked to this
                # Page", and it answers by refusing the create. The ad stays a WhatsApp
                # ad: the client is told to link the number and launch again, rather
                # than being given a link ad that can never count a conversation.
                if want_native_whatsapp and _whatsapp_refusal(adset_data):
                    _raise_not_linked(adset_data, plan)

                # Named areas carry their REAL boundaries, which are far smaller than
                # the radius pins they replaced — a named neighbourhood estimates
                # thousands where a 2km pin estimated hundreds of thousands. Three of
                # those plus a detailed interest list can leave an audience Meta simply
                # refuses to serve: "The configured audience is not valid — Broaden your
                # audience" (code=100, subcode=2446395). Live-reported on a plan for
                # Opebi + Allen Avenue + Ogba that estimated a reach of 1,000.
                #
                # Widen one rung at a time and re-ask, rather than failing the launch or
                # jumping straight to nationwide. The campaign then lands at the tightest
                # geography Meta will actually deliver to, which is the honest answer to
                # "where should this run" — and each attempt builds a NEW payload, since
                # httpx holds a reference to what was already sent.
                widened_to = ""
                while (adset_data.get("error") or {}).get("error_subcode") == 2446395:
                    from ..geo import widen_targeting

                    broader = await widen_targeting(
                        adset_payload["targeting"],
                        (plan.geo.city if plan.geo else ""),
                        self._access_token,
                    )
                    if broader is None:
                        break
                    widened_to = json.dumps(broader.get("geo_locations"))
                    print(f"[MetaAds] audience too narrow to deliver — widening to {widened_to}",
                          flush=True)
                    adset_payload = {**adset_payload, "targeting": broader}
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
                    # The tap opens the brand's chosen destination through a plain link
                    # (see the ad set above for why this is a link ad and not Meta's
                    # native Click-to-WhatsApp). For photo ads the destination is
                    # link_data.link itself; video_data has no link field of its own
                    # (confirmed against Meta's AdCreativeVideoData reference), so there
                    # the destination has to ride on the CTA's value.link — which
                    # WHATSAPP_MESSAGE refuses to carry. destination.meta_cta owns that
                    # whole choice.
                    if use_native_whatsapp:
                        # The native button carries no link: the ad set's
                        # destination_type/promoted_object own the routing, and
                        # WHATSAPP_MESSAGE refuses a value.link (which is exactly why
                        # the link-ad path can't use it). Works for photo and video
                        # alike, since neither needs a link on the CTA here.
                        cta = {"type": "WHATSAPP_MESSAGE"}
                    else:
                        cta = destination.meta_call_to_action(plan.creative.is_video)
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
                            # A native Click-to-WhatsApp ad routes through the ad set,
                            # not through a link — Meta rejects a wa.me URL sitting in a
                            # link-destination field (#2446860), which is what Ads
                            # Manager complains about on the link-ad form.
                            "link": (
                                f"https://www.facebook.com/{plan.page_id}"
                                if (is_followers_goal or use_native_whatsapp)
                                else dest_link
                            ),
                            "picture": plan.creative.image_url,
                            "call_to_action": cta,
                        },
                    }
                creative_body = {
                    "name": f"JaneAds-{plan.business_id}-creative",
                    "object_story_spec": object_story_spec,
                }
                if use_multi_destination:
                    # The manual ad's creative: the Messenger button on the story, one
                    # call-to-action per inbox in the asset feed, the welcome message
                    # with its ice-breaker, a headline, and Meta's standard creative
                    # enhancements opted in. An API-created creative defaults every
                    # enhancement to OPT_OUT, which is most of what made Jane's ad a
                    # lesser version of the same ad.
                    cta_type = CTA_CHOICES.get(destination.cta, ("", "CONTACT_US"))[1]
                    ig = await self._instagram_ids(plan.page_id)
                    story = {"page_id": plan.page_id}
                    if ig.get("id"):
                        story["instagram_user_id"] = ig["id"]
                    common = {
                        "message": message,
                        "call_to_action": messaging_ad.primary_call_to_action(cta_type),
                        "page_welcome_message": messaging_ad.welcome_message(),
                    }
                    if plan.creative.is_video:
                        story["video_data"] = {
                            "video_id": video_id, "image_url": video_thumbnail_url,
                            "title": plan.creative.headline or "", **common,
                        }
                    else:
                        story["link_data"] = {
                            "link": "https://fb.com/messenger_doc/",
                            "name": plan.creative.headline or "",
                            "picture": plan.creative.image_url, **common,
                        }
                    creative_body = {
                        "name": f"JaneAds-{plan.business_id}-creative",
                        "object_story_spec": story,
                        "asset_feed_spec": messaging_ad.asset_feed_spec(
                            cta_type, ig.get("ig_id", "")),
                        **messaging_ad.creative_extras(),
                    }
                creative_resp = await client.post(
                    f"{self._graph_base}/act_{self._ad_account_id}/adcreatives",
                    params={"access_token": self._access_token},
                    json=creative_body,
                )
                creative_data = creative_resp.json()
                if want_native_whatsapp and _whatsapp_refusal(creative_data):
                    _raise_not_linked(creative_data, plan)
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
                if want_native_whatsapp and _whatsapp_refusal(ad_data):
                    _raise_not_linked(ad_data, plan)
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
                # The billing basis this campaign was SOLD under, frozen here so the
                # meter can never re-base a live campaign onto a later fee change
                # (billing.py reads this per record). Campaigns launched before this
                # field existed fall back to C.LEGACY_AD_SPEND_MARKUP.
                "ad_spend_markup": C.AD_SPEND_MARKUP,
                # Whether this campaign can EVER report a conversation. Meta fires
                # onsite_conversion.messaging_conversation_started only for native
                # Click-to-WhatsApp; a wa.me link ad reports 0 forever — which is not
                # "nobody messaged", it is "unmeasurable". Without this flag the
                # dashboard's headline metric ("X people messaged you") renders a
                # confident zero on every fallback campaign, so it is recorded at the
                # moment the distinction is actually known.
                # Multi-destination messaging reports started conversations the
                # same way native Click-to-WhatsApp does.
                "conversations_measurable": bool(use_native_whatsapp or use_multi_destination),
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

    async def _instagram_ids(self, page_id: str) -> dict:
        """The Page's Instagram business id and LEGACY id, or {} if unknowable.

        The Instagram leg of a multi-destination ad deep-links by the legacy id, which
        the shared ads token cannot read — Graph returns "missing permissions" for every
        Instagram account it is asked about. The Page's own token can, and it is on the
        brand's ads connection. Best-effort: without it the ad still runs on Messenger
        and WhatsApp.
        """
        try:
            conn = await self._db["social_connections"].find_one(
                {"page_id": page_id, "platform": "facebook_ads"}) or {}
            token = conn.get("page_access_token") or ""
            if not token:
                return {}
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{self._graph_base}/{page_id}",
                    params={"fields": "instagram_business_account{id,ig_id}",
                            "access_token": token},
                )
            account = (resp.json() or {}).get("instagram_business_account") or {}
            return {"id": str(account.get("id") or ""),
                    "ig_id": messaging_ad.instagram_legacy_id(resp.json())}
        except Exception as e:
            print(f"[MetaAds] instagram id lookup failed for page {page_id}: {e}", flush=True)
            return {}

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

    async def fetch_adset_schedule(self, campaign_id: str) -> dict:
        """When this campaign runs and what it spends a day — read fresh from Meta.

        `daily_budget` comes back in kobo, like every other Meta money field.
        """
        record = await self._get_campaign_record(campaign_id)
        adset_id = record.get("adset_id")
        if not adset_id:
            raise MetaAPIError(f"campaign {campaign_id} has no ad set recorded")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._graph_base}/{adset_id}",
                params={"access_token": self._access_token,
                        "fields": "id,status,effective_status,daily_budget,lifetime_budget,"
                                  "start_time,end_time"},
            )
            data = resp.json()
            _raise_for_error(data, "adset schedule read")
        return {
            "adset_id": adset_id,
            "ad_id": record.get("ad_id", ""),
            "status": data.get("status"),
            "effective_status": data.get("effective_status"),
            "daily_ngn": round(float(data.get("daily_budget") or 0) / 100, 2),
            "lifetime_ngn": round(float(data.get("lifetime_budget") or 0) / 100, 2),
            "start_time": data.get("start_time"),
            "end_time": data.get("end_time"),
        }

    async def extend_adset(self, adset_id: str, new_end_time: datetime) -> dict:
        """Push an ad set's end date out, then READ IT BACK.

        Meta answering 200 means accepted, not applied, so the caller is told what Meta
        actually holds afterwards — the same rule the targeting editor follows. This is
        the whole mechanism behind "keep it running": an ad set that has already ended
        resumes delivering once its end_time is in the future again, keeping the
        campaign id, the creative and everything Meta has learned about who responds.
        """
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._graph_base}/{adset_id}",
                params={"access_token": self._access_token},
                json={"end_time": new_end_time.isoformat()},
            )
            data = resp.json()
            _raise_for_error(data, "adset extend")
            confirm = await client.get(
                f"{self._graph_base}/{adset_id}",
                params={"access_token": self._access_token, "fields": "id,end_time"},
            )
            confirmed = confirm.json()
            _raise_for_error(confirmed, "adset extend readback")
        return {"end_time": confirmed.get("end_time")}

    async def fetch_adset_targeting(self, campaign_id: str) -> dict:
        """The ad set's CURRENT targeting, read fresh from Meta.

        Never served from our own copy. The whole point of reading it here is that
        somebody may have changed it in Ads Manager since we last looked, and an edit
        built on a stale baseline would quietly overwrite their work (PRD D45/CM17).
        """
        record = await self._get_campaign_record(campaign_id)
        adset_id = record.get("adset_id")
        if not adset_id:
            raise MetaAPIError(f"campaign {campaign_id} has no ad set recorded")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._graph_base}/{adset_id}",
                params={"access_token": self._access_token,
                        "fields": "id,name,status,effective_status,targeting"},
            )
            data = resp.json()
            _raise_for_error(data, "adset read")
        return {"adset_id": adset_id, "status": data.get("status"),
                "effective_status": data.get("effective_status"),
                "targeting": data.get("targeting") or {}}

    async def update_adset_targeting(
        self, adset_id: str, targeting: dict, validate_only: bool = False,
    ) -> dict:
        """Write new targeting to a live ad set, then READ IT BACK.

        Meta answering 200 means the request was accepted, not that the ad set now
        carries what we sent (PRD CM13). So this returns the targeting Meta actually
        holds afterwards and the caller compares — an ack is not evidence.

        `validate_only` asks Meta to check without changing anything. Live-verified
        that this genuinely validates targeting: age_min 5 and an invented interest id
        are both rejected, unlike the creative endpoint which ignores the Instagram
        identity entirely.
        """
        # A live Advantage+ ad set takes age only as a suggestion (see _advantage_age).
        if ((targeting.get("targeting_automation") or {}).get("advantage_audience") == 1):
            targeting = _advantage_age(targeting)
        payload = {"targeting": json.dumps(targeting),
                   "access_token": self._access_token}
        if validate_only:
            payload["execution_options"] = json.dumps(["validate_only"])
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(f"{self._graph_base}/{adset_id}", data=payload)
            data = resp.json()
            _raise_for_error(data, "adset targeting update")
            if validate_only:
                return {"validated": True}

            # Readback. A timeout above raises; a 200 here still has to be proved.
            confirm = await client.get(
                f"{self._graph_base}/{adset_id}",
                params={"access_token": self._access_token, "fields": "id,targeting"},
            )
            confirmed = confirm.json()
            _raise_for_error(confirmed, "adset targeting readback")
        return {"applied": True, "targeting": confirmed.get("targeting") or {}}

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

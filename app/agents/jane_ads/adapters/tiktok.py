"""
Jane + Ads — the TikTok Marketing API adapter (Phase 1, mirrors adapters/google.py's
own Phase-1 shape and honesty).

Implements AdPlatformAdapter against TikTok's documented Marketing API v1.3 REST
surface — NOT yet verified against a live or Test advertiser account (no
TIKTOK_ADS_ADVERTISER_ID/TIKTOK_ADS_ACCESS_TOKEN exist yet). Payload shapes below are
hand-built against TikTok's documented REST conventions and unit-tested via mocked
httpx responses, exactly how adapters/google.py's own request shapes were proven
correct before any live account existed — this file should get the same "verified
end-to-end against a real Ad Account" header update adapters/meta.py has, once real
credentials exist.

Scoping decision (Jane + Ads TikTok Phase-1 plan): TikTok's own preferred mechanism —
Spark Ads, running an ad from the business's OWN organic TikTok video — requires the
creator to manually generate a video-specific authorization code inside the TikTok
app itself and hand it to us. There is no OAuth path to that; it would be a real,
recurring manual step per ad. Phase 1 skips it: every brand's video creative is
uploaded to and launched from ONE shared URI-owned advertiser account instead, the
same already-established pattern META_ADS_PAGE_ID uses for Meta (every brand's Meta
ad already runs from one shared URI Page — see that setting's own comment in
config.py) — a brand is distinguished only by its own WhatsApp number and creative,
never a separate platform identity. Native Spark Ads / Click-to-WhatsApp support are
documented follow-ups, not blockers for this phase.

Every campaign/ad group/ad is created with operation_status="DISABLE" (TikTok's
paused-equivalent) — the same hard product rule as Meta and Google: nothing this
adapter creates is ever live until a human reviews and enables it in TikTok Ads
Manager.

Destination is whatever the brand chose (destination.py: their WhatsApp chat, their
website, or their Instagram DMs), resolved by the caller before this adapter is
constructed and carried on plan.destination_link — TikTok's native
click-to-message/Click-to-WhatsApp availability in Nigeria is an explicitly open
question in the Master PRD (Part E3); this adapter does not depend on it.

TikTok's response envelope is a real, documented difference from both Meta (a raw
resource, or an "error" key on failure) and Google ("error" key at top level): every
TikTok Marketing API response is {"code": 0, "message": "OK", "data": {...},
"request_id": "..."} on success — a non-zero "code" is the failure signal, not a
missing/present "error" key. _raise_for_error below checks that, not "error".
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

import httpx

from app.core.config import settings
from .base import AdPlatformAdapter
from ..destination import link_for_plan
from ..models import (
    CampaignPlan,
    ConversationDelivered,
    LaunchResult,
    PerAdSpend,
    Platform,
    SpendAuthorization,
)
from .. import constants as C

COLLECTION = "jane_ads_tiktok_campaigns"

# TikTok's location_ids are GeoNames ids (confirmed: TikTok's own docs give
# 6252001 as their United States example, which is exactly GeoNames' id for
# the US — https://www.geonames.org/6252001). Nigeria's GeoNames id is
# 2328926 (https://www.geonames.org/2328926/nigeria.html). The old value
# here (10000541) was an unverified guess that failed live against a real
# advertiser account ("At least 1 location ID is invalid") — TikTok's own
# /tool/region/ lookup isn't available on a Sandbox Ad Account (404s there),
# so this is confirmed by the GeoNames pattern match rather than a live
# region-list call; re-verify once a real (non-sandbox) launch is possible.
_NIGERIA_LOCATION_ID = "2328926"

# TikTok's ad-group-level optimization/billing pair for a click-driving campaign —
# mirrors the "Maximise Clicks"-equivalent choice google.py made for the same reason
# (no conversion volume exists yet to train a smarter bidding strategy).
_OPTIMIZATION_GOAL = "CLICK"
_BILLING_EVENT = "CPC"

# Every TikTok ad creative must declare who it's posted "as" (their Identity
# feature — like a Facebook Page for a Meta ad). Confirmed live (2026-08-31):
# omitting it fails ad/create with "creatives.identity_id is required".
#
# Was a synthetic CUSTOMIZED_USER identity (logo upload, no real TikTok
# account behind it) — confirmed live (2026-09-15) TikTok rejected ad/create
# with "Custom identities are no longer supported. Use an authorized TikTok
# account." TikTok deprecated Custom Identity for ALL new campaigns, API
# included, starting January 2026 (their own "F.I.R.S.T. Presence" framework:
# https://ads.tiktok.com/business/en-US/blog/custom-identity-transition).
#
# Fixed the same way, not around it: one real TikTok account (@uri.creative)
# linked once in Business Center → Accounts → TikTok accounts, authorized for
# the advertiser account, "Only show as ads" permission — keeps the same
# one-shared-identity architecture, just backed by a real linked account
# instead of a synthetic one. _get_authorized_identity looks it up via
# GET /identity/get/?identity_type=BC_AUTH_TT (confirmed live 2026-09-16,
# after the app's Identity scope was approved) rather than creating anything.
_IDENTITY_COLLECTION = "jane_ads_tiktok_identity"
_IDENTITY_TYPE = "BC_AUTH_TT"

# TikTok's operation_status values, translated to plain language for the campaign-
# list view — same purpose as meta.py's own _DELIVERY_LABELS. An empty campaign/get/
# result (the campaign no longer exists on TikTok's side at all) is treated as
# DELETE by the caller, not represented here.
_DELIVERY_LABELS = {
    "ENABLE": "Active",
    "DISABLE": "Paused",
    "DELETE": "Deleted",
}


class TikTokAdsAPIError(Exception):
    """A TikTok Marketing API call returned a non-zero `code`, or the adapter is
    misconfigured."""

    def __init__(self, message: str, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


def _raise_for_error(data: dict, context: str) -> None:
    # TikTok's envelope: {"code": 0, "message": "OK", "data": {...}}. code != 0 is
    # the failure signal — there is no "error" key the way Meta/Google use one.
    code = data.get("code")
    if code not in (0, None):
        raise TikTokAdsAPIError(f"{context}: {data.get('message', 'unknown error')}", code=code)


def _force_jpg_delivery(image_url: str) -> str:
    """Live-caught 2026-09-22: TikTok's file/image/ad/upload/ rejected a real photo
    with "Invalid params: image cannot be decoded" — the user had uploaded a WEBP,
    which TikTok's own Carousel Ads spec explicitly excludes (JPG/PNG only; Meta and
    our own upload picker both happily accept WEBP, so this mismatch is invisible
    everywhere except TikTok's decoder). Rather than reject the upload upstream (a
    WEBP dropped in from drafts/reuse would hit the same wall later), force Cloudinary
    to deliver JPG regardless of the stored format via its f_jpg transformation —
    standard Cloudinary delivery-URL syntax, inserted right after '/upload/'. A no-op,
    unchanged URL for anything not hosted on Cloudinary (nothing else is, today, but
    this must never raise on an unexpected shape)."""
    marker = "/image/upload/"
    idx = image_url.find(marker)
    if idx == -1:
        return image_url
    insert_at = idx + len(marker)
    return image_url[:insert_at] + "f_jpg/" + image_url[insert_at:]


def _force_tiktok_video_ratio(video_url: str) -> str:
    """Live-caught 2026-09-22: TikTok flagged an uploaded video ("WhatsApp Video
    2026-0...") with "Cannot be delivered to TikTok: Video ratio must be
    16:9/1:1/9:16" — its real dimensions (480x848) are close to but not exactly
    9:16, which is enough for TikTok to reject delivery outright even though the ad
    itself was created successfully. Our own upload endpoint (creative_upload in
    router.py) applies zero video transformation — whatever the source file's real
    dimensions are is exactly what gets sent to TikTok, and phone/WhatsApp-exported
    video is routinely a few pixels off a clean ratio.

    Rather than reject uploads with the wrong ratio (most user-shot vertical video
    for a mobile-first platform like TikTok is ALREADY close to 9:16, just not
    exact — asking the user to re-export is real friction for something we can fix
    transparently), force Cloudinary to deliver a clean 9:16 crop via its standard
    c_fill,ar_9:16,g_auto transformation — g_auto is Cloudinary's content-aware
    gravity, so it crops toward the visually important part of the frame rather
    than a blind centre-crop. A no-op, unchanged URL for anything not hosted on
    Cloudinary (nothing else is, today, but this must never raise on an unexpected
    shape). Applied unconditionally rather than only when the ratio is already
    wrong — re-encoding an already-9:16 video through this transformation is a
    harmless no-op, and detecting the source ratio first would mean an extra
    Cloudinary metadata call for no real benefit."""
    marker = "/video/upload/"
    idx = video_url.find(marker)
    if idx == -1:
        return video_url
    insert_at = idx + len(marker)
    return video_url[:insert_at] + "c_fill,ar_9:16,g_auto/" + video_url[insert_at:]


def _video_thumbnail_url(video_url: str) -> str:
    """Live-caught 2026-09-23: TikTok video campaigns showed no thumbnail at all in
    Jane's own Campaign Manager list (unlike carousel/Meta cards, which render
    fine) — router.py's campaign-record write stores plan.creative.image_url
    verbatim as the display image_url, but for a video ad that field IS the raw
    .mp4 URL, and a plain <img> tag silently renders nothing for a video file.
    Cloudinary can derive a JPG poster frame straight from a hosted video by
    requesting the exact same delivery URL with its extension swapped to .jpg —
    no separate upload, no extra API call, same "delivery-URL trick" pattern as
    _force_jpg_delivery/_force_tiktok_video_ratio above. A no-op, unchanged URL
    for anything not hosted on Cloudinary or with no file extension to swap."""
    if "/video/upload/" not in video_url:
        return video_url
    head, slash, last_segment = video_url.rpartition("/")
    if not slash or "." not in last_segment:
        return video_url
    stem, _, _ext = last_segment.rpartition(".")
    return f"{head}/{stem}.jpg"


class TikTokAdsAdapter(AdPlatformAdapter):
    """One instance per request/job. advertiser_id/access_token are ALWAYS
    caller-supplied (never read from settings inside this class) — Phase 1 callers
    pass settings.TIKTOK_ADS_ADVERTISER_ID/TIKTOK_ADS_ACCESS_TOKEN directly, since
    there is exactly one shared URI identity today (see module docstring); a future
    per-brand identity would only change what the CALLER passes in, not this class."""

    def __init__(self, db, advertiser_id: str, access_token: str) -> None:
        self._db = db
        self._advertiser_id = advertiser_id
        self._access_token = access_token
        self._api_base = f"{settings.TIKTOK_ADS_API_BASE}/open_api/{settings.TIKTOK_ADS_API_VERSION}"
        if not self._advertiser_id:
            raise TikTokAdsAPIError("advertiser_id is required")
        if not self._access_token:
            raise TikTokAdsAPIError("access_token is required")

    def _headers(self) -> dict:
        # TikTok's Marketing API uses a bare "Access-Token" header — NOT
        # "Authorization: Bearer", a real difference from both Meta and Google.
        return {"Access-Token": self._access_token, "Content-Type": "application/json"}

    async def _get_authorized_identity(self, client: httpx.AsyncClient) -> Tuple[str, str]:
        """Returns (identity_id, identity_authorized_bc_id) for the real
        TikTok account linked in Business Center and authorized for this
        advertiser account — every ad creative needs both (see the module
        comment above for why this replaced the old synthetic-identity
        creation flow). Confirmed live (2026-09-16): ad/create rejects a
        BC_AUTH_TT creative with '"Identity_type" and "Identity_bc_ID" don't
        match.' if identity_authorized_bc_id is omitted — it's not optional
        the way it might look from the identity_id alone being unique.
        Looked up once via GET /identity/get/ and cached in Mongo; the cache
        is keyed with _IDENTITY_TYPE so a stale CUSTOMIZED_USER-era doc from
        before this change is never mistaken for a valid BC_AUTH_TT identity."""
        cached = await self._db[_IDENTITY_COLLECTION].find_one(
            {"advertiser_id": self._advertiser_id, "identity_type": _IDENTITY_TYPE}
        )
        if cached and cached.get("identity_id") and cached.get("identity_authorized_bc_id"):
            return cached["identity_id"], cached["identity_authorized_bc_id"]

        identity_resp = await client.get(
            f"{self._api_base}/identity/get/",
            headers=self._headers(),
            params={"advertiser_id": self._advertiser_id, "identity_type": _IDENTITY_TYPE},
        )
        identity_data = identity_resp.json()
        _raise_for_error(identity_data, "identity lookup")
        candidates = (identity_data.get("data") or {}).get("identity_list") or []
        # available_status confirmed live: "AVAILABLE" on a working linked
        # account — prefer one, but fall back to the first entry rather than
        # hard-require the field (TikTok's shape here isn't documented enough
        # to be sure it's always present).
        chosen = next((c for c in candidates if c.get("available_status") == "AVAILABLE"), None) or (
            candidates[0] if candidates else None
        )
        if not chosen or not chosen.get("identity_id") or not chosen.get("identity_authorized_bc_id"):
            raise TikTokAdsAPIError(
                "No TikTok account is linked and authorized for this advertiser account. "
                "Link one in Business Center → Accounts → TikTok accounts, then authorize it "
                "for this advertiser account (see the F.I.R.S.T. Presence flow)."
            )
        identity_id = chosen["identity_id"]
        identity_bc_id = chosen["identity_authorized_bc_id"]

        await self._db[_IDENTITY_COLLECTION].update_one(
            {"advertiser_id": self._advertiser_id, "identity_type": _IDENTITY_TYPE},
            {"$set": {
                "identity_id": identity_id,
                "identity_authorized_bc_id": identity_bc_id,
                "username": chosen.get("username"),
                "display_name": chosen.get("display_name"),
                "cached_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        return identity_id, identity_bc_id

    async def _get_carousel_music_id(self, client: httpx.AsyncClient) -> str:
        """A usable music_id for a Carousel Ad — required by TikTok whenever
        ad_format is CAROUSEL_ADS and the ad is a Standard Carousel Non-Spark Ad
        (our exact scenario; confirmed against TikTok's own /ad/create/ API
        reference 2026-09-22, and the "source of this post is invalid" error this
        replaces was TikTok's actual live rejection of a carousel submitted without
        one). Nothing about this ad is meant to be about the music, so this just
        picks the first result of a generic keyword search via GET
        /file/music/get/ (music_scene=CAROUSEL_ADS, search_type=SEARCH_BY_KEYWORD)
        rather than asking the user to choose a track. NOT yet verified live —
        first real attempt at this specific call; if the keyword below returns no
        results on the real account, that's the next thing to adjust here."""
        resp = await client.get(
            f"{self._api_base}/file/music/get/",
            headers=self._headers(),
            params={
                "advertiser_id": self._advertiser_id,
                "music_scene": "CAROUSEL_ADS",
                "search_type": "SEARCH_BY_KEYWORD",
                "filtering": json.dumps({"keyword": "upbeat"}),
            },
        )
        data = resp.json()
        _raise_for_error(data, "carousel music lookup")
        musics = (data.get("data") or {}).get("musics") or []
        if not musics or not musics[0].get("music_id"):
            raise TikTokAdsAPIError(
                "No usable TikTok music found for a Carousel Ad (searched keyword "
                "'upbeat', music_scene=CAROUSEL_ADS). TikTok requires a music_id for "
                "every Carousel Ad; try again shortly or use a video ad instead."
            )
        return str(musics[0]["music_id"])

    async def launch_campaign(self, plan: CampaignPlan, auth: SpendAuthorization) -> LaunchResult:
        tiktok_plans = [p for p in plan.platforms if p.platform == Platform.TIKTOK]
        if not tiktok_plans:
            raise ValueError("TikTokAdsAdapter only handles Platform.TIKTOK plans")
        dest_link = link_for_plan(plan).link
        if not dest_link:
            raise ValueError(
                f"CampaignPlan has no usable destination link for destination_type="
                f"'{plan.destination_type}' — a TikTok ad needs a landing page URL"
            )
        is_carousel = bool(plan.creative and len(plan.creative.carousel_image_urls) >= 2)
        if not plan.creative or not (
            (plan.creative.image_url and plan.creative.is_video) or is_carousel
        ):
            # router.py's tiktok_needs_video gate means a TikTok plan should never
            # reach here without either real video creative OR 2+ carousel photos —
            # asserted explicitly rather than trusted silently, same discipline as
            # Meta's hard creative requirement. TikTok has no single-static-image ad
            # unit at all (video or Carousel Ads only), which is why this isn't just
            # "is there an image_url".
            raise ValueError(
                "TikTok requires either video creative (plan.creative.is_video) or "
                "2+ carousel photos (plan.creative.carousel_image_urls)"
            )

        platform_plan = tiktok_plans[0]
        total_budget_ngn = min(platform_plan.budget_ngn, auth.funded_amount_ngn)
        days = max(platform_plan.days, 1)

        now = datetime.now(timezone.utc)
        end = now + timedelta(days=days)

        campaign_id = ""
        adgroup_id = ""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # 1. Campaign — a container only; the real budget lives on the ad
                # group (mirrors meta.py's own "budget lives on the ad set, not the
                # campaign" decision, for the same per-business isolation reason).
                campaign_resp = await client.post(
                    f"{self._api_base}/campaign/create/",
                    headers=self._headers(),
                    json={
                        "advertiser_id": self._advertiser_id,
                        # Live-caught 2026-09-21: TikTok rejects campaign/create with
                        # "Campaign name already exists" once a business launches a
                        # second campaign with the same goal — this name had no
                        # uniqueness suffix at all, unlike every other name/file_name
                        # in this adapter (ad_name, adgroup_name, video/image
                        # file_name), which all already append a random hex suffix for
                        # exactly this reason.
                        "campaign_name": f"JaneAds-{plan.business_id}-{plan.goal.value}-{uuid.uuid4().hex[:8]}",
                        "objective_type": "TRAFFIC",
                        "budget_mode": "BUDGET_MODE_INFINITE",
                        "operation_status": "DISABLE",
                    },
                )
                campaign_data = campaign_resp.json()
                _raise_for_error(campaign_data, "campaign creation")
                campaign_id = str(campaign_data["data"]["campaign_id"])

                video_id = ""
                image_ids_for_ad: list[str] = []   # cover image (video path) or every
                                                    # carousel slide (carousel path)
                if not is_carousel:
                    # 2. Video upload — UPLOAD_BY_URL lets TikTok fetch the hosted file
                    # directly (same "server fetches it, no re-streaming needed here"
                    # shape as Meta's /advideos file_url).
                    tiktok_video_url = _force_tiktok_video_ratio(plan.creative.image_url)
                    # Live-caught 2026-09-22: "video upload: Failed to fetch url data."
                    # Cloudinary generates a video transformation ON DEMAND on its
                    # first request — unlike images this is a real transcode, which
                    # can take longer than TikTok's own URL-fetch timeout for a
                    # derivative nobody has ever requested before (every brand-new
                    # upload's first launch attempt). Warm the EXACT SAME transformed
                    # URL ourselves first (generous timeout — a real transcode, not a
                    # quick API call) so Cloudinary already has it cached by the time
                    # TikTok's own fetch hits it moments later. Best-effort: if the
                    # warm-up itself times out, still attempt the real upload — the
                    # transcode may finish server-side moments after our request gives
                    # up, and TikTok's own fetch could still land on the now-cached
                    # result.
                    if tiktok_video_url != plan.creative.image_url:
                        # Only when a transformation was actually applied (a real
                        # Cloudinary derivative to warm) — for anything not hosted on
                        # Cloudinary, the URL is unchanged and there's no cold-cache
                        # concern to pre-empt.
                        try:
                            await client.head(tiktok_video_url, timeout=90)
                        except Exception as e:
                            print(f"[TikTokAdsAdapter] video warm-up failed, continuing anyway: {e}", flush=True)
                    video_resp = await client.post(
                        f"{self._api_base}/file/video/ad/upload/",
                        headers=self._headers(),
                        json={
                            "advertiser_id": self._advertiser_id,
                            "upload_type": "UPLOAD_BY_URL",
                            "video_url": tiktok_video_url,
                            "file_name": f"jane-ads-{plan.business_id}-{uuid.uuid4().hex[:8]}.mp4",
                        },
                    )
                    video_data = video_resp.json()
                    _raise_for_error(video_data, "video upload")
                    video_entry = video_data["data"][0] if isinstance(video_data["data"], list) else video_data["data"]
                    video_id = video_entry["video_id"]

                    # 2b. Cover image — confirmed live (2026-09-16): ad/create
                    # rejects a SINGLE_VIDEO creative with "You must upload an
                    # image." without an image_ids entry, even though the ad is
                    # purely a video. Re-upload the video's own auto-generated
                    # cover frame (video_cover_url, returned by the upload above)
                    # as an image asset rather than asking for a second creative
                    # input anywhere upstream — it's a required-but-cosmetic
                    # thumbnail, not a real second asset choice.
                    cover_url = video_entry.get("video_cover_url")
                    if not cover_url:
                        raise TikTokAdsAPIError(f"video upload returned no video_cover_url: {video_entry}")
                    cover_resp = await client.post(
                        f"{self._api_base}/file/image/ad/upload/",
                        headers=self._headers(),
                        json={
                            "advertiser_id": self._advertiser_id,
                            "upload_type": "UPLOAD_BY_URL",
                            "image_url": cover_url,
                            "file_name": f"jane-ads-{plan.business_id}-cover-{uuid.uuid4().hex[:8]}.jpg",
                        },
                    )
                    cover_data = cover_resp.json()
                    _raise_for_error(cover_data, "cover image upload")
                    cover_entry = cover_data["data"][0] if isinstance(cover_data["data"], list) else cover_data["data"]
                    image_ids_for_ad = [cover_entry["image_id"]]
                else:
                    # 2-carousel. Carousel Ads (TikTok's real image-ad format — there is
                    # no single-static-image ad unit) need every slide uploaded as its
                    # own image asset, same UPLOAD_BY_URL call the video path's cover
                    # image already uses, once per photo. NOT yet verified live — first
                    # real attempt; if TikTok's ad/create rejects this for a missing
                    # music_infos/music_id (their own docs say Carousel Ads require
                    # music, no silent carousels), that's the next thing to add here,
                    # discovered the same way every other TikTok requirement in this
                    # file was (see the "Confirmed live" comments throughout).
                    for i, url in enumerate(plan.creative.carousel_image_urls):
                        img_resp = await client.post(
                            f"{self._api_base}/file/image/ad/upload/",
                            headers=self._headers(),
                            json={
                                "advertiser_id": self._advertiser_id,
                                "upload_type": "UPLOAD_BY_URL",
                                "image_url": _force_jpg_delivery(url),
                                "file_name": f"jane-ads-{plan.business_id}-carousel-{i}-{uuid.uuid4().hex[:8]}.jpg",
                            },
                        )
                        img_data = img_resp.json()
                        _raise_for_error(img_data, f"carousel image {i} upload")
                        img_entry = img_data["data"][0] if isinstance(img_data["data"], list) else img_data["data"]
                        image_ids_for_ad.append(img_entry["image_id"])

                # 3. Ad group — the real budget + targeting + schedule live here.
                # PAUSED via operation_status="DISABLE", same as every other create
                # call in this method.
                adgroup_resp = await client.post(
                    f"{self._api_base}/adgroup/create/",
                    headers=self._headers(),
                    json={
                        "advertiser_id": self._advertiser_id,
                        "campaign_id": campaign_id,
                        "adgroup_name": f"JaneAds-{plan.business_id}-adgroup",
                        # Required by TikTok for every ad group — confirmed live
                        # (2026-08-31): omitting it fails with "Invalid value for
                        # promotion_type" rather than defaulting to anything. The
                        # destination is an external URL (wa.me), matching the
                        # campaign's own TRAFFIC objective_type above, so this is
                        # "WEBSITE" — not TikTok's native in-app messaging type,
                        # since we're not using their Click-to-Message integration.
                        "promotion_type": "WEBSITE",
                        "placement_type": "PLACEMENT_TYPE_NORMAL",
                        "placements": ["PLACEMENT_TIKTOK"],
                        "location_ids": [_NIGERIA_LOCATION_ID],
                        "budget_mode": "BUDGET_MODE_TOTAL",
                        "budget": total_budget_ngn,
                        "schedule_type": "SCHEDULE_START_END",
                        "schedule_start_time": now.strftime("%Y-%m-%d %H:%M:%S"),
                        "schedule_end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
                        "optimization_goal": _OPTIMIZATION_GOAL,
                        "billing_event": _BILLING_EVENT,
                        # Confirmed live (2026-08-31): omitting bid_type fails with
                        # "Bid needs to be greater than $0.00" — TikTok defaults to
                        # requiring an explicit bid_price rather than automatic
                        # bidding unless told otherwise. NO_BID lets TikTok pick the
                        # bid automatically to spend the budget efficiently, matching
                        # Meta/Google's own automatic-bidding choice elsewhere in
                        # Jane + Ads — no per-click bid management for the user.
                        "bid_type": "BID_TYPE_NO_BID",
                        # Confirmed live (2026-08-31): whatever pacing TikTok defaults
                        # to here reads as "accelerated," which it flatly rejects
                        # paired with BID_TYPE_NO_BID for this objective ("Accelerated
                        # delivery under No-Bid strategy is not supported"). SMOOTH
                        # (standard pacing, spend spread across the whole schedule)
                        # is also just the right choice for an unattended, no-manual-
                        # tuning campaign regardless of the error.
                        "pacing": "PACING_MODE_SMOOTH",
                        "operation_status": "DISABLE",
                    },
                )
                adgroup_data = adgroup_resp.json()
                _raise_for_error(adgroup_data, "ad group creation")
                adgroup_id = str(adgroup_data["data"]["adgroup_id"])

                # 4. The ad itself — video creative + copy + the brand's destination
                # as the landing page, paused. Every creative must declare an identity
                # (who it's posted "as") — the one linked-and-authorized TikTok
                # account, looked up once (see module comment for the Custom
                # Identity deprecation this replaced).
                identity_id, identity_bc_id = await self._get_authorized_identity(client)
                creative_fields: dict = {
                    # Confirmed live (2026-09-11): ad/create rejects the
                    # creative with "Missing required field(s): 'ad_format'"
                    # without this.
                    "ad_format": "CAROUSEL_ADS" if is_carousel else "SINGLE_VIDEO",
                    "ad_name": f"JaneAds-{plan.business_id}-ad",
                    "ad_text": (plan.creative.primary_text or plan.creative.headline or "")[:100],
                    "identity_id": identity_id,
                    "identity_type": _IDENTITY_TYPE,
                    # Confirmed live (2026-09-16): required alongside
                    # identity_id for BC_AUTH_TT — ad/create rejects it
                    # with '"Identity_type" and "Identity_bc_ID" don't
                    # match.' without this.
                    "identity_authorized_bc_id": identity_bc_id,
                    "landing_page_url": dest_link,
                    "call_to_action": "CONTACT_US",
                }
                if is_carousel:
                    # Confirmed live 2026-09-22: ad/create rejected the carousel with
                    # "The source of this post is invalid" — TikTok's own API
                    # reference confirms music_id is REQUIRED for
                    # "ad_format: CAROUSEL_ADS ... Standard Carousel Non-Spark Ads"
                    # (our exact scenario). Fetched via GET /file/music/get/
                    # (music_scene=CAROUSEL_ADS) rather than asking the user to pick
                    # one — nothing about this ad is meant to be about the music.
                    creative_fields["image_ids"] = image_ids_for_ad
                    creative_fields["music_id"] = await self._get_carousel_music_id(client)
                else:
                    creative_fields["video_id"] = video_id
                    # Confirmed live (2026-09-16): SINGLE_VIDEO still requires a
                    # cover image or ad/create fails with "You must upload an
                    # image." — image_ids_for_ad here is the video's own
                    # auto-generated cover frame, re-uploaded as an image asset.
                    creative_fields["image_ids"] = image_ids_for_ad

                ad_resp = await client.post(
                    f"{self._api_base}/ad/create/",
                    headers=self._headers(),
                    json={
                        "advertiser_id": self._advertiser_id,
                        "adgroup_id": adgroup_id,
                        "creatives": [creative_fields],
                        "operation_status": "DISABLE",
                    },
                )
                ad_data = ad_resp.json()
                _raise_for_error(ad_data, "ad creation")
                ad_id = str(ad_data["data"]["ad_ids"][0]) if ad_data["data"].get("ad_ids") else str(ad_data["data"].get("ad_id", ""))
        except Exception:
            await self._rollback_partial_launch(campaign_id)
            raise

        await self._db[COLLECTION].update_one(
            {"campaign_id": campaign_id},
            {"$set": {
                "campaign_id": campaign_id,
                "adgroup_id": adgroup_id,
                "ad_id": ad_id,
                "business_id": plan.business_id,
                "advertiser_id": self._advertiser_id,
                "platform": "tiktok",
                "last_click_count": 0,
                "created_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )

        return LaunchResult(
            campaign_id=campaign_id,
            ad_ids={plan.business_id: ad_id},
            platforms=[Platform.TIKTOK],
            launched=True,
        )

    async def _rollback_partial_launch(self, campaign_id: str) -> None:
        """Undo a launch that failed midway. Strictly best-effort and never raises —
        the caller is already unwinding a real failure; a cleanup problem must never
        mask the ORIGINAL error. Same shape as google.py's own rollback."""
        if not campaign_id:
            return
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    # Live-caught 2026-09-22: this path's segments were swapped —
                    # TikTok's real endpoint is campaign/status/update/ (confirmed
                    # live earlier this session, deleting the 10 diagnostic test
                    # campaigns through the exact same call shape). The wrong path
                    # 404s, which is exactly what turned every rollback attempt into
                    # a real orphaned campaign in the live account instead of a
                    # cleaned-up one.
                    f"{self._api_base}/campaign/status/update/",
                    headers=self._headers(),
                    json={
                        "advertiser_id": self._advertiser_id,
                        "campaign_ids": [campaign_id],
                        "operation_status": "DELETE",
                    },
                )
                # A non-JSON/empty body (seen live: httpx's .json() raising
                # "Expecting value: line 1 column 1") means something failed
                # before TikTok's own JSON envelope ever got written — surface
                # the raw status/text instead of a cryptic decode error, since
                # this path is diagnostic-only and never re-raises anyway.
                try:
                    data = resp.json()
                except ValueError:
                    print(f"[TikTokAdsAdapter] ORPHANED campaign {campaign_id} — "
                          f"rollback got a non-JSON response: HTTP {resp.status_code} {resp.text[:300]!r}", flush=True)
                    return
                if data.get("code") not in (0, None):
                    print(f"[TikTokAdsAdapter] ORPHANED campaign {campaign_id} — "
                          f"rollback rejected: {data.get('message')}", flush=True)
                else:
                    print(f"[TikTokAdsAdapter] rolled back partial launch: deleted campaign {campaign_id}", flush=True)
        except Exception as e:
            print(f"[TikTokAdsAdapter] ORPHANED campaign {campaign_id} — rollback failed: {e}", flush=True)

    async def _get_campaign_record(self, campaign_id: str) -> dict:
        record = await self._db[COLLECTION].find_one({"campaign_id": campaign_id})
        if not record:
            raise TikTokAdsAPIError(
                f"No stored record for campaign_id={campaign_id} — was it launched via this adapter?"
            )
        return record

    async def fetch_per_ad_spend(self, campaign_id: str) -> list[PerAdSpend]:
        """Current CUMULATIVE spend per ad (matches the interface contract). TikTok's
        Reporting API returns spend in the advertiser account's own currency — same
        NGN-conversion discipline as google.py's fetch_per_ad_spend: convert via
        constants.USD_TO_NGN if USD, pass through if already NGN, log loudly for
        anything else (no safe guess possible without a live FX source)."""
        record = await self._get_campaign_record(campaign_id)
        now = datetime.now(timezone.utc)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._api_base}/report/integrated/get/",
                headers=self._headers(),
                params={
                    "advertiser_id": self._advertiser_id,
                    "report_type": "BASIC",
                    "data_level": "AUCTION_AD",
                    "dimensions": json.dumps(["ad_id"]),
                    "metrics": json.dumps(["spend"]),
                    "filtering": json.dumps([{"field_name": "campaign_id", "filter_type": "IN", "filter_value": json.dumps([campaign_id])}]),
                    "start_date": (now - timedelta(days=30)).strftime("%Y-%m-%d"),
                    "end_date": now.strftime("%Y-%m-%d"),
                    "page": 1,
                    "page_size": 100,
                },
            )
        data = resp.json()
        _raise_for_error(data, "spend report")
        rows = (data.get("data") or {}).get("list") or []

        if not rows:
            return [PerAdSpend(
                business_id=record["business_id"], ad_id=record["ad_id"],
                campaign_id=campaign_id, platform=Platform.TIKTOK, spend_ngn=0.0, at=now,
            )]

        def _to_ngn(spend: float, currency: str) -> float:
            if currency in ("", "NGN"):
                return spend
            if currency == "USD":
                return spend * C.USD_TO_NGN
            print(f"[TikTokAdsAdapter] unhandled account currency {currency!r} — "
                  f"returning un-converted amount, verify manually", flush=True)
            return spend

        return [
            PerAdSpend(
                business_id=record["business_id"],
                ad_id=str((row.get("dimensions") or {}).get("ad_id", record["ad_id"])),
                campaign_id=campaign_id,
                platform=Platform.TIKTOK,
                spend_ngn=_to_ngn(
                    float((row.get("metrics") or {}).get("spend", 0)),
                    (data.get("data") or {}).get("currency", ""),
                ),
                at=now,
            )
            for row in rows
        ]

    async def poll_conversations(self, campaign_id: str) -> list[ConversationDelivered]:
        """TikTok has no 'conversation started' webhook wired up yet (same gap as
        Google) — maps a CLICK to ConversationDelivered, same delta-since-last-poll
        discipline as google.py (last_click_count stored per campaign)."""
        record = await self._get_campaign_record(campaign_id)
        now = datetime.now(timezone.utc)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{self._api_base}/report/integrated/get/",
                headers=self._headers(),
                params={
                    "advertiser_id": self._advertiser_id,
                    "report_type": "BASIC",
                    "data_level": "AUCTION_CAMPAIGN",
                    "dimensions": json.dumps(["campaign_id"]),
                    "metrics": json.dumps(["clicks", "spend"]),
                    "filtering": json.dumps([{"field_name": "campaign_id", "filter_type": "IN", "filter_value": json.dumps([campaign_id])}]),
                    "start_date": (now - timedelta(days=30)).strftime("%Y-%m-%d"),
                    "end_date": now.strftime("%Y-%m-%d"),
                    "page": 1,
                    "page_size": 1,
                },
            )
        data = resp.json()
        _raise_for_error(data, "conversation poll")
        rows = (data.get("data") or {}).get("list") or []
        if not rows:
            return []
        metrics = rows[0].get("metrics") or {}
        total_clicks = int(float(metrics.get("clicks", 0)))
        spend_ngn = float(metrics.get("spend", 0))

        already_seen = int(record.get("last_click_count", 0))
        new_count = max(total_clicks - already_seen, 0)
        if new_count == 0:
            return []

        await self._db[COLLECTION].update_one(
            {"campaign_id": campaign_id},
            {"$set": {"last_click_count": total_clicks}},
        )

        cost_per_click = (spend_ngn / total_clicks) if total_clicks else 0.0
        return [
            ConversationDelivered(
                business_id=record["business_id"],
                ad_id=record["ad_id"],
                campaign_id=campaign_id,
                platform=Platform.TIKTOK,
                at=now,
                charge_ngn=cost_per_click,
            )
            for _ in range(new_count)
        ]

    async def pause_ad(self, campaign_id: str, ad_id: str) -> bool:
        record = await self._get_campaign_record(campaign_id)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._api_base}/ad/status/update/",
                headers=self._headers(),
                json={
                    "advertiser_id": self._advertiser_id,
                    "adgroup_id": record.get("adgroup_id", ""),
                    "ad_ids": [ad_id],
                    "operation_status": "DISABLE",
                },
            )
        data = resp.json()
        _raise_for_error(data, "pause ad")
        return data.get("code") == 0

    # ── Methods below are NOT part of the AdPlatformAdapter ABC — they mirror an
    # extra contract MetaAdPlatformAdapter (adapters/meta.py) exposes that billing.py
    # and the campaign-management router endpoints call directly on whichever
    # adapter they're given. Kept the same shape here so those callers can dispatch
    # by platform without caring which adapter they're holding. ──────────────────

    async def fetch_campaign_summary(self, campaign_id: str) -> dict:
        """One combined snapshot for the campaign-list (management) view — mirrors
        MetaAdPlatformAdapter.fetch_campaign_summary's return shape exactly, since
        billing.py and the campaign-list endpoint depend on that dict shape
        directly. Two calls where Meta needs one (status, then a report call) —
        TikTok's Marketing API has no single combined field-expansion read the way
        Meta's does. Not yet verified against a live account, same honesty as the
        rest of this file."""
        await self._get_campaign_record(campaign_id)  # 404s cleanly if unknown to us
        now = datetime.now(timezone.utc)
        async with httpx.AsyncClient(timeout=30) as client:
            status_resp = await client.get(
                f"{self._api_base}/campaign/get/",
                headers=self._headers(),
                params={
                    "advertiser_id": self._advertiser_id,
                    "filtering": json.dumps({"campaign_ids": [campaign_id]}),
                },
            )
            status_data = status_resp.json()
            _raise_for_error(status_data, "campaign status fetch")
            status_rows = (status_data.get("data") or {}).get("list") or []
            # No rows back means TikTok no longer has this campaign at all — same
            # "Deleted" signal Meta gives via effective_status, just absent here
            # instead of an explicit value.
            raw_status = status_rows[0].get("operation_status", "") if status_rows else "DELETE"
            delivery = _DELIVERY_LABELS.get(raw_status, raw_status.replace("_", " ").title() or "Paused")

            report_resp = await client.get(
                f"{self._api_base}/report/integrated/get/",
                headers=self._headers(),
                params={
                    "advertiser_id": self._advertiser_id,
                    "report_type": "BASIC",
                    "data_level": "AUCTION_CAMPAIGN",
                    "dimensions": json.dumps(["campaign_id"]),
                    "metrics": json.dumps(["impressions", "reach", "clicks", "spend"]),
                    "filtering": json.dumps([{"field_name": "campaign_id", "filter_type": "IN", "filter_value": json.dumps([campaign_id])}]),
                    "start_date": (now - timedelta(days=30)).strftime("%Y-%m-%d"),
                    "end_date": now.strftime("%Y-%m-%d"),
                    "page": 1,
                    "page_size": 1,
                },
            )
        report_data = report_resp.json()
        _raise_for_error(report_data, "campaign summary report")
        report_rows = (report_data.get("data") or {}).get("list") or []
        metrics = (report_rows[0].get("metrics") if report_rows else None) or {}
        clicks = int(float(metrics.get("clicks", 0)))
        # Advertiser account currency, same un-converted-if-unknown caveat
        # fetch_per_ad_spend's _to_ngn documents — left as-is here since this method
        # doesn't have a currency field to check against (report/integrated/get/
        # doesn't return one at this data_level); revisit once a live account
        # confirms whether the advertiser account is NGN- or USD-denominated.
        spend_ngn = float(metrics.get("spend", 0))
        cost_per_click = (spend_ngn / clicks) if clicks else None

        return {
            "delivery": delivery,
            "spend_ngn": spend_ngn,
            "impressions": int(float(metrics.get("impressions", 0))),
            "reach": int(float(metrics.get("reach", 0))),
            # A click approximates a conversation here, same documented gap
            # poll_conversations/fetch_per_ad_spend already carry — no
            # conversation-start webhook exists yet for TikTok.
            "conversations": clicks,
            "cost_per_conversation_ngn": cost_per_click,
            "ends_at": None,   # not stored on the campaign record today
        }

    async def set_delivery(self, campaign_id: str, active: bool) -> dict:
        """Turn a campaign on or off from the caller's own campaign-management
        view — no TikTok Ads Manager needed. Cascades the SAME operation_status to
        the campaign, its ad group, and its ad, mirroring
        MetaAdPlatformAdapter.set_delivery's cascade exactly: TikTok, like Meta,
        only actually delivers when every level is enabled. Going active is the
        one genuinely consequential action here — real budget can start being
        spent from that point on."""
        record = await self._get_campaign_record(campaign_id)
        status = "ENABLE" if active else "DISABLE"
        updated: dict[str, bool] = {}
        async with httpx.AsyncClient(timeout=30) as client:
            campaign_resp = await client.post(
                # Same segment-order bug as _rollback_partial_launch had (see its
                # comment) — TikTok's real endpoint is campaign/status/update/,
                # not campaign/update/status/. The wrong path 404s, which is
                # exactly why activating a TikTok campaign from Jane's own toggle
                # silently failed and had to be done manually in Ads Manager.
                f"{self._api_base}/campaign/status/update/",
                headers=self._headers(),
                json={
                    "advertiser_id": self._advertiser_id,
                    "campaign_ids": [campaign_id],
                    "operation_status": status,
                },
            )
            campaign_data = campaign_resp.json()
            _raise_for_error(campaign_data, "campaign status update")
            updated["campaign"] = campaign_data.get("code") == 0

            adgroup_id = record.get("adgroup_id", "")
            if adgroup_id:
                adgroup_resp = await client.post(
                    # Same bug, same fix: adgroup/status/update/, not
                    # adgroup/update/status/.
                    f"{self._api_base}/adgroup/status/update/",
                    headers=self._headers(),
                    json={
                        "advertiser_id": self._advertiser_id,
                        "adgroup_ids": [adgroup_id],
                        "operation_status": status,
                    },
                )
                adgroup_data = adgroup_resp.json()
                _raise_for_error(adgroup_data, "ad group status update")
                updated["adgroup"] = adgroup_data.get("code") == 0

            ad_id = record.get("ad_id", "")
            if ad_id:
                ad_resp = await client.post(
                    f"{self._api_base}/ad/status/update/",
                    headers=self._headers(),
                    json={
                        "advertiser_id": self._advertiser_id,
                        "adgroup_id": adgroup_id,
                        "ad_ids": [ad_id],
                        "operation_status": status,
                    },
                )
                ad_data = ad_resp.json()
                _raise_for_error(ad_data, "ad status update")
                updated["ad"] = ad_data.get("code") == 0

        return {"status": status, "updated": updated}

    async def delete_campaign(self, campaign_id: str) -> bool:
        """Permanently delete the campaign on TikTok's side (its ad group and ad
        go with it — TikTok doesn't require deleting those separately, same as
        Meta). Same endpoint _rollback_partial_launch already uses for a failed
        launch, but this one RAISES on failure rather than swallowing it — a
        caller-requested delete failing silently would leave them thinking a
        campaign is gone when it isn't."""
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._api_base}/campaign/update/status/",
                headers=self._headers(),
                json={
                    "advertiser_id": self._advertiser_id,
                    "campaign_ids": [campaign_id],
                    "operation_status": "DELETE",
                },
            )
        data = resp.json()
        _raise_for_error(data, "campaign delete")
        return data.get("code") == 0

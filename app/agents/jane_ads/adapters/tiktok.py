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

Destination is always a wa.me link (reusing whatsapp.py via plan.whatsapp_number,
resolved by the caller before this adapter is constructed) — TikTok's native
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
from typing import Optional

import httpx

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
from .. import constants as C

COLLECTION = "jane_ads_tiktok_campaigns"

# TikTok's documented geo-target id for Nigeria (region-code lookup) — like Google's
# geoTargetConstants/2566, this is a value to confirm against a live
# /open_api/v1.3/tool/region/ call once real credentials exist; not guessable purely
# from public docs with certainty, flagged here rather than silently trusted.
_NIGERIA_LOCATION_ID = "10000541"

# TikTok's ad-group-level optimization/billing pair for a click-driving campaign —
# mirrors the "Maximise Clicks"-equivalent choice google.py made for the same reason
# (no conversion volume exists yet to train a smarter bidding strategy).
_OPTIMIZATION_GOAL = "CLICK"
_BILLING_EVENT = "CPC"

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

    async def launch_campaign(self, plan: CampaignPlan, auth: SpendAuthorization) -> LaunchResult:
        tiktok_plans = [p for p in plan.platforms if p.platform == Platform.TIKTOK]
        if not tiktok_plans:
            raise ValueError("TikTokAdsAdapter only handles Platform.TIKTOK plans")
        if not plan.whatsapp_number:
            raise ValueError("CampaignPlan.whatsapp_number is required — TikTok ads route to wa.me")
        if not plan.creative or not plan.creative.image_url or not plan.creative.is_video:
            # decision_engine.py's video-only gate (PRD C1) means a TikTok plan should
            # never reach here without real video creative — asserted explicitly
            # rather than trusted silently, same discipline as Meta's hard creative
            # requirement.
            raise ValueError("TikTok requires video creative (plan.creative.is_video)")

        platform_plan = tiktok_plans[0]
        total_budget_ngn = min(platform_plan.budget_ngn, auth.funded_amount_ngn)
        days = max(platform_plan.days, 1)

        now = datetime.now(timezone.utc)
        end = now + timedelta(days=days)
        wa_link = f"https://wa.me/{plan.whatsapp_number}"

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
                        "campaign_name": f"JaneAds-{plan.business_id}-{plan.goal.value}",
                        "objective_type": "TRAFFIC",
                        "budget_mode": "BUDGET_MODE_INFINITE",
                        "operation_status": "DISABLE",
                    },
                )
                campaign_data = campaign_resp.json()
                _raise_for_error(campaign_data, "campaign creation")
                campaign_id = str(campaign_data["data"]["campaign_id"])

                # 2. Video upload — UPLOAD_BY_URL lets TikTok fetch the hosted file
                # directly (same "server fetches it, no re-streaming needed here"
                # shape as Meta's /advideos file_url).
                video_resp = await client.post(
                    f"{self._api_base}/file/video/ad/upload/",
                    headers=self._headers(),
                    json={
                        "advertiser_id": self._advertiser_id,
                        "upload_type": "UPLOAD_BY_URL",
                        "video_url": plan.creative.image_url,
                        "file_name": f"jane-ads-{plan.business_id}-{uuid.uuid4().hex[:8]}.mp4",
                    },
                )
                video_data = video_resp.json()
                _raise_for_error(video_data, "video upload")
                video_id = video_data["data"][0]["video_id"] if isinstance(video_data["data"], list) else video_data["data"]["video_id"]

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
                        "operation_status": "DISABLE",
                    },
                )
                adgroup_data = adgroup_resp.json()
                _raise_for_error(adgroup_data, "ad group creation")
                adgroup_id = str(adgroup_data["data"]["adgroup_id"])

                # 4. The ad itself — video creative + copy + the wa.me landing page,
                # paused.
                ad_resp = await client.post(
                    f"{self._api_base}/ad/create/",
                    headers=self._headers(),
                    json={
                        "advertiser_id": self._advertiser_id,
                        "adgroup_id": adgroup_id,
                        "creatives": [{
                            "ad_name": f"JaneAds-{plan.business_id}-ad",
                            "ad_text": (plan.creative.primary_text or plan.creative.headline or "")[:100],
                            "video_id": video_id,
                            "landing_page_url": wa_link,
                            "call_to_action": "CONTACT_US",
                        }],
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
                    f"{self._api_base}/campaign/update/status/",
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
                f"{self._api_base}/campaign/update/status/",
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
                    f"{self._api_base}/adgroup/update/status/",
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

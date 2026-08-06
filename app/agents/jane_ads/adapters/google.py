"""
Jane + Ads — the Google Ads adapter (Phase 1: Search-only, REST via httpx).

Implements AdPlatformAdapter against Google Ads API's REST surface — NOT yet
verified against a live or Test Account (no Google Ads Manager Account, developer
token, or dedicated OAuth client exist yet; see google_ads_connection.py's module
docstring and the Phase-1 plan). Payload shapes below are hand-built against Google
Ads API's documented REST conventions and unit-tested via mocked httpx responses,
exactly how adapters/meta.py's own request shapes were proven correct before being
live-verified against a real Ad Account — this file should get the same "verified
end-to-end against..." header update once real credentials exist.

Deliberately Search-only for this phase: one Search campaign, one tightly-themed ad
group, phrase/exact-match keywords only (never broad — a micro-budget on broad match
spends money discovering what doesn't work), Maximise-Clicks bidding (no conversion
volume exists yet to train automated bidding), no Display network, no Performance Max.
Every campaign is created PAUSED, never ACTIVE — same hard product rule as Meta; a
human reviews and activates in Google Ads UI.

Destination is always a wa.me link (reusing whatsapp.py, already platform-agnostic —
confirmed no Meta imports). Call extensions and Lead form extensions are explicitly
out of scope for this phase — lead forms in particular carry real personal-data
handling obligations (deliver immediately to WhatsApp, never retain) that are a
meaningfully separate scope, not a default to build casually.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx
import openai

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

COLLECTION = "jane_ads_google_campaigns"

# Standing negative-keyword list (§3.3 of the source design doc) — filtered out of the
# model's own output only when the category itself legitimately IS one of these (e.g.
# a training business shouldn't negative "training").
_STANDING_NEGATIVES = {"jobs", "salary", "training", "free", "diy", "wholesale"}

# No live Keyword Plan API access yet (no credentials) — this is a clearly-flagged
# placeholder, not a real benchmark. Swap for a live estimate the moment
# cpc_estimator is wired to Google's keyword-idea endpoint; nothing else in this file
# needs to change when that happens (see estimate_clicks_per_day / launch_campaign's
# cpc_estimator parameter).
_DEFAULT_ESTIMATED_CPC_NGN = 250.0

_MIN_USEFUL_CLICKS_PER_DAY = 10.0


class GoogleAdsAPIError(Exception):
    """A Google Ads REST call returned an error payload, or the adapter is
    misconfigured."""

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code


class LowClicksWarning(Exception):
    """Raised by the clicks-per-day pre-flight (§3.4) instead of a generic
    ValueError, so callers can surface the three real alternatives (raise budget /
    narrow keywords / use Meta instead) rather than just blocking. Carries the
    numbers so the caller's message can be specific, not generic."""

    def __init__(self, daily_budget_ngn: float, estimated_cpc_ngn: float, estimated_clicks_per_day: float) -> None:
        super().__init__(
            f"At ₦{daily_budget_ngn:,.0f}/day and an estimated ₦{estimated_cpc_ngn:,.0f} "
            f"per click, this campaign would get about {estimated_clicks_per_day:.0f} "
            f"clicks a day — below the {_MIN_USEFUL_CLICKS_PER_DAY:.0f}/day needed to "
            f"actually learn what's working."
        )
        self.daily_budget_ngn = daily_budget_ngn
        self.estimated_cpc_ngn = estimated_cpc_ngn
        self.estimated_clicks_per_day = estimated_clicks_per_day


def _raise_for_error(data: dict, context: str) -> None:
    if "error" in data:
        err = data["error"]
        detail = err.get("message") or "unknown error"
        details = err.get("details") or []
        if details:
            inner = (details[0].get("errors") or [{}])[0]
            detail = inner.get("message") or detail
        raise GoogleAdsAPIError(f"{context}: {detail}", code=err.get("status"))


def estimate_clicks_per_day(daily_budget_ngn: float, estimated_cpc_ngn: float) -> float:
    """Pure function (§3.4) — never blocks on an unknown/zero CPC, since guessing
    wrong in the blocking direction is worse than not checking at all."""
    if estimated_cpc_ngn <= 0:
        return float("inf")
    return daily_budget_ngn / estimated_cpc_ngn


async def _call_keyword_model(prompt: str) -> Optional[dict]:
    """Mirrors creative.py's _call_ad_copy_model exactly — same client, same model,
    same JSON-object response format, same "return None on any failure, caller falls
    back" contract."""
    try:
        client = openai.AsyncOpenAI(api_key=settings.jane_ads_openai_key)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            timeout=15,
        )
        return json.loads(resp.choices[0].message.content or "{}")
    except Exception as e:
        print(f"[GoogleAdsAdapter] keyword generation error: {e}", flush=True)
        return None


def _validate_keywords(kw: dict, category: str) -> list[str]:
    """Deterministic completeness check, same philosophy as content_calendar_service's
    _validate_day — returns a list of human-readable failures; empty means usable."""
    issues: list[str] = []
    head_terms = kw.get("head_terms")
    if not isinstance(head_terms, list) or not head_terms:
        issues.append("head_terms must be a non-empty list of short (2-4 word) phrases")
    else:
        too_long = [t for t in head_terms if len(str(t).split()) > 6]
        if too_long:
            issues.append(f"{len(too_long)} head_term(s) are too long/broad for phrase match")

    negatives = kw.get("negatives")
    if not isinstance(negatives, list):
        issues.append("negatives must be a list")
    else:
        lower_negatives = {str(n).lower() for n in negatives}
        category_lower = category.lower()
        missing_standing = {
            n for n in _STANDING_NEGATIVES
            if n not in lower_negatives and n not in category_lower
        }
        if missing_standing:
            issues.append(f"missing standing negative keyword(s): {sorted(missing_standing)}")

    total_terms = (
        len(kw.get("head_terms") or []) + len(kw.get("geo_terms") or [])
        + len(kw.get("intent_terms") or [])
    )
    if total_terms > 20:
        issues.append(f"{total_terms} total keyword terms is too many for one tightly-themed ad group")

    return issues


async def translate_keywords(category: str, description: str, geo: str) -> dict:
    """The genuinely new work for Google (§3.3) — no existing scaffolding anywhere in
    jane_ads for keyword/match-type/negative translation (confirmed via repo-wide
    grep). AI-generates a structured JSON keyword set from the business's own
    description, validated deterministically, one retry with the specific failures
    listed back to the model, best-effort on the second attempt — same pattern already
    proven for the content calendar's per-day schema."""
    base_prompt = f"""You are a Google Ads search-campaign strategist for a small business.

Business category: {category or "not specified"}
What they do: {description or "not specified"}
Service area: {geo or "not specified"}

Generate a tightly-themed keyword set for ONE Google Search ad group. Return ONLY a
valid JSON object:
{{
  "head_terms": ["2-6 short (2-4 word) phrases for WHAT is sold — e.g. 'solar installation', 'AC repair'"],
  "geo_terms": ["service area + head term combined — e.g. 'plumber Surulere', 'AC repair Lekki' — empty list if no service area given"],
  "intent_terms": ["urgency/moment phrases — e.g. 'emergency plumber', 'solar installer near me', 'same day AC repair'"],
  "negatives": ["a standing list PLUS any category-specific negatives — always include jobs, salary, training, free, DIY, wholesale UNLESS the business itself IS one of those (e.g. a training business should not negative 'training')"]
}}

Rules:
- Every term must be phrase/exact-match appropriate — short, specific, no broad/generic single words
- geo_terms empty is fine if no service area was given; do not invent a location
- negatives must include the standing list terms literally, not paraphrased, unless genuinely inapplicable
"""
    correction_block = ""
    result: dict = {}
    for attempt in range(2):
        parsed = await _call_keyword_model(base_prompt + correction_block)
        if parsed is None:
            if attempt == 0:
                correction_block = "\n\nYour previous response could not be parsed as JSON. Return ONLY the JSON object, no other text."
                continue
            return {"head_terms": [], "geo_terms": [], "intent_terms": [], "negatives": sorted(_STANDING_NEGATIVES)}
        result = parsed
        issues = _validate_keywords(result, category)
        if not issues:
            break
        if attempt == 0:
            correction_block = (
                "\n\n=== FIX THESE SPECIFIC PROBLEMS FROM YOUR PREVIOUS ATTEMPT ===\n"
                + "; ".join(issues)
                + "\n==================================================================="
            )
    # Best-effort floor: the standing negatives are always present regardless of what
    # the model produced, even on a degraded/failed generation — a missing negative
    # keyword is a real cost risk, never worth leaving to chance.
    result["negatives"] = sorted(set(str(n).lower() for n in (result.get("negatives") or [])) | _STANDING_NEGATIVES)
    return result


class GoogleAdsAdapter(AdPlatformAdapter):
    """One instance per request/job. customer_id/login_customer_id/access_token are
    ALWAYS caller-supplied — resolved per-brand by
    google_ads_connection.resolve_customer_id_for_launch() before construction, never
    read from settings inside this class. Only the developer token and API version
    (legitimately global properties of URI's own Google Cloud project, not of any one
    brand) come from settings."""

    def __init__(
        self,
        db,
        customer_id: str,
        login_customer_id: str,
        access_token: str,
    ) -> None:
        self._db = db
        self._customer_id = customer_id
        self._login_customer_id = login_customer_id
        self._access_token = access_token
        self._api_base = f"https://googleads.googleapis.com/{settings.GOOGLE_ADS_API_VERSION}"
        if not self._customer_id:
            raise GoogleAdsAPIError("customer_id is required — resolve via resolve_customer_id_for_launch()")
        if not settings.GOOGLE_ADS_DEVELOPER_TOKEN:
            raise GoogleAdsAPIError("GOOGLE_ADS_DEVELOPER_TOKEN is not configured")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "developer-token": settings.GOOGLE_ADS_DEVELOPER_TOKEN,
            "login-customer-id": self._login_customer_id,
        }

    async def launch_campaign(
        self,
        plan: CampaignPlan,
        auth: SpendAuthorization,
        *,
        cpc_estimator: Optional[Callable[[list[str]], Awaitable[float]]] = None,
    ) -> LaunchResult:
        google_plans = [p for p in plan.platforms if p.platform == Platform.GOOGLE]
        if not google_plans:
            raise ValueError("GoogleAdsAdapter only handles Platform.GOOGLE plans")
        if not plan.whatsapp_number:
            raise ValueError("CampaignPlan.whatsapp_number is required — Google Search ads route to wa.me")
        # Deliberately NO check on plan.page_id (Meta-only concept) and NO check on
        # plan.creative — CreativeKind.NONE means a Search campaign needs no image;
        # Meta hard-requires plan.creative.image_url, Google does not (see the
        # ad-creation step below for the keyword-derived fallback headline path).

        platform_plan = google_plans[0]
        total_budget_ngn = min(platform_plan.budget_ngn, auth.funded_amount_ngn)
        days = max(platform_plan.days, 1)
        daily_budget_ngn = total_budget_ngn / days

        kw_context = plan.google_keyword_context or {}
        category = kw_context.get("category", "")
        description = kw_context.get("description", "")
        geo = kw_context.get("geo", "")

        keywords = await translate_keywords(category, description, geo)
        all_terms = (
            (keywords.get("head_terms") or [])
            + (keywords.get("geo_terms") or [])
            + (keywords.get("intent_terms") or [])
        )

        estimated_cpc_ngn = (
            await cpc_estimator(all_terms) if cpc_estimator else _DEFAULT_ESTIMATED_CPC_NGN
        )
        clicks_per_day = estimate_clicks_per_day(daily_budget_ngn, estimated_cpc_ngn)
        if clicks_per_day < _MIN_USEFUL_CLICKS_PER_DAY:
            raise LowClicksWarning(daily_budget_ngn, estimated_cpc_ngn, clicks_per_day)

        wa_link = f"https://wa.me/{plan.whatsapp_number}"

        campaign_resource_name = ""
        budget_resource_name = ""
        ad_group_resource_name = ""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # 1. Campaign budget (Google requires this as its own resource,
                # referenced by the campaign, not an inline field).
                budget_resp = await client.post(
                    f"{self._api_base}/customers/{self._customer_id}/campaignBudgets:mutate",
                    headers=self._headers(),
                    json={"operations": [{"create": {
                        "name": f"JaneAds-{plan.business_id}-budget-{uuid.uuid4().hex[:8]}",
                        "amountMicros": str(int(daily_budget_ngn * 1_000_000)),
                        "deliveryMethod": "STANDARD",
                    }}]},
                )
                budget_data = budget_resp.json()
                _raise_for_error(budget_data, "campaign budget creation")
                budget_resource_name = budget_data["results"][0]["resourceName"]

                # 2. Search campaign — PAUSED, Search network only, Maximise Clicks
                # bidding (TargetSpend), no Display, no Performance Max.
                campaign_resp = await client.post(
                    f"{self._api_base}/customers/{self._customer_id}/campaigns:mutate",
                    headers=self._headers(),
                    json={"operations": [{"create": {
                        "name": f"JaneAds-{plan.business_id}-{plan.goal.value}",
                        "status": "PAUSED",
                        "advertisingChannelType": "SEARCH",
                        "campaignBudget": budget_resource_name,
                        "targetSpend": {},
                        "networkSettings": {
                            "targetGoogleSearch": True,
                            "targetSearchNetwork": False,
                            "targetContentNetwork": False,
                            "targetPartnerSearchNetwork": False,
                        },
                    }}]},
                )
                campaign_data = campaign_resp.json()
                _raise_for_error(campaign_data, "campaign creation")
                campaign_resource_name = campaign_data["results"][0]["resourceName"]

                # 3. Geo targeting — Nigeria-scoped, matching Meta's ["NG"] default.
                # (Real per-brand geo targeting from plan.geo is a follow-up; Nigeria-
                # wide is the safe starting default, never narrower than intended.)
                geo_resp = await client.post(
                    f"{self._api_base}/customers/{self._customer_id}/campaignCriteria:mutate",
                    headers=self._headers(),
                    json={"operations": [{"create": {
                        "campaign": campaign_resource_name,
                        "location": {"geoTargetConstant": "geoTargetConstants/2566"},  # Nigeria
                    }}]},
                )
                _raise_for_error(geo_resp.json(), "geo targeting creation")

                # 4. One tightly-themed ad group.
                adgroup_resp = await client.post(
                    f"{self._api_base}/customers/{self._customer_id}/adGroups:mutate",
                    headers=self._headers(),
                    json={"operations": [{"create": {
                        "name": f"JaneAds-{plan.business_id}-adgroup",
                        "campaign": campaign_resource_name,
                        "status": "PAUSED",
                        "type": "SEARCH_STANDARD",
                    }}]},
                )
                adgroup_data = adgroup_resp.json()
                _raise_for_error(adgroup_data, "ad group creation")
                ad_group_resource_name = adgroup_data["results"][0]["resourceName"]

                # 5. Keywords as ad-group criteria — PHRASE/EXACT only, never BROAD,
                # plus negatives.
                keyword_ops = [
                    {"create": {
                        "adGroup": ad_group_resource_name,
                        "status": "ENABLED",
                        "keyword": {"text": term, "matchType": "PHRASE"},
                    }}
                    for term in all_terms
                ] + [
                    {"create": {
                        "adGroup": ad_group_resource_name,
                        "negative": True,
                        "keyword": {"text": neg, "matchType": "EXACT"},
                    }}
                    for neg in (keywords.get("negatives") or [])
                ]
                if keyword_ops:
                    kw_resp = await client.post(
                        f"{self._api_base}/customers/{self._customer_id}/adGroupCriteria:mutate",
                        headers=self._headers(),
                        json={"operations": keyword_ops},
                    )
                    _raise_for_error(kw_resp.json(), "keyword criteria creation")

                # 6. One Responsive Search Ad. Falls back to keyword-derived generic
                # headlines when no creative was generated (CreativeKind.NONE path) —
                # unlike Meta, this never hard-requires plan.creative.
                if plan.creative and plan.creative.headline:
                    headlines = [plan.creative.headline]
                    descriptions = [plan.creative.primary_text or plan.creative.headline]
                else:
                    headlines = [t.title() for t in (keywords.get("head_terms") or ["Chat With Us"])[:3]]
                    descriptions = ["Message us on WhatsApp to get started today."]
                ad_resp = await client.post(
                    f"{self._api_base}/customers/{self._customer_id}/adGroupAds:mutate",
                    headers=self._headers(),
                    json={"operations": [{"create": {
                        "adGroup": ad_group_resource_name,
                        "status": "PAUSED",
                        "ad": {
                            "finalUrls": [wa_link],
                            "responsiveSearchAd": {
                                "headlines": [{"text": h[:30]} for h in headlines],
                                "descriptions": [{"text": d[:90]} for d in descriptions],
                            },
                        },
                    }}]},
                )
                ad_data = ad_resp.json()
                _raise_for_error(ad_data, "ad creation")
                ad_resource_name = ad_data["results"][0]["resourceName"]
        except Exception:
            await self._rollback_partial_launch(campaign_resource_name)
            raise

        campaign_id = campaign_resource_name.split("/")[-1] if campaign_resource_name else ""
        # adGroupAd resource names are composite ("customers/.../adGroupAds/{adGroupId}~{adId}") —
        # the ad's own id is only the part after the "~", not the whole trailing segment.
        ad_id = ad_resource_name.split("/")[-1].split("~")[-1] if ad_resource_name else ""

        await self._db[COLLECTION].update_one(
            {"campaign_id": campaign_id},
            {"$set": {
                "campaign_id": campaign_id,
                "campaign_resource_name": campaign_resource_name,
                "ad_group_id": ad_group_resource_name,
                "ad_id": ad_id,
                "business_id": plan.business_id,
                "customer_id": self._customer_id,
                "platform": "google",
                "last_click_count": 0,
                "created_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )

        return LaunchResult(
            campaign_id=campaign_id,
            ad_ids={plan.business_id: ad_id},
            platforms=[Platform.GOOGLE],
            launched=True,
        )

    async def _rollback_partial_launch(self, campaign_resource_name: str) -> None:
        """Undo a launch that failed midway. Google Ads has no hard delete via API —
        the correct rollback primitive is setting the campaign's status to REMOVED
        (which cascades to its ad group/ads), distinct from Meta's real DELETE.
        Strictly best-effort and never raises — the caller is already unwinding a real
        failure; a cleanup problem must never mask the ORIGINAL error."""
        if not campaign_resource_name:
            return
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{self._api_base}/customers/{self._customer_id}/campaigns:mutate",
                    headers=self._headers(),
                    json={"operations": [{
                        "update": {"resourceName": campaign_resource_name, "status": "REMOVED"},
                        "updateMask": "status",
                    }]},
                )
                data = resp.json()
                if "error" in data:
                    print(f"[GoogleAdsAdapter] ORPHANED campaign {campaign_resource_name} — "
                          f"rollback rejected: {data['error'].get('message')}", flush=True)
                else:
                    print(f"[GoogleAdsAdapter] rolled back partial launch: removed {campaign_resource_name}", flush=True)
        except Exception as e:
            print(f"[GoogleAdsAdapter] ORPHANED campaign {campaign_resource_name} — rollback failed: {e}", flush=True)

    async def _get_campaign_record(self, campaign_id: str) -> dict:
        record = await self._db[COLLECTION].find_one({"campaign_id": campaign_id})
        if not record:
            raise GoogleAdsAPIError(
                f"No stored record for campaign_id={campaign_id} — was it launched via this adapter?"
            )
        return record

    async def _gaql_search(self, query: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._api_base}/customers/{self._customer_id}/googleAds:search",
                headers=self._headers(),
                json={"query": query},
            )
        data = resp.json()
        _raise_for_error(data, "GAQL search")
        return data.get("results", [])

    async def fetch_per_ad_spend(self, campaign_id: str) -> list[PerAdSpend]:
        """Current CUMULATIVE spend per ad (matches the interface contract). Reads the
        account's own currency rather than assuming NGN — converts via
        constants.USD_TO_NGN if USD, passes through if already NGN, logs loudly for
        anything else (no safe guess possible without a live FX source)."""
        record = await self._get_campaign_record(campaign_id)
        rows = await self._gaql_search(
            f"SELECT ad_group_ad.ad.id, metrics.cost_micros, customer.currency_code "
            f"FROM ad_group_ad WHERE campaign.id = {campaign_id}"
        )
        now = datetime.now(timezone.utc)
        if not rows:
            return [PerAdSpend(
                business_id=record["business_id"], ad_id=record["ad_id"],
                campaign_id=campaign_id, platform=Platform.GOOGLE, spend_ngn=0.0, at=now,
            )]

        def _to_ngn(cost_micros: float, currency: str) -> float:
            amount = cost_micros / 1_000_000
            if currency in ("", "NGN"):
                return amount
            if currency == "USD":
                return amount * C.USD_TO_NGN
            print(f"[GoogleAdsAdapter] unhandled account currency {currency!r} — "
                  f"returning un-converted amount, verify manually", flush=True)
            return amount

        return [
            PerAdSpend(
                business_id=record["business_id"],
                ad_id=row.get("adGroupAd", {}).get("ad", {}).get("id", record["ad_id"]),
                campaign_id=campaign_id,
                platform=Platform.GOOGLE,
                spend_ngn=_to_ngn(
                    float(row.get("metrics", {}).get("costMicros", 0)),
                    row.get("customer", {}).get("currencyCode", ""),
                ),
                at=now,
            )
            for row in rows
        ]

    async def poll_conversations(self, campaign_id: str) -> list[ConversationDelivered]:
        """Google has no 'conversation started' concept — maps a CLICK (not an
        impression, not a conversion needing a pixel we don't control) to
        ConversationDelivered, same delta-since-last-poll discipline as Meta
        (last_click_count stored per campaign, mirroring Meta's
        last_conversation_count). A click is a WEAKER signal than Meta's real started-
        WhatsApp-thread metric — whether/how this gets billed is a billing.py decision,
        out of scope here; this method only produces the event."""
        record = await self._get_campaign_record(campaign_id)
        rows = await self._gaql_search(
            f"SELECT metrics.clicks, metrics.cost_micros FROM campaign WHERE campaign.id = {campaign_id}"
        )
        if not rows:
            return []
        row = rows[0]
        total_clicks = int(row.get("metrics", {}).get("clicks", 0))
        cost_micros = float(row.get("metrics", {}).get("costMicros", 0))

        already_seen = int(record.get("last_click_count", 0))
        new_count = max(total_clicks - already_seen, 0)
        if new_count == 0:
            return []

        await self._db[COLLECTION].update_one(
            {"campaign_id": campaign_id},
            {"$set": {"last_click_count": total_clicks}},
        )

        spend_ngn = cost_micros / 1_000_000
        cost_per_click = (spend_ngn / total_clicks) if total_clicks else 0.0
        now = datetime.now(timezone.utc)
        return [
            ConversationDelivered(
                business_id=record["business_id"],
                ad_id=record["ad_id"],
                campaign_id=campaign_id,
                platform=Platform.GOOGLE,
                at=now,
                charge_ngn=cost_per_click,
            )
            for _ in range(new_count)
        ]

    async def pause_ad(self, campaign_id: str, ad_id: str) -> bool:
        """Google's REST mutate requires an explicit updateMask field list, unlike
        Meta's implicit-merge POST — a real, documented difference."""
        record = await self._get_campaign_record(campaign_id)
        ad_resource_name = f"customers/{self._customer_id}/adGroupAds/{record.get('ad_group_id', '').split('/')[-1]}~{ad_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self._api_base}/customers/{self._customer_id}/adGroupAds:mutate",
                headers=self._headers(),
                json={"operations": [{
                    "update": {"resourceName": ad_resource_name, "status": "PAUSED"},
                    "updateMask": "status",
                }]},
            )
        data = resp.json()
        _raise_for_error(data, "pause ad")
        return bool(data.get("results"))

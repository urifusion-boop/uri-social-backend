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

Destination is whatever the brand chose (destination.py: their WhatsApp chat, their
website, or their Instagram DMs) — all three resolve to one plain link, which is all
a Search ad's finalUrls needs. Call extensions and Lead form extensions are explicitly
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

COLLECTION = "jane_ads_google_campaigns"

_NIGERIA = "geoTargetConstants/2566"
# Google campaign.status -> the labels the campaign list already uses for Meta/TikTok.
_DELIVERY_LABELS = {"ENABLED": "Active", "PAUSED": "Paused", "REMOVED": "Deleted"}

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


_FILLER_HEADLINES = ("Chat With Us On WhatsApp", "Get A Quick Reply Today", "Message Us Now")
_FILLER_DESCRIPTIONS = ("Message us on WhatsApp to get started today.",
                        "Friendly, fast replies. Tell us what you need and we will help.")


def _fit(text: str, limit: int) -> str:
    """Trim to Google's limit on a word boundary, never mid-word."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit + 1].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    return cut or text[:limit]


def rsa_assets(headlines: list, descriptions: list, keywords: dict) -> tuple:
    """Headlines and descriptions Google will accept for a Responsive Search Ad:
    at least 3 headlines (max 30 characters each) and 2 descriptions (max 90),
    all different. One headline and one description used to be sent whenever
    Jane had written the ad, which Google rejects outright. Jane's own lines
    stay first; the rest are filled from the keywords, then from neutral lines."""
    def unique(items, limit):
        out, seen = [], set()
        for item in items:
            text = _fit(item, limit)
            if text and text.lower() not in seen:
                seen.add(text.lower())
                out.append(text)
        return out

    terms = [t.title() for t in (keywords.get("head_terms") or []) + (keywords.get("geo_terms") or [])]
    heads = unique(list(headlines) + terms + list(_FILLER_HEADLINES), 30)[:15]
    descs = unique(list(descriptions) + list(_FILLER_DESCRIPTIONS), 90)[:4]
    return heads, descs


def to_ngn(amount: float, currency_code: str) -> float:
    """An amount in the Ads account's currency, in naira. NGN passes through,
    USD converts at constants.USD_TO_NGN; anything else is logged loudly and
    returned as-is (no safe guess without a live FX source)."""
    currency = (currency_code or "").upper()
    if currency in ("", "NGN"):
        return amount
    if currency == "USD":
        return amount * C.USD_TO_NGN
    print(f"[GoogleAdsAdapter] unhandled account currency {currency!r} — "
          f"returning un-converted amount, verify manually", flush=True)
    return amount


def budget_micros(daily_budget_ngn: float, currency_code: str) -> str:
    """The daily budget in the ACCOUNT's currency, as Google's micros string.

    Plans are in naira; Google reads amountMicros in whatever currency the Ads
    account is set to. This used to send the naira figure as-is, so a ₦20,000
    a day plan on a US-dollar account would have been created as $20,000 a day
    (found when the first real account linked on dev turned out to be USD).
    NGN passes through, USD converts at constants.USD_TO_NGN, and any other or
    unknown currency refuses to launch — there is no safe guess with money.
    Rounded to whole cents/kobo, as Google requires."""
    currency = (currency_code or "").upper()
    if currency == "NGN":
        amount = daily_budget_ngn
    elif currency == "USD":
        amount = daily_budget_ngn / C.USD_TO_NGN
    elif not currency:
        raise GoogleAdsAPIError(
            "The Google Ads account's currency isn't known yet, so a budget can't be set safely. "
            "Press Check again on the Google Ads connection, then retry."
        )
    else:
        raise GoogleAdsAPIError(
            f"This Google Ads account is in {currency}. Budgets can only be set on NGN or USD accounts for now."
        )
    cents = int(round(amount * 100))
    if cents < 1:
        raise GoogleAdsAPIError("That daily budget is too small for this Google Ads account's currency.")
    return str(cents * 10_000)


def _raise_for_error(data: dict, context: str) -> None:
    if "error" in data:
        err = data["error"]
        detail = err.get("message") or "unknown error"
        details = err.get("details") or []
        if details:
            inner = (details[0].get("errors") or [{}])[0]
            detail = inner.get("message") or detail
            # Say WHICH field: "The required field was not present" alone is undiagnosable.
            path = [e.get("fieldName", "") for e in
                    ((inner.get("location") or {}).get("fieldPathElements") or []) if e.get("fieldName")]
            if path:
                detail = f"{detail} (field: {'.'.join(path)})"
            # Google says what the per-day minimum is: keep it, so the owner is told the number.
            minimum = ((inner.get("details") or {}).get("budgetPerDayMinimumErrorDetails") or {})
            if minimum.get("minimumBudgetAmountMicros"):
                detail = (f"{detail} (minimum {float(minimum['minimumBudgetAmountMicros']) / 1_000_000:g} "
                          f"{minimum.get('currencyCode', '')} a day)")
            code = inner.get("errorCode") or {}
            if isinstance(code, dict) and code:
                detail = f"{detail} [{next(iter(code.values()))}]"
        raise GoogleAdsAPIError(f"{context}: {detail}", code=err.get("status"))


def campaign_name(business: str, placement_label: str, goal: str = "", today: Optional["date"] = None) -> str:
    """A name a person can read in Google Ads: "Mama Kitchen · Search · Leads ·
    14 Oct 2026 · 3f2a". Google needs campaign names unique in the account, so a
    short tag ends it; the old names were codes (JaneAds-brnd_personal_1f38…-leads)."""
    from datetime import date
    day = today or date.today()
    business = " ".join((business or "").split())[:40].strip() or "URI campaign"
    parts = [business, placement_label]
    if goal:
        parts.append(goal.replace("_", " ").title())
    parts += [f"{day.day} {day.strftime('%b %Y')}", uuid.uuid4().hex[:4]]
    return " · ".join(parts)


def campaign_end(days: int, today: Optional["date"] = None) -> str:
    """Google's end_date_time for a campaign running `days` days from today
    (today counts as day one), in the format Google takes."""
    from datetime import date, timedelta
    start = today or date.today()
    return f"{(start + timedelta(days=max(days, 1) - 1)).isoformat()} 23:59:59"


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
        currency_code: str = "",
    ) -> None:
        self._db = db
        self._customer_id = customer_id
        self._login_customer_id = login_customer_id
        self._access_token = access_token
        # The Ads account's own currency (from the connection, read from Google
        # when the link is checked). Budgets are sent in THIS currency.
        self._currency_code = (currency_code or "").upper()
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
        dest_link = link_for_plan(plan).link
        if not dest_link:
            raise ValueError(
                f"CampaignPlan has no usable destination link for destination_type="
                f"'{plan.destination_type}' — a Search ad needs a final URL"
            )
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

        from .google_visual import VISUAL_PLACEMENTS, normalize_placement
        placement = normalize_placement(kw_context.get("placement"))
        if placement in VISUAL_PLACEMENTS:
            return await self._launch_visual(plan, placement, daily_budget_ngn, dest_link, kw_context, days)

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
        if clicks_per_day < _MIN_USEFUL_CLICKS_PER_DAY and not kw_context.get("low_budget_ok"):
            raise LowClicksWarning(daily_budget_ngn, estimated_cpc_ngn, clicks_per_day)

        readable_name = campaign_name(kw_context.get("business_name") or category, "Search", plan.goal.value)
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
                        "name": f"{readable_name} · budget",
                        "amountMicros": budget_micros(daily_budget_ngn, self._currency_code),
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
                        "name": readable_name,
                        "status": "PAUSED",
                        "advertisingChannelType": "SEARCH",
                        "campaignBudget": budget_resource_name,
                        "targetSpend": {},
                        # Mandatory on every new campaign in current API versions
                        # (Google's EU political-ads rules) — without it the create
                        # fails with a bare "required field was not present"
                        # (hit on the first real launch against a live account).
                        "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
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

                # 3. Geo targeting — the plan's own city/state when Google knows it
                # (see _resolve_geo), otherwise Nigeria. Every campaign used to target
                # the whole country, so a Lagos-only business paid for clicks from
                # everywhere.
                geo_constant = await self._resolve_geo(geo)
                geo_resp = await client.post(
                    f"{self._api_base}/customers/{self._customer_id}/campaignCriteria:mutate",
                    headers=self._headers(),
                    json={"operations": [{"create": {
                        "campaign": campaign_resource_name,
                        "location": {"geoTargetConstant": geo_constant},
                    }}]},
                )
                _raise_for_error(geo_resp.json(), "geo targeting creation")

                # 4. One tightly-themed ad group.
                adgroup_resp = await client.post(
                    f"{self._api_base}/customers/{self._customer_id}/adGroups:mutate",
                    headers=self._headers(),
                    json={"operations": [{"create": {
                        "name": f"{readable_name} · ad group",
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
                headlines, descriptions = rsa_assets(headlines, descriptions, keywords)
                ad_resp = await client.post(
                    f"{self._api_base}/customers/{self._customer_id}/adGroupAds:mutate",
                    headers=self._headers(),
                    json={"operations": [{"create": {
                        "adGroup": ad_group_resource_name,
                        "status": "PAUSED",
                        "ad": {
                            "finalUrls": [dest_link],
                            "responsiveSearchAd": {
                                "headlines": [{"text": h} for h in headlines],
                                "descriptions": [{"text": d} for d in descriptions],
                            },
                        },
                    }}]},
                )
                ad_data = ad_resp.json()
                _raise_for_error(ad_data, "ad creation")
                ad_resource_name = ad_data["results"][0]["resourceName"]
        except Exception:
            await self._rollback_partial_launch(campaign_resource_name, budget_resource_name)
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
                "login_customer_id": self._login_customer_id,
                "currency_code": self._currency_code,
                "budget_resource_name": budget_resource_name,
                "geo_target": geo_constant,
                "placement": "search",
                "days": days,
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

    async def _launch_visual(self, plan: CampaignPlan, placement: str, daily_budget_ngn: float,
                             dest_link: str, kw_context: dict, days: int = 0) -> LaunchResult:
        """Display, YouTube & Discover, or Everywhere: one atomic request (see
        google_visual), so a failure leaves nothing behind in the account."""
        from . import google_visual as gv

        creative = plan.creative
        if not creative or not creative.image_url or creative.is_video:
            raise GoogleAdsAPIError(
                f"{gv.PLACEMENT_LABELS[placement]} ads need a picture. Add or generate an image, then try again.")
        image = await gv.fetch_bytes(creative.image_url)
        if not image:
            raise GoogleAdsAPIError("The ad's picture couldn't be loaded. Try again, or upload it again.")
        business_name = kw_context.get("business_name") or kw_context.get("category") or "Our business"
        try:
            images = gv.marketing_images(image)
        except Exception as e:
            raise GoogleAdsAPIError(f"The ad's picture couldn't be prepared for Google: {e}")
        logo_b64 = gv.logo_image(await gv.fetch_bytes(kw_context.get("logo_url", "")), business_name,
                                 kw_context.get("brand_color") or "#C2185B")
        copy = gv.ad_copy(creative.headline, creative.primary_text, business_name, rsa_assets,
                          headline_limit=40 if placement == "demand_gen" else 30)
        geo_constant = await self._resolve_geo(kw_context.get("geo", ""))
        short = {"display": "Display", "demand_gen": "YouTube & Discover", "pmax": "Everywhere"}[placement]
        name = campaign_name(business_name, short, plan.goal.value)
        ops = gv.build_operations(
            placement, self._customer_id, name=name,
            daily_micros=budget_micros(daily_budget_ngn, self._currency_code),
            geo_constant=geo_constant, final_url=dest_link, copy=copy, images=images, logo_b64=logo_b64,
        )
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(
                f"{self._api_base}/customers/{self._customer_id}/googleAds:mutate",
                headers=self._headers(),
                json={"mutateOperations": ops},
            )
        data = resp.json()
        _raise_for_error(data, f"{gv.PLACEMENT_LABELS[placement]} campaign creation")
        made = gv.created(data.get("mutateOperationResponses") or [])
        campaign_resource_name = made.get("campaign", "")
        campaign_id = campaign_resource_name.split("/")[-1]
        ad_group = made.get("adGroup", "")
        ad_id = made.get("adGroupAd", "").split("~")[-1] if made.get("adGroupAd") else ""

        await self._db[COLLECTION].update_one(
            {"campaign_id": campaign_id},
            {"$set": {
                "campaign_id": campaign_id,
                "campaign_resource_name": campaign_resource_name,
                "ad_group_id": ad_group,
                "ad_id": ad_id,
                "asset_group": made.get("assetGroup", ""),
                "placement": placement,
                "days": days,
                "business_id": plan.business_id,
                "customer_id": self._customer_id,
                "login_customer_id": self._login_customer_id,
                "currency_code": self._currency_code,
                "budget_resource_name": made.get("campaignBudget", ""),
                "geo_target": geo_constant,
                "platform": "google",
                "last_click_count": 0,
                "created_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
        return LaunchResult(campaign_id=campaign_id, ad_ids={plan.business_id: ad_id},
                            platforms=[Platform.GOOGLE], launched=True)

    async def _resolve_geo(self, geo: str) -> str:
        """Google's location id for the plan's place ("Lagos", "Ikeja, Lagos",
        "Abuja"...), from Google's own location search limited to Nigeria.
        Falls back to the whole country when there's no place, no match or the
        lookup fails — never blocks a launch."""
        place = (geo or "").split(",")[0].strip()
        if not place or place.lower() in ("nigeria", "ng", "nationwide", "all of nigeria"):
            return _NIGERIA
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{self._api_base}/geoTargetConstants:suggest",
                    headers=self._headers(),
                    json={"locale": "en", "countryCode": "NG", "locationNames": {"names": [place]}},
                )
            data = resp.json()
            for suggestion in (data.get("geoTargetConstantSuggestions") or []):
                constant = suggestion.get("geoTargetConstant") or {}
                if constant.get("status", "ENABLED") == "ENABLED" and constant.get("resourceName"):
                    return constant["resourceName"]
        except Exception as e:
            print(f"[GoogleAdsAdapter] location lookup for {place!r} failed, using Nigeria: {e}", flush=True)
        return _NIGERIA

    async def fetch_campaign_summary(self, campaign_id: str) -> dict:
        """One snapshot for the campaign list and the hourly spend sweep — the same
        dict shape MetaAdPlatformAdapter/TikTokAdsAdapter return, which billing.py
        and the campaign list read directly. Spend comes back in the account's
        currency and is converted to naira. A click stands in for a conversation
        (Google has no "conversation started" signal), as poll_conversations says.

        Also carries Google's own verdict on whether it is serving
        (primary_status + reasons) and whether the account can pay for ads, so
        the list can say WHY a turned-on campaign isn't showing."""
        cid = "".join(ch for ch in str(campaign_id) if ch.isdigit())
        # end_date_time, not end_date: Google removed end_date in API v23.
        rows = await self._gaql_search(
            "SELECT campaign.status, campaign.end_date_time, campaign.primary_status, "
            "campaign.primary_status_reasons, customer.currency_code, metrics.cost_micros, "
            f"metrics.impressions, metrics.clicks FROM campaign WHERE campaign.id = {cid}"
        )
        if not rows:
            return {"delivery": "Deleted", "spend_ngn": 0.0, "impressions": 0, "reach": 0,
                    "conversations": 0, "cost_per_conversation_ngn": None, "ends_at": None}
        row = rows[0]
        campaign = row.get("campaign", {})
        currency = row.get("customer", {}).get("currencyCode", "") or self._currency_code
        metrics = row.get("metrics", {})
        clicks = int(metrics.get("clicks", 0))
        spend_ngn = to_ngn(float(metrics.get("costMicros", 0)) / 1_000_000, currency)
        impressions = int(metrics.get("impressions", 0))
        ends = (campaign.get("endDateTime") or "")[:10] or None
        return {
            "delivery": _DELIVERY_LABELS.get(campaign.get("status", ""), "Paused"),
            "spend_ngn": spend_ngn,
            "impressions": impressions,
            "reach": impressions,          # Search reports impressions, not unique reach
            "conversations": clicks,
            "cost_per_conversation_ngn": (spend_ngn / clicks) if clicks else None,
            "ends_at": None if not ends or ends.startswith("2037") else ends,
            "primary_status": campaign.get("primaryStatus", ""),
            "primary_status_reasons": list(campaign.get("primaryStatusReasons") or []),
            "billing_ready": await self.billing_ready(),
        }

    async def billing_ready(self) -> Optional[bool]:
        """Has this Google Ads account got an approved way to pay for ads?
        Without one, Google shows nothing however the campaign is set. None when
        it can't be read (never blocks anything on a failed read)."""
        try:
            rows = await self._gaql_search("SELECT billing_setup.status FROM billing_setup")
        except Exception as e:
            print(f"[google] billing read failed for {self._customer_id}: {e}", flush=True)
            return None
        return any(r.get("billingSetup", {}).get("status") in ("APPROVED", "APPROVED_HELD") for r in rows)

    async def set_delivery(self, campaign_id: str, active: bool) -> dict:
        """Turn a campaign on or off from the campaign list. Like Meta and TikTok,
        Google only delivers when the campaign, its ad group AND its ad are all
        enabled — everything is created paused — so the same status is set on all
        three. Going active is the one consequential action: budget can start
        being spent from then on."""
        record = await self._get_campaign_record(campaign_id)
        status = "ENABLED" if active else "PAUSED"
        cid = "".join(ch for ch in str(campaign_id) if ch.isdigit())
        ad_group = record.get("ad_group_id") or ""
        ad_id = record.get("ad_id") or ""
        targets = [("campaign", "campaigns", f"customers/{self._customer_id}/campaigns/{cid}")]
        if ad_group:
            targets.append(("ad_group", "adGroups", ad_group))
            if ad_id:
                targets.append(("ad", "adGroupAds",
                                f"customers/{self._customer_id}/adGroupAds/{ad_group.split('/')[-1]}~{ad_id}"))
        updated: dict = {}
        async with httpx.AsyncClient(timeout=30) as client:
            for name, service, resource in targets:
                update, mask = {"resourceName": resource, "status": status}, "status"
                if name == "campaign" and active and record.get("days"):
                    # The plan's days count from when it's switched on, not from
                    # when it was created paused: Google stops it on the last day.
                    update["endDateTime"] = campaign_end(int(record["days"]))
                    mask = "status,end_date_time"
                resp = await client.post(
                    f"{self._api_base}/customers/{self._customer_id}/{service}:mutate",
                    headers=self._headers(),
                    json={"operations": [{"update": update, "updateMask": mask}]},
                )
                data = resp.json()
                _raise_for_error(data, f"{name} status update")
                updated[name] = bool(data.get("results"))
        return {"status": status, "updated": updated}

    async def delete_campaign(self, campaign_id: str) -> bool:
        """Permanently remove the campaign (and the budget it used) from the
        client's Google Ads account. Raises on failure — a delete the caller
        asked for must not fail silently."""
        result = await self.remove_campaign(campaign_id)
        return bool(result.get("removed"))

    async def _remove_budget(self, budget_resource_name: str) -> None:
        """Best-effort removal of a budget a failed launch left behind (it costs
        nothing on its own, but it clutters the client's account). Never raises."""
        if not budget_resource_name:
            return
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{self._api_base}/customers/{self._customer_id}/campaignBudgets:mutate",
                    headers=self._headers(),
                    json={"operations": [{"remove": budget_resource_name}]},
                )
                data = resp.json()
                if isinstance(data, dict) and "error" in data:
                    print(f"[GoogleAdsAdapter] leftover budget {budget_resource_name}: "
                          f"{data['error'].get('message')}", flush=True)
        except Exception as e:
            print(f"[GoogleAdsAdapter] leftover budget {budget_resource_name}: {e}", flush=True)

    async def _rollback_partial_launch(self, campaign_resource_name: str, budget_resource_name: str = "") -> None:
        """Undo a launch that failed midway. Google Ads has no hard delete via API —
        the correct rollback primitive is setting the campaign's status to REMOVED
        (which cascades to its ad group/ads), distinct from Meta's real DELETE.
        Strictly best-effort and never raises — the caller is already unwinding a real
        failure; a cleanup problem must never mask the ORIGINAL error."""
        if not campaign_resource_name:
            # Failed before a campaign existed — only the budget was created.
            await self._remove_budget(budget_resource_name)
            return
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{self._api_base}/customers/{self._customer_id}/campaigns:mutate",
                    headers=self._headers(),
                    # A real "remove" operation. Setting status=REMOVED through an
                    # update is rejected by Google (INVALID_ENUM_VALUE, seen live), so
                    # this rollback never actually removed anything before.
                    json={"operations": [{"remove": campaign_resource_name}]},
                )
                data = resp.json()
                if "error" in data:
                    print(f"[GoogleAdsAdapter] ORPHANED campaign {campaign_resource_name} — "
                          f"rollback rejected: {data['error'].get('message')}", flush=True)
                else:
                    print(f"[GoogleAdsAdapter] rolled back partial launch: removed {campaign_resource_name}", flush=True)
                    await self._remove_budget(budget_resource_name)
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

    async def describe_campaign(self, campaign_id: str) -> dict:
        """What Google actually holds for a campaign we created: status, daily
        budget (in the account's currency and in naira), the ad and its status,
        keywords and location targeting. For verifying a launch end to end."""
        cid = "".join(ch for ch in str(campaign_id) if ch.isdigit())
        camp = await self._gaql_search(
            "SELECT campaign.id, campaign.name, campaign.status, campaign.advertising_channel_type, "
            "campaign.campaign_budget, campaign_budget.amount_micros, customer.currency_code, "
            f"metrics.clicks, metrics.impressions, metrics.cost_micros FROM campaign WHERE campaign.id = {cid}"
        )
        if not camp:
            raise GoogleAdsAPIError(f"campaign {cid} was not found in this Google Ads account")
        row = camp[0]
        currency = row.get("customer", {}).get("currencyCode", "") or self._currency_code
        daily = float(row.get("campaignBudget", {}).get("amountMicros", 0)) / 1_000_000
        ads = await self._gaql_search(
            "SELECT ad_group.status, ad_group_ad.status, ad_group_ad.ad.id, ad_group_ad.ad.final_urls, "
            "ad_group_ad.ad.responsive_search_ad.headlines, ad_group_ad.ad.responsive_search_ad.descriptions, "
            f"ad_group_ad.policy_summary.approval_status FROM ad_group_ad WHERE campaign.id = {cid}"
        )
        kws = await self._gaql_search(
            "SELECT ad_group_criterion.keyword.text, ad_group_criterion.keyword.match_type, "
            f"ad_group_criterion.negative FROM ad_group_criterion WHERE campaign.id = {cid} "
            "AND ad_group_criterion.type = 'KEYWORD'"
        )
        geo = await self._gaql_search(
            "SELECT campaign_criterion.location.geo_target_constant, campaign_criterion.negative "
            f"FROM campaign_criterion WHERE campaign.id = {cid} AND campaign_criterion.type = 'LOCATION'"
        )
        ad_list = []
        for a in ads:
            ad = a.get("adGroupAd", {})
            rsa = ad.get("ad", {}).get("responsiveSearchAd", {})
            ad_list.append({
                "ad_id": ad.get("ad", {}).get("id"),
                "status": ad.get("status"),
                "ad_group_status": a.get("adGroup", {}).get("status"),
                "approval": (ad.get("policySummary") or {}).get("approvalStatus"),
                "final_urls": ad.get("ad", {}).get("finalUrls") or [],
                "headlines": [h.get("text") for h in rsa.get("headlines") or []],
                "descriptions": [d.get("text") for d in rsa.get("descriptions") or []],
            })
        keywords = [k.get("adGroupCriterion", {}) for k in kws]
        return {
            "campaign_id": cid,
            "name": row.get("campaign", {}).get("name"),
            "status": row.get("campaign", {}).get("status"),
            "channel": row.get("campaign", {}).get("advertisingChannelType"),
            "budget_resource": row.get("campaign", {}).get("campaignBudget"),
            "currency_code": currency,
            "daily_budget": daily,
            "daily_budget_ngn": round(to_ngn(daily, currency), 2),
            "clicks": int(row.get("metrics", {}).get("clicks", 0)),
            "impressions": int(row.get("metrics", {}).get("impressions", 0)),
            "spend_ngn": round(to_ngn(float(row.get("metrics", {}).get("costMicros", 0)) / 1_000_000, currency), 2),
            "ads": ad_list,
            "keywords": [k.get("keyword", {}).get("text") for k in keywords if not k.get("negative")],
            "negative_keywords": [k.get("keyword", {}).get("text") for k in keywords if k.get("negative")],
            "locations": [g.get("campaignCriterion", {}).get("location", {}).get("geoTargetConstant") for g in geo],
        }

    async def remove_campaign(self, campaign_id: str) -> dict:
        """Remove a campaign we created (Google's REMOVED status — it cascades to
        the ad group and ads) and the budget it used."""
        info = await self.describe_campaign(campaign_id)
        resource = f"customers/{self._customer_id}/campaigns/{info['campaign_id']}"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                f"{self._api_base}/customers/{self._customer_id}/campaigns:mutate",
                headers=self._headers(),
                json={"operations": [{"remove": resource}]},
            )
        _raise_for_error(resp.json(), "remove campaign")
        await self._remove_budget(info.get("budget_resource") or "")
        await self._db[COLLECTION].update_one(
            {"campaign_id": info["campaign_id"]},
            {"$set": {"removed_at": datetime.now(timezone.utc)}},
        )
        return {"removed": True, "campaign_id": info["campaign_id"]}

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
            return to_ngn(cost_micros / 1_000_000, currency or self._currency_code)

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
            f"SELECT metrics.clicks, metrics.cost_micros, customer.currency_code "
            f"FROM campaign WHERE campaign.id = {campaign_id}"
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

        # Google reports cost in the ACCOUNT's currency. This used to be read as
        # naira, so a $0.50 click on a dollar account was charged as ₦0.50.
        currency = row.get("customer", {}).get("currencyCode", "") or self._currency_code
        spend_ngn = to_ngn(cost_micros / 1_000_000, currency)
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

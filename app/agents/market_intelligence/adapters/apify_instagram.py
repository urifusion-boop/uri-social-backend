"""
Uri Market Intelligence — Instagram source adapter, collected through Apify.

One official actor (apify/instagram-hashtag-scraper) rather than a test+pro
pair like apify_x.py/apify_tiktok.py — it's Apify's own maintained actor
(most trustworthy choice for the only Instagram adapter), and hashtag search
is exactly the PRD §7.1 value this source is for: "product discovery,
comments, hashtags." A single profile is registered under both "instagram"
and "instagram_pro" keys at different item caps so the rest of the module's
"cheap test key + pro key" convention still holds without inventing a second,
less-trustworthy actor just to fill the slot.

NOTE — verify before first real (non-mock) run: actor id, pricing and input
field names were researched against the live Apify Store but not run here.
This source ships unapproved (permissions.py) by design, so nothing collects
or spends until an admin runs one small real scan via the curation console
and approves it — that run is also the moment to confirm the field mapping
in `to_evidence` below actually matches what the actor returns.

Like TikTok, Instagram hashtag search has no boolean query syntax — the
actor takes a flat list of hashtags, so build_search_terms here just cleans
and dedupes the keyword list into bare hashtag strings.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from app.core.config import settings

from .base import AdapterCapabilities, CollectionPage, SourceAdapter
from ..models import Geography, RawEvidence, ScanStatus

API = "https://api.apify.com/v2"

ITEMS_PER_KEYWORD = 40
PAGE_SIZE = 200
WAIT_FOR_RUN_SECONDS = 15 * 60
POLL_SECONDS = 10
RETRIES = 3
RETRY_BACKOFF_SECONDS = 3

_STATUS = {
    "READY": ScanStatus.QUEUED,
    "RUNNING": ScanStatus.COLLECTING,
    "SUCCEEDED": ScanStatus.COMPLETED,
    "FAILED": ScanStatus.FAILED,
    "TIMING-OUT": ScanStatus.FAILED,
    "TIMED-OUT": ScanStatus.FAILED,
    "ABORTING": ScanStatus.CANCELLED,
    "ABORTED": ScanStatus.CANCELLED,
}
_FINISHED = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}


class ApifyInstagramProfile:
    def __init__(self, key: str, actor: str, usd_per_1000: float, min_items: int, label: str):
        self.key, self.actor, self.usd_per_1000, self.min_items, self.label = key, actor, usd_per_1000, min_items, label


TEST_PROFILE = ApifyInstagramProfile(
    "instagram", "apify~instagram-hashtag-scraper", 1.90, 15,
    "Instagram (test cap, official actor, ~$1.90 per 1,000 posts — verify before enabling)",
)
PRO_PROFILE = ApifyInstagramProfile(
    "instagram_pro", "apify~instagram-hashtag-scraper", 1.90, 40,
    "Instagram (official actor, higher cap, ~$1.90 per 1,000 posts — verify before enabling)",
)


def build_search_terms(keywords: list[str], excluded: list[str], since: datetime, until: datetime,
                       language: Optional[str] = None) -> list[str]:
    """A flat hashtag list — no boolean operators, no server-side date filter
    or exclusion on this actor. `excluded` and the date window are applied as
    a post-filter by the generic collection pipeline that already does this
    for every source (filters stored items by published_at against the
    requested window before counting them toward a market)."""
    seen, out = set(), []
    for k in keywords:
        k = " ".join(k.strip().split()).lstrip("#").replace(" ", "")
        if not k or k.lower() in seen:
            continue
        seen.add(k.lower())
        out.append(k)
    return out


def _parse_time(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def to_evidence(item: dict, profile_key: str, collected_at: datetime) -> Optional[RawEvidence]:
    """One actor dataset item -> RawEvidence. Field names follow the common
    Apify Instagram hashtag-scraper convention — confirm against a real test
    run before this source is approved for collection."""
    text = (item.get("caption") or "").strip()
    pid = str(item.get("id") or item.get("shortCode") or "")
    if not pid:
        return None
    handle = item.get("ownerUsername")
    metrics = {}
    for src, dst in (("likesCount", "likes"), ("commentsCount", "replies"), ("videoViewCount", "views")):
        v = item.get(src)
        if isinstance(v, (int, float)):
            metrics[dst] = int(v)
    return RawEvidence(
        provider=profile_key,
        platform="instagram",
        source_id=f"instagram:{pid}",
        url=item.get("url") or (f"https://www.instagram.com/p/{item.get('shortCode')}/" if item.get("shortCode") else None),
        content_type="post",
        text=text or (item.get("hashtags") and " ".join(f"#{h}" for h in item["hashtags"])) or "",
        language="en",  # the hashtag-scraper doesn't report post language; left as the pipeline's default
        author_handle=f"@{handle}" if handle else None,
        author_is_verified=item.get("ownerIsVerified"),
        published_at=_parse_time(item.get("timestamp")),
        collected_at=collected_at,
        geography=Geography(source_field_location=(item.get("locationName") or None)),
        raw_metrics=metrics,
    )


class ApifyInstagramAdapter(SourceAdapter):
    def __init__(self, profile: ApifyInstagramProfile, token: Optional[str] = None, client_factory=None) -> None:
        self.profile = profile
        self._token = token
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=60))
        self._runs: dict[str, dict] = {}

    @property
    def token(self) -> str:
        tok = (self._token or settings.APIFY_API_TOKEN or "").strip()
        if not tok:
            raise RuntimeError("Apify isn't connected on this server (APIFY_API_TOKEN is not set).")
        return tok

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def _request(self, method: str, url: str, **kw) -> httpx.Response:
        last_exc: Optional[Exception] = None
        for attempt in range(RETRIES):
            try:
                async with self._client_factory() as c:
                    r = await c.request(method, url, headers=self._headers(), **kw)
                if r.status_code == 429 or r.status_code >= 500:
                    last_exc = RuntimeError(f"Apify {r.status_code}: {r.text[:200]}")
                else:
                    return r
            except httpx.TransportError as e:
                last_exc = e
            if attempt < RETRIES - 1:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
        raise RuntimeError(f"Apify didn't respond after {RETRIES} tries: {last_exc}")

    def _cap(self, keywords: list[str], items_per_query: Optional[int] = None) -> tuple[int, int]:
        per = max(items_per_query or ITEMS_PER_KEYWORD, self.profile.min_items)
        n = max(1, len([k for k in keywords if k.strip()]))
        return per, per * n

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            provider=self.profile.key,
            platform="instagram",
            verified_lookback_days=14,
            supports_date_filters=False,
            supports_keyword_search=False,  # hashtag-only, not free-text keyword search
            accessible_languages=["en", "pcm", "yo", "ig", "ha", "fr"],
            refresh_cadence_hours=24,
            notes=(f"{self.profile.label}. Public hashtagged posts only; up to {ITEMS_PER_KEYWORD} per hashtag per "
                   "scan. Field mapping unverified against a live run — see module docstring."),
        )

    async def estimate_cost(self, keywords: list[str], days: int) -> float:
        _, total = self._cap(keywords)
        return round(total * self.profile.usd_per_1000 / 1000 + 0.01, 4)

    async def start_collection(self, keywords: list[str], excluded_keywords: list[str],
                               since: datetime, until: datetime, language: Optional[str] = None,
                               items_per_query: Optional[int] = None, location: Optional[dict] = None) -> str:
        # Phase F: this actor (apify~instagram-hashtag-scraper) has no
        # location/geo field at all — verified against its input schema.
        # Real IG geo-targeting needs a different actor entirely (a
        # location-scraper taking a numeric IG location id), not a param
        # on this one — see geo.py's module docstring. `location` is
        # accepted for interface compatibility only and intentionally unused.
        hashtags = build_search_terms(keywords, excluded_keywords, since, until, language)
        if not hashtags:
            raise ValueError("no hashtags to search for")
        per, total = self._cap(hashtags, items_per_query)
        body = {"hashtags": hashtags, "resultsLimit": per}
        max_charge = round(total * self.profile.usd_per_1000 / 1000 * 1.5 + 0.05, 2)
        params = {"maxItems": total, "maxTotalChargeUsd": max_charge}
        r = await self._request("POST", f"{API}/acts/{self.profile.actor}/runs", params=params, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"Apify refused to start {self.profile.actor}: {r.status_code} {r.text[:300]}")
        data = r.json().get("data") or {}
        run_id = data.get("id")
        if not run_id:
            raise RuntimeError(f"Apify returned no run id: {r.text[:300]}")
        self._runs[run_id] = {"dataset": data.get("defaultDatasetId"), "status": data.get("status")}
        print(f"[MI][apify] started {self.profile.actor} run={run_id} hashtags={len(hashtags)} cap={total} max_charge=${max_charge}")
        return run_id

    async def _run(self, run_id: str) -> dict:
        r = await self._request("GET", f"{API}/actor-runs/{run_id}")
        r.raise_for_status()
        data = r.json().get("data") or {}
        self._runs[run_id] = {"dataset": data.get("defaultDatasetId"), "status": data.get("status"),
                              "usage_usd": data.get("usageTotalUsd")}
        return data

    async def get_status(self, run_id: str) -> ScanStatus:
        data = await self._run(run_id)
        return _STATUS.get(data.get("status"), ScanStatus.COLLECTING)

    async def wait(self, run_id: str, timeout: float = WAIT_FOR_RUN_SECONDS, poll: float = POLL_SECONDS) -> dict:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            data = await self._run(run_id)
            if data.get("status") in _FINISHED:
                return data
            if loop.time() >= deadline:
                return data
            await asyncio.sleep(poll)

    async def fetch_page(self, run_id: str, cursor: Optional[str] = None) -> CollectionPage:
        gaps: list[str] = []
        info = self._runs.get(run_id) or {}
        if cursor is None or info.get("status") not in _FINISHED:
            data = await self.wait(run_id)
            status = data.get("status")
            if status not in _FINISHED:
                gaps.append(f"Instagram collection was still running after {WAIT_FOR_RUN_SECONDS // 60} minutes — "
                            "only what had arrived so far is included")
            elif status != "SUCCEEDED":
                gaps.append(f"Instagram collection ended as {status} — results may be incomplete")
            info = self._runs.get(run_id) or {}
        dataset = info.get("dataset")
        if not dataset:
            return CollectionPage(evidence=[], has_more=False, gaps=gaps + ["Apify run has no dataset"])
        offset = int(cursor or 0)
        r = await self._request("GET", f"{API}/datasets/{dataset}/items",
                                params={"offset": offset, "limit": PAGE_SIZE, "clean": "true", "format": "json"})
        r.raise_for_status()
        items = r.json() if isinstance(r.json(), list) else []
        now = datetime.utcnow()
        seen: set[str] = set()
        evidence = []
        for item in items:
            ev = to_evidence(item, self.profile.key, now)
            if ev and ev.source_id not in seen:
                seen.add(ev.source_id)
                evidence.append(ev)
        has_more = len(items) == PAGE_SIZE
        return CollectionPage(evidence=evidence, has_more=has_more,
                              next_cursor=str(offset + len(items)) if has_more else None, gaps=gaps)

    async def actual_cost(self, run_id: str) -> Optional[float]:
        usage = (self._runs.get(run_id) or {}).get("usage_usd")
        if usage is None:
            try:
                usage = (await self._run(run_id)).get("usageTotalUsd")
            except Exception:
                return None
        return float(usage) if usage is not None else None

    async def cancel_if_supported(self, run_id: str) -> bool:
        try:
            r = await self._request("POST", f"{API}/actor-runs/{run_id}/abort")
        except RuntimeError:
            return False
        return r.status_code < 400

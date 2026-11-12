"""
Uri Market Intelligence — Facebook source adapter, collected through Apify.

One actor (scrapeflow/facebook-posts-search-scraper) — of the Facebook
actors researched against the live Apify Store, this is the one built for
genuine keyword/phrase search across public posts (`searchQueries`), not a
single-page or single-profile scraper (PRD §7.1 needs search by ontology
term, not a fixed list of pages to watch). Most other Facebook actors found
in the same research pass take a Page/Group/profile URL as their starting
point instead of a keyword — that shape doesn't fit this module's query
model at all, so they weren't considered. Two profiles (test/pro) at
different item caps, same actor — matching apify_reddit.py's convention —
rather than two different publishers, since only one candidate's input
field names were actually confirmed against its live listing.

NOTE — verify before first real (non-mock) run: actor id, pricing and input
field names were researched against the live Apify Store but not run here.
This source ships unapproved (permissions.py) by design, so nothing collects
or spends until an admin runs one small real scan via the curation console
and approves it — that run is also the moment to confirm the field mapping
in `to_evidence` below actually matches what the actor returns.

Facebook's public search has no boolean AND/OR/exclude syntax exposed
through this actor — it takes a flat list of search phrases
(`searchQueries`), so build_search_terms here is a deduped flat list like
TikTok/Instagram/News, not X/Reddit's OR-group composition.
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

ITEMS_PER_KEYWORD = 30          # small while unverified — same caution as every other new-platform adapter
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


class ApifyFacebookProfile:
    def __init__(self, key: str, actor: str, usd_per_1000: float, min_items: int, label: str):
        self.key, self.actor, self.usd_per_1000, self.min_items, self.label = key, actor, usd_per_1000, min_items, label


TEST_PROFILE = ApifyFacebookProfile(
    "facebook", "scrapeflow~facebook-posts-search-scraper", 3.99, 15,
    "Facebook (test cap, ~$3.99 per 1,000 results — verify before enabling)",
)
PRO_PROFILE = ApifyFacebookProfile(
    "facebook_pro", "scrapeflow~facebook-posts-search-scraper", 3.99, 50,
    "Facebook (higher cap, ~$3.99 per 1,000 results — verify before enabling)",
)


def build_search_terms(keywords: list[str], excluded: list[str], since: datetime, until: datetime,
                       language: Optional[str] = None) -> list[str]:
    """Facebook's search actor takes a flat phrase list, not a boolean
    query. `excluded` and the date window are applied client-side, same as
    TikTok/Instagram/News (collector.py already filters by published_at
    against the requested window for every source)."""
    seen, out = set(), []
    for k in keywords:
        k = " ".join(k.strip().split()).lstrip("#")
        if not k or k.lower() in seen:
            continue
        seen.add(k.lower())
        out.append(k)
    return out


def _parse_time(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(raw, tz=timezone.utc).replace(tzinfo=None)
        except Exception:
            return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def to_evidence(item: dict, profile_key: str, collected_at: datetime) -> Optional[RawEvidence]:
    """One actor dataset item -> RawEvidence. Field names follow the common
    Facebook post-search actor convention — confirm against a real test run
    before this source is approved for collection."""
    text = (item.get("text") or item.get("message") or item.get("content") or "").strip()
    pid = str(item.get("postId") or item.get("id") or "")
    if not text or not pid:
        return None
    author = item.get("author") or item.get("pageName") or item.get("authorName")
    metrics = {}
    for src, dst in (("likes", "likes"), ("reactionsCount", "likes"), ("comments", "replies"),
                     ("commentsCount", "replies"), ("shares", "reposts"), ("sharesCount", "reposts")):
        v = item.get(src)
        if isinstance(v, (int, float)) and dst not in metrics:
            metrics[dst] = int(v)
    return RawEvidence(
        provider=profile_key,
        platform="facebook",
        source_id=f"facebook:{pid}",
        url=item.get("url") or item.get("postUrl"),
        content_type="post",
        text=text,
        language=item.get("language") or "en",
        author_handle=author,
        published_at=_parse_time(item.get("time") or item.get("date") or item.get("timestamp")),
        collected_at=collected_at,
        geography=Geography(source_field_location=item.get("location") or None),
        raw_metrics=metrics,
    )


class ApifyFacebookAdapter(SourceAdapter):
    def __init__(self, profile: ApifyFacebookProfile, token: Optional[str] = None, client_factory=None) -> None:
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
            platform="facebook",
            verified_lookback_days=14,  # unverified; Facebook search skews recent, conservative default
            supports_date_filters=False,
            supports_keyword_search=True,
            accessible_languages=["en", "pcm", "yo", "ig", "ha", "fr"],
            refresh_cadence_hours=24,
            notes=(f"{self.profile.label}. Public posts only; up to {ITEMS_PER_KEYWORD} per keyword per scan. "
                   "Field mapping unverified against a live run — see module docstring."),
        )

    async def estimate_cost(self, keywords: list[str], days: int) -> float:
        _, total = self._cap(keywords)
        return round(total * self.profile.usd_per_1000 / 1000 + 0.01, 4)

    async def start_collection(self, keywords: list[str], excluded_keywords: list[str],
                               since: datetime, until: datetime, language: Optional[str] = None,
                               items_per_query: Optional[int] = None, location: Optional[dict] = None) -> str:
        # Phase F: this actor's input schema couldn't be verified (its
        # Apify Store page 404s) — no geo lever is built on an unconfirmed
        # assumption. `location` is accepted for interface compatibility
        # only and intentionally unused until verified in the Apify console.
        terms = build_search_terms(keywords, excluded_keywords, since, until, language)
        if not terms:
            raise ValueError("no keywords to search for")
        per, total = self._cap(terms, items_per_query)
        body = {"searchQueries": terms, "maxPostsPerQuery": per, "recentPostsOnly": True}
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
        print(f"[MI][apify] started {self.profile.actor} run={run_id} terms={len(terms)} cap={total} max_charge=${max_charge}")
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
                gaps.append(f"Facebook collection was still running after {WAIT_FOR_RUN_SECONDS // 60} minutes — "
                            "only what had arrived so far is included")
            elif status != "SUCCEEDED":
                gaps.append(f"Facebook collection ended as {status} — results may be incomplete")
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

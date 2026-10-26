"""
Uri Market Intelligence — YouTube source adapter, collected through Apify.

One actor (automation-lab/youtube-scraper) covers search, video metadata and
comments in a single run via a `maxComments` setting — exactly the PRD §7.1
value this source is for: "how-to demand, reviews, comment questions." Two
profiles (test/pro) at different comment caps, matching the module's usual
"cheap test key + pro key" convention.

NOTE — verify before first real (non-mock) run: actor id, pricing and input
field names were researched against the live Apify Store but not run here.
This source ships unapproved (permissions.py) by design, so nothing collects
or spends until an admin runs one small real scan via the curation console
and approves it — that run is also the moment to confirm the field mapping
in `to_evidence` below actually matches what the actor returns.

The actor returns a mix of video-level items (title/description) and
comment-level items (text/author) in the same dataset when maxComments > 0 —
to_evidence tells them apart by which fields are present rather than
assuming one shape, since a video's own description is a different kind of
evidence (the creator's words) from a viewer's comment (a customer's own
words) and both matter for listening but shouldn't be conflated.
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

ITEMS_PER_KEYWORD = 10          # videos per search term
COMMENTS_PER_VIDEO = 40         # comments per matched video — this is where the real listening signal is
PAGE_SIZE = 200
WAIT_FOR_RUN_SECONDS = 20 * 60  # comment scraping is slower than a plain search
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


class ApifyYouTubeProfile:
    def __init__(self, key: str, actor: str, usd_per_1000: float, min_items: int, label: str, comments_per_video: int):
        self.key, self.actor, self.usd_per_1000, self.min_items, self.label = key, actor, usd_per_1000, min_items, label
        self.comments_per_video = comments_per_video


TEST_PROFILE = ApifyYouTubeProfile(
    "youtube", "automation-lab~youtube-scraper", 0.90, 10,
    "YouTube (test cap, ~$0.90 per 1,000 comments — verify before enabling)", comments_per_video=15,
)
PRO_PROFILE = ApifyYouTubeProfile(
    "youtube_pro", "automation-lab~youtube-scraper", 0.90, 20,
    "YouTube (higher cap, ~$0.90 per 1,000 comments — verify before enabling)", comments_per_video=COMMENTS_PER_VIDEO,
)


def build_search_terms(keywords: list[str], excluded: list[str], since: datetime, until: datetime,
                       language: Optional[str] = None) -> list[str]:
    """A flat search-query list — this actor's search has no boolean syntax.
    `excluded` and the date window are applied as a post-filter by the
    generic collection pipeline, same as every other non-X source here."""
    seen, out = set(), []
    for k in keywords:
        k = " ".join(k.strip().split())
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
    """One actor dataset item -> RawEvidence. Comment-shaped items (have a
    comment `text`/`author` pair) map to content_type="comment"; video-shaped
    items (have `title`) map to content_type="post" using the description as
    text. Field names follow the common automation-lab actor convention —
    confirm against a real test run before this source is approved."""
    is_comment = bool(item.get("commentId") or (item.get("type") == "comment"))
    if is_comment:
        text = (item.get("text") or item.get("comment") or "").strip()
        cid = str(item.get("commentId") or item.get("id") or "")
        if not text or not cid:
            return None
        handle = item.get("author") or item.get("authorText")
        metrics = {}
        v = item.get("likesCount") or item.get("voteCount")
        if isinstance(v, (int, float)):
            metrics["likes"] = int(v)
        return RawEvidence(
            provider=profile_key, platform="youtube", source_id=f"youtube:comment:{cid}",
            url=item.get("videoUrl") or item.get("url"),
            content_type="comment", parent_id=str(item.get("videoId")) if item.get("videoId") else None,
            text=text, language="en",
            author_handle=f"@{handle}" if handle else None,
            published_at=_parse_time(item.get("publishedAt") or item.get("date")),
            collected_at=collected_at, raw_metrics=metrics,
        )
    title = (item.get("title") or "").strip()
    desc = (item.get("text") or item.get("description") or "").strip()
    vid = str(item.get("id") or item.get("videoId") or "")
    if not (title or desc) or not vid:
        return None
    channel = item.get("channelName") or item.get("channelTitle")
    metrics = {}
    for src, dst in (("viewCount", "views"), ("likes", "likes"), ("commentsCount", "replies")):
        v = item.get(src)
        if isinstance(v, (int, float)):
            metrics[dst] = int(v)
    return RawEvidence(
        provider=profile_key, platform="youtube", source_id=f"youtube:video:{vid}",
        url=item.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else None),
        content_type="post",
        text=" — ".join(p for p in (title, desc[:500]) if p),
        language="en",
        author_handle=f"@{channel}" if channel else None,
        published_at=_parse_time(item.get("date") or item.get("publishedAt")),
        collected_at=collected_at,
        geography=Geography(),
        raw_metrics=metrics,
    )


class ApifyYouTubeAdapter(SourceAdapter):
    def __init__(self, profile: ApifyYouTubeProfile, token: Optional[str] = None, client_factory=None) -> None:
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
        videos = per * n
        total = videos * (1 + self.profile.comments_per_video)  # videos + their comments, worst case
        return per, total

    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            provider=self.profile.key,
            platform="youtube",
            verified_lookback_days=30,
            supports_date_filters=False,
            supports_keyword_search=True,
            accessible_languages=["en", "pcm", "yo", "ig", "ha", "fr"],
            refresh_cadence_hours=24,
            notes=(f"{self.profile.label}. Up to {ITEMS_PER_KEYWORD} videos per search term, "
                   f"{self.profile.comments_per_video} comments per matched video. "
                   "Field mapping unverified against a live run — see module docstring."),
        )

    async def estimate_cost(self, keywords: list[str], days: int) -> float:
        _, total = self._cap(keywords)
        return round(total * self.profile.usd_per_1000 / 1000 + 0.01, 4)

    async def start_collection(self, keywords: list[str], excluded_keywords: list[str],
                               since: datetime, until: datetime, language: Optional[str] = None,
                               items_per_query: Optional[int] = None, location: Optional[dict] = None) -> str:
        # Phase F: this actor's `country` field is documented as a
        # localization setting, not a confirmed hard content filter —
        # left unused here rather than overselling precision we can't
        # verify. `location` is accepted for interface compatibility only.
        terms = build_search_terms(keywords, excluded_keywords, since, until, language)
        if not terms:
            raise ValueError("no keywords to search for")
        per, total = self._cap(terms, items_per_query)
        body = {"searchQueries": terms, "maxResults": per, "maxComments": self.profile.comments_per_video,
                "downloadSubtitles": False}
        max_charge = round(total * self.profile.usd_per_1000 / 1000 * 1.5 + 0.1, 2)
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
                gaps.append(f"YouTube collection was still running after {WAIT_FOR_RUN_SECONDS // 60} minutes — "
                            "only what had arrived so far is included")
            elif status != "SUCCEEDED":
                gaps.append(f"YouTube collection ended as {status} — results may be incomplete")
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

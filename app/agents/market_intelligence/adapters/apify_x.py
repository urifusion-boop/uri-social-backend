"""
Uri Market Intelligence — X (Twitter) source adapter, collected through Apify.

Two interchangeable Apify actors sit behind the same adapter (PRD §17: the
scan orchestration never knows which one ran):

  • "x"      — kaitoeasyapi/twitter-x-data-tweet-scraper-pay-per-result-cheapest
               (~$0.25 / 1,000 tweets). Cheapest; used to test the pipeline.
  • "x_pro"  — apidojo/tweet-scraper ("Tweet Scraper V2", ~$0.40 / 1,000).
               The most-used, most reliable X actor; the enterprise choice.

Both take X's own advanced-search syntax in `searchTerms`, so one query per
keyword carries the window (since:/until:), language (lang:) and exclusions
(-term). Every run is started with a hard spend cap (maxTotalChargeUsd) and an
item cap, so a mistake can never run up the Apify bill.

Collection is asynchronous on Apify's side: start_collection returns the
Apify run id straight away (persisted by the caller before anything is
fetched), and fetch_page waits for the run to finish before paging through
its dataset.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional

import httpx

from app.core.config import settings

from .base import AdapterCapabilities, CollectionPage, SourceAdapter
from ..models import Geography, RawEvidence, ScanStatus

API = "https://api.apify.com/v2"

# Per keyword, per scan. Small on purpose while testing; a scan of 6 keywords
# is ~600 tweets, ~$0.15 on the test actor.
ITEMS_PER_KEYWORD = 100
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


class ApifyXProfile:
    def __init__(self, key: str, actor: str, usd_per_1000: float, min_items: int, label: str):
        self.key, self.actor, self.usd_per_1000, self.min_items, self.label = key, actor, usd_per_1000, min_items, label


TEST_PROFILE = ApifyXProfile(
    "x", "kaitoeasyapi~twitter-x-data-tweet-scraper-pay-per-result-cheapest", 0.25, 20,
    "X (test actor, ~$0.25 per 1,000 tweets)",
)
PRO_PROFILE = ApifyXProfile(
    "x_pro", "apidojo~tweet-scraper", 0.40, 50,
    "X (Tweet Scraper V2, ~$0.40 per 1,000 tweets)",
)


def _quote(term: str) -> str:
    """For exclusions: an exact phrase, so '-free trial' doesn't drop every
    post containing "free"."""
    term = " ".join(term.split())
    return f'"{term}"' if " " in term else term


def _keyword(term: str) -> str:
    """A keyword matches posts containing all its words, in any order — an
    exact quoted phrase finds almost nothing on X (people don't write a
    marketer's phrase word for word). Already-quoted terms stay exact."""
    term = " ".join(term.split())
    if term.startswith("(") and term.endswith(")"):
        return term  # a planned OR-group, already well-formed
    if term.startswith('"') and term.endswith('"') and term.count('"') == 2:
        return term
    return f"({term})" if " " in term else term


def build_search_terms(keywords: list[str], excluded: list[str], since: datetime, until: datetime,
                       language: Optional[str] = None) -> list[str]:
    """One X advanced-search query per keyword. Retweets are left out (the same
    words again, by someone else) so each tweet is one person's own voice."""
    tail = [f"since:{since.strftime('%Y-%m-%d')}", f"until:{until.strftime('%Y-%m-%d')}", "-filter:retweets"]
    if language:
        tail.append(f"lang:{language}")
    tail += [f"-{_quote(x)}" for x in excluded if x.strip()]
    seen, out = set(), []
    for k in keywords:
        k = k.strip()
        if not k or k.lower() in seen:
            continue
        seen.add(k.lower())
        out.append(" ".join([_keyword(k)] + tail))
    return out


def _parse_time(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return parsedate_to_datetime(str(raw)).astimezone(timezone.utc).replace(tzinfo=None)  # "Thu Oct 17 09:30:41 +0000 2024"
    except Exception:
        pass
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def to_evidence(item: dict, profile_key: str, collected_at: datetime) -> Optional[RawEvidence]:
    """One actor dataset item -> RawEvidence. Both actors share the field names
    used here; anything missing is left unknown rather than guessed."""
    text = (item.get("text") or item.get("fullText") or "").strip()
    tid = str(item.get("id") or item.get("tweetId") or "")
    if not text or not tid or item.get("type") in ("mock_tweet",) or item.get("noResults"):
        return None
    author = item.get("author") or {}
    handle = author.get("userName") or author.get("screen_name")
    is_reply = bool(item.get("isReply"))
    parent = item.get("conversationId") or item.get("inReplyToId")
    metrics = {}
    for src, dst in (("likeCount", "likes"), ("replyCount", "replies"), ("retweetCount", "reposts"),
                     ("quoteCount", "quotes"), ("viewCount", "views")):
        v = item.get(src)
        if isinstance(v, (int, float)):
            metrics[dst] = int(v)
    return RawEvidence(
        provider=profile_key,
        platform="x",
        source_id=f"x:{tid}",
        url=item.get("url") or item.get("twitterUrl") or (f"https://x.com/{handle}/status/{tid}" if handle else None),
        content_type="reply" if is_reply else "post",
        parent_id=str(parent) if parent and str(parent) != tid else None,
        text=text,
        language=(item.get("lang") or "en") if item.get("lang") not in (None, "", "und", "zxx") else "en",
        author_handle=f"@{handle}" if handle else None,
        author_is_verified=author.get("isBlueVerified") if author.get("isBlueVerified") is not None else author.get("isVerified"),
        published_at=_parse_time(item.get("createdAt")),
        collected_at=collected_at,
        # The account's own profile location is a source field, never the
        # post's explicit location (PRD §11) — the classifier decides that.
        geography=Geography(source_field_location=(author.get("location") or None)),
        raw_metrics=metrics,
    )


class ApifyXAdapter(SourceAdapter):
    def __init__(self, profile: ApifyXProfile, token: Optional[str] = None,
                 client_factory=None) -> None:
        self.profile = profile
        self._token = token
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=60))
        self._runs: dict[str, dict] = {}

    # ── helpers ────────────────────────────────────────────────────────────
    @property
    def token(self) -> str:
        tok = (self._token or settings.APIFY_API_TOKEN or "").strip()
        if not tok:
            raise RuntimeError("Apify isn't connected on this server (APIFY_API_TOKEN is not set).")
        return tok

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def _request(self, method: str, url: str, **kw) -> httpx.Response:
        """One Apify call, retried on rate limits, server errors and dropped
        connections (Apify's own guidance: back off and retry 429/5xx)."""
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

    # ── SourceAdapter ──────────────────────────────────────────────────────
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities(
            provider=self.profile.key,
            platform="x",
            verified_lookback_days=30,
            supports_date_filters=True,
            supports_keyword_search=True,
            accessible_languages=["en", "pcm", "yo", "ig", "ha", "fr"],
            refresh_cadence_hours=24,  # paid per post: once a day is plenty for keep-updating
            notes=(f"{self.profile.label}. Public posts only; up to {ITEMS_PER_KEYWORD} tweets per keyword per scan. "
                   "X search reliably reaches about the last 30 days. Supports real city-level geo "
                   "targeting (near + radius) when a known place is requested."),
        )

    async def estimate_cost(self, keywords: list[str], days: int) -> float:
        _, total = self._cap(keywords)
        return round(total * self.profile.usd_per_1000 / 1000 + 0.01, 4)  # + run-start fee

    async def start_collection(self, keywords: list[str], excluded_keywords: list[str],
                               since: datetime, until: datetime, language: Optional[str] = None,
                               items_per_query: Optional[int] = None, location: Optional[dict] = None) -> str:
        terms = build_search_terms(keywords, excluded_keywords, since, until, language)
        if not terms:
            raise ValueError("no keywords to search for")
        per, total = self._cap(terms, items_per_query)
        if self.profile.key == "x_pro":
            body = {"searchTerms": terms, "maxItems": total, "sort": "Latest"}
        else:
            body = {"searchTerms": terms, "maxItems": per, "queryType": "Latest"}
        # Phase F: real city-precise geo targeting — both actors expose a
        # place-name "near" + radius field (apidojo calls them
        # geotaggedNear/withinRadius; kaitoeasyapi calls them near/within),
        # verified against each actor's own input schema.
        near = (location or {}).get("x_near")
        if near:
            radius = (location or {}).get("x_radius_km")
            if self.profile.key == "x_pro":
                body["geotaggedNear"] = near
                if radius:
                    body["withinRadius"] = f"{radius}km"
            else:
                body["near"] = near
                if radius:
                    body["within"] = f"{radius}km"
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
                gaps.append(f"X collection was still running after {WAIT_FOR_RUN_SECONDS // 60} minutes — "
                            "only what had arrived so far is included")
            elif status != "SUCCEEDED":
                gaps.append(f"X collection ended as {status} — results may be incomplete")
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
        """What Apify actually charged for the run, when it says."""
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

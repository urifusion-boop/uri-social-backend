"""
Uri Market Intelligence — scan orchestration.

Wires collection (adapters) → classification → clustering → scoring →
insight composition → persistence into one pipeline. This is the file that
changes least often once the module is stable — new adapters plug into
ADAPTER_REGISTRY, new classification/scoring logic lives in classification/,
new insight text logic lives in insight_composer.py. This file's only job is
sequencing and persistence, matching the "router.py stays thin, services do
the work" convention used throughout this codebase.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from .adapters.base import AdapterCapabilities, SourceAdapter
from .adapters.apify_x import PRO_PROFILE, TEST_PROFILE, ApifyXAdapter
from .adapters.apify_tiktok import (
    PRO_PROFILE as TIKTOK_PRO_PROFILE, TEST_PROFILE as TIKTOK_TEST_PROFILE, ApifyTikTokAdapter,
)
from .adapters.apify_instagram import (
    PRO_PROFILE as INSTAGRAM_PRO_PROFILE, TEST_PROFILE as INSTAGRAM_TEST_PROFILE, ApifyInstagramAdapter,
)
from .adapters.apify_youtube import (
    PRO_PROFILE as YOUTUBE_PRO_PROFILE, TEST_PROFILE as YOUTUBE_TEST_PROFILE, ApifyYouTubeAdapter,
)
from .adapters.apify_reddit import (
    PRO_PROFILE as REDDIT_PRO_PROFILE, TEST_PROFILE as REDDIT_TEST_PROFILE, ApifyRedditAdapter,
)
from .adapters.apify_news import (
    PRO_PROFILE as NEWS_PRO_PROFILE, TEST_PROFILE as NEWS_TEST_PROFILE, ApifyNewsAdapter,
)
from .adapters.apify_facebook import (
    PRO_PROFILE as FACEBOOK_PRO_PROFILE, TEST_PROFILE as FACEBOOK_TEST_PROFILE, ApifyFacebookAdapter,
)
from .adapters.mock import MockSourceAdapter
from .budget import reconcile_spend, reserve_budget
from .classification.classify import classify_evidence
from .classification_cache import get_cached_classification, store_classification_cache
from .classification.scoring import (
    compute_lifecycle,
    confidence_breakdown,
    is_concern_eligible,
    is_development_eligible,
    is_inquiry_eligible,
    is_trend_eligible,
    relevance_breakdown,
    urgency_for_concern,
    urgency_for_inquiry,
)
from .clustering import cluster_evidence, match_existing_cluster
from .development_extractor import extract_development
from .insight_composer import compose_insight
from .noise_filter import deterministic_noise_reason
from . import query_planner, term_learning
from .listening.geo import resolve_topic_location_hint
from .notifications import queue_notification
from app.services.PostHogService import track_event
from .models import (
    ActionReadiness,
    Classification,
    Cluster,
    CollectionRun,
    Development,
    DevelopmentStatus,
    Evidence,
    EvidenceType,
    InsightStatus,
    InsightVersion,
    Lifecycle,
    ScanStatus,
    ScoreBreakdown,
    SourceCoveragePreview,
    Topic,
)

NOISE_FILTER_MODEL_NAME = "deterministic-filter"
NOISE_FILTER_PROMPT_VERSION = "mi-noise-filter-v1"

# One adapter instance per provider — new real adapters register here.
# Every non-X source below ships unapproved in listening/permissions.py by
# design: registering an adapter here only makes it *available*, never
# *active* — collector.py's permissions.is_approved() gate blocks any source
# until an admin has run a real test scan and approved it through the
# curation console (see each adapter module's own docstring note).
ADAPTER_REGISTRY: dict[str, SourceAdapter] = {
    "mock": MockSourceAdapter(),
    # X through Apify: the cheap actor for testing, Tweet Scraper V2 for scale.
    "x": ApifyXAdapter(TEST_PROFILE),
    "x_pro": ApifyXAdapter(PRO_PROFILE),
    "tiktok": ApifyTikTokAdapter(TIKTOK_TEST_PROFILE),
    "tiktok_pro": ApifyTikTokAdapter(TIKTOK_PRO_PROFILE),
    "instagram": ApifyInstagramAdapter(INSTAGRAM_TEST_PROFILE),
    "instagram_pro": ApifyInstagramAdapter(INSTAGRAM_PRO_PROFILE),
    "youtube": ApifyYouTubeAdapter(YOUTUBE_TEST_PROFILE),
    "youtube_pro": ApifyYouTubeAdapter(YOUTUBE_PRO_PROFILE),
    "reddit": ApifyRedditAdapter(REDDIT_TEST_PROFILE),
    "reddit_pro": ApifyRedditAdapter(REDDIT_PRO_PROFILE),
    "news": ApifyNewsAdapter(NEWS_TEST_PROFILE),
    "news_pro": ApifyNewsAdapter(NEWS_PRO_PROFILE),
    "facebook": ApifyFacebookAdapter(FACEBOOK_TEST_PROFILE),
    "facebook_pro": ApifyFacebookAdapter(FACEBOOK_PRO_PROFILE),
}

# Real collection pages through results; a bound keeps one bad cursor from
# looping forever (200 items/page -> up to 10,000 items per source).
MAX_PAGES_PER_SOURCE = 50

INCREMENTAL_OVERLAP = timedelta(hours=2)

CLUSTERED_TYPES = {
    EvidenceType.CUSTOMER_CONCERN,
    EvidenceType.UNMET_NEED,
    EvidenceType.EMERGING_TREND,
    EvidenceType.COMPETITOR_MOVEMENT,
    # PRD §10 routes product_praise -> "Message and product insight" and
    # reputation_risk -> "Risk review" — both need a surfaced insight like
    # any other clusterable type. general_discussion is deliberately NOT
    # here: its own PRD route is "Supporting evidence," meaning it enriches
    # other insights rather than becoming a standalone card — that's the
    # PRD's own intent, not an oversight.
    EvidenceType.PRODUCT_PRAISE,
    EvidenceType.REPUTATION_RISK,
}


async def _fetch_business_context(db: AsyncIOMotorDatabase, user_id: str, brand_id: str) -> dict:
    """Reads real fields from brand_profiles — never invents a fact the
    business hasn't actually recorded (PRD §7: 'unknown business facts
    remain unknown'). stock_availability/delivery_capability/lead_time/
    budget_ceiling/margin_band are stored as empty string/None by default
    (see BrandProfileService.save's DEFAULTS) until the business fills them
    in via the Market Intelligence Settings panel — an empty value here
    means "unknown," normalised to None so scoring.py's existing "unknown
    -> 0" handling applies rather than treating "" as a real answer.
    service_locations isn't its own brand_profiles field; `region` is the
    closest existing fact and is reused here rather than inventing a
    second, overlapping field."""
    from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService

    try:
        result = await BrandProfileService.get(user_id, db, brand_id=brand_id)
        profile = (result.get("responseData") or {}) if result.get("status") else {}
    except Exception as e:
        print(f"[MI][scan] could not load business context: {e}")
        profile = {}

    region = profile.get("region") or None

    return {
        "brand_name": profile.get("brand_name"),
        "industry": profile.get("industry"),
        "key_products_services": profile.get("key_products_services") or [],
        "primary_goal": profile.get("primary_goal"),
        "service_locations": [region] if region else None,
        "stock_availability": profile.get("stock_availability") or None,
        "delivery_capability": profile.get("delivery_capability") or None,
        "lead_time": profile.get("lead_time") or None,
        "budget_ceiling": profile.get("budget_ceiling"),
        "margin_band": profile.get("margin_band") or None,
    }


def clamp_requested_days(requested_days: int, capability: AdapterCapabilities) -> tuple[int, Optional[str]]:
    """PRD §9: 'A shorter available period must never silently replace the
    requested one.' Never mutates the topic's own requested_days — returns
    the days actually usable for this source plus a human-readable note
    whenever that's less than what was asked for, so every caller (the
    pre-scan preview endpoint AND execute_scan itself) is forced to surface
    the reduction rather than just quietly using fewer days."""
    if requested_days <= capability.verified_lookback_days:
        return requested_days, None
    note = (
        f"requested {requested_days} days but '{capability.provider}' only verifies "
        f"{capability.verified_lookback_days} days of lookback — using {capability.verified_lookback_days}"
    )
    return capability.verified_lookback_days, note


async def preview_topic_coverage(topic: Topic) -> list[SourceCoveragePreview]:
    """PRD §9: 'Before running, show the accessible period, limits, collection
    scope and estimated usage.' Called by the router BEFORE a scan is
    started — uses the exact same clamp_requested_days used inside
    execute_scan, so the preview can never promise a different period than
    what the scan actually ends up using."""
    previews: list[SourceCoveragePreview] = []
    for source in topic.sources:
        adapter = ADAPTER_REGISTRY.get(source.provider)
        if adapter is None:
            previews.append(SourceCoveragePreview(
                provider=source.provider,
                requested_days=topic.requested_days,
                accessible_days=0,
                capped=True,
                note=f"source '{source.provider}' has no registered adapter",
            ))
            continue

        capability = adapter.capabilities()
        accessible_days, note = clamp_requested_days(topic.requested_days, capability)
        cost = await adapter.estimate_cost(topic.keywords, accessible_days)
        previews.append(SourceCoveragePreview(
            provider=source.provider,
            requested_days=topic.requested_days,
            accessible_days=accessible_days,
            capped=note is not None,
            note=note,
            estimated_cost_usd=cost,
        ))
    return previews


async def _dedupe_against_existing(db: AsyncIOMotorDatabase, topic_id: str, source_ids: list[str]) -> set[str]:
    """PRD §11: exact-record dedup by platform+source_id. Returns the set of
    source_ids ALREADY stored for this topic, so the caller skips them."""
    cursor = db["mi_evidence"].find({"topic_id": topic_id, "source_id": {"$in": source_ids}}, {"source_id": 1})
    return {doc["source_id"] async for doc in cursor}


def _action_readiness_from(relevance: ScoreBreakdown) -> ActionReadiness:
    """PRD §7: 'Recommendations with missing fulfilment information are
    labelled "Check suitability" rather than "Ready to act."' Reads
    relevance scoring's own fulfilment_feasibility component rather than
    re-deriving the same fact a second way."""
    component = next((c for c in relevance.components if c.name == "fulfilment_feasibility"), None)
    if component is not None and component.points > 0:
        return ActionReadiness.READY_TO_ACT
    return ActionReadiness.CHECK_SUITABILITY


def _majority_language(evidence_list: list[Evidence]) -> str:
    """Used only to gate auto-alerting (PRD §10/§14) — never to change
    classification. Ties fall to whichever language sorts first, which is
    an arbitrary but harmless choice among equally-represented languages."""
    if not evidence_list:
        return "en"
    counts: dict[str, int] = {}
    for e in evidence_list:
        counts[e.language] = counts.get(e.language, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def _track_insight_published(topic: Topic, insight: InsightVersion) -> None:
    """PRD §26 instrumentation. Deliberately excludes raw evidence/insight
    text — only ids, types and scores, matching §26's own "excluding raw
    private content from analytics.\""""
    track_event(topic.user_id, "insight_published", {
        "brand_id": topic.brand_id, "topic_id": topic.id, "insight_id": insight.id,
        "type": insight.type.value, "revision": insight.revision,
        "confidence_band": insight.confidence.band.value, "relevance_band": insight.relevance.band.value,
        "is_urgent": insight.urgency.is_urgent,
    })


async def _merge_cluster(db: AsyncIOMotorDatabase, existing: Cluster, candidate: Cluster) -> Cluster:
    """Combines a newly-clustered batch into an already-persisted cluster,
    keeping the EXISTING id — PRD §11/§19: 'keep a stable cluster ID through
    ordinary updates.' Counts are recomputed from the full (old + new)
    membership read back from mi_evidence, not incrementally accumulated,
    so they're always provably correct from source data rather than
    drifting across repeated merges."""
    merged_evidence_ids = list(dict.fromkeys(existing.evidence_ids + candidate.evidence_ids))
    member_docs = [doc async for doc in db["mi_evidence"].find({"id": {"$in": merged_evidence_ids}})]
    all_evidence = [Evidence(**{k: v for k, v in d.items() if k != "_id"}) for d in member_docs]

    independent_accounts = len({e.author_handle for e in all_evidence if e.author_handle})
    thread_ids = {e.parent_id for e in all_evidence if e.parent_id} or {e.source_id for e in all_evidence}
    dated = [e.published_at for e in all_evidence if e.published_at]

    return Cluster(
        id=existing.id,
        brand_id=existing.brand_id,
        topic_id=existing.topic_id,
        primary_type=existing.primary_type,
        theme=candidate.theme,  # refresh the label toward the latest common terms
        evidence_ids=merged_evidence_ids,
        independent_account_count=independent_accounts,
        original_thread_count=len(thread_ids),
        first_seen=min([existing.first_seen] + ([min(dated)] if dated else [])),
        last_updated=datetime.utcnow(),
        lifecycle=existing.lifecycle,
        embedding_centroid=candidate.embedding_centroid or existing.embedding_centroid,
        eligible_evaluation_count=existing.eligible_evaluation_count,
        consecutive_ineligible_count=existing.consecutive_ineligible_count,
    )


async def _resolve_classifications(
    db: AsyncIOMotorDatabase, member_evidence: list[Evidence], in_memory: dict[str, Classification]
) -> list[Classification]:
    """A merged cluster's membership includes evidence from earlier scans
    whose Classification isn't in this run's in-memory dict — fetched from
    mi_classifications so compose_insight always sees every member's real
    quote, not just this run's own slice."""
    result: list[Classification] = []
    missing_ids: list[str] = []
    for e in member_evidence:
        if e.id in in_memory:
            result.append(in_memory[e.id])
        else:
            missing_ids.append(e.id)
    if missing_ids:
        async for doc in db["mi_classifications"].find({"evidence_id": {"$in": missing_ids}}):
            doc.pop("_id", None)
            result.append(Classification(**doc))
    return result


async def _apply_revision(db: AsyncIOMotorDatabase, cluster_id: str, insight: InsightVersion) -> InsightVersion:
    """PRD §13: 'Keep every published insight revision immutable. Corrections
    create a new revision, update the active view and preserve an audit
    trail.' The previous ACTIVE revision for this cluster (if any) is marked
    SUPERSEDED — never edited in place — and this new one becomes active."""
    previous = await db["mi_insights"].find_one({"cluster_id": cluster_id, "status": InsightStatus.ACTIVE.value})
    if previous is None:
        return insight
    await db["mi_insights"].update_one({"id": previous["id"]}, {"$set": {"status": InsightStatus.SUPERSEDED.value}})
    insight.revision = previous.get("revision", 1) + 1
    insight.first_seen = previous.get("first_seen", insight.first_seen)
    return insight


async def create_scan_run(topic: Topic, db: AsyncIOMotorDatabase) -> CollectionRun:
    """Inserts a run and returns immediately — PRD §9: 'Return a job
    identifier immediately, allow the user to leave and return.' The router
    calls this synchronously, then (only if budget allowed it) schedules
    `execute_scan` (the actual work) as a background task against the same
    run id, so a slow scan never holds the HTTP request open.

    PRD §23/P0-15: reserves the estimated cost from the brand's monthly
    allowance BEFORE the run is ever queued. If reservation fails, the run
    is still recorded — visibly, as BUDGET_LIMITED — rather than silently
    dropped, but execute_scan is never scheduled for it (see the router)."""
    previews = await preview_topic_coverage(topic)
    total_estimated_cost = sum(p.estimated_cost_usd for p in previews)

    reserved, reason = await reserve_budget(db, topic.brand_id, total_estimated_cost)

    run = CollectionRun(
        id=str(uuid.uuid4()),
        topic_id=topic.id,
        brand_id=topic.brand_id,
        source_provider=",".join(s.provider for s in topic.sources) or "none",
        status=ScanStatus.QUEUED if reserved else ScanStatus.BUDGET_LIMITED,
        estimated_cost_usd=total_estimated_cost,
        gaps=[] if reserved else [f"budget limit reached: {reason}"],
    )
    await db["mi_scans"].insert_one(run.dict())
    return run


async def execute_scan(topic: Topic, run_id: str, db: AsyncIOMotorDatabase) -> None:
    """Runs as a FastAPI background task — nothing here returns to an HTTP
    caller, and nothing else ever calls this again for the same run_id, so
    if the pipeline below raises anything unhandled, the run would
    otherwise be stranded in COLLECTING/ANALYSING forever with no caller
    left to notice or retry. This wrapper is the one place that guarantees
    every run reaches a terminal status no matter what breaks inside —
    PRD §9's own state list includes FAILED for exactly this reason."""
    try:
        await _run_scan_pipeline(topic, run_id, db)
    except Exception as e:
        print(f"[MI][scan] run {run_id} crashed and was marked failed: {e}")
        await db["mi_scans"].update_one(
            {"id": run_id},
            {"$set": {
                "status": ScanStatus.FAILED.value,
                "completed_at": datetime.utcnow(),
                "gaps": [f"scan failed unexpectedly: {e}"],
            }},
        )
        # PRD §24 "run failure" — this crash path had zero visibility outside
        # a DB row's gaps field until now; a dashboard can't chart what never
        # fires an event.
        track_event(topic.user_id, "scan_failed", {
            "brand_id": topic.brand_id, "topic_id": topic.id, "scan_id": run_id, "error": str(e)[:200],
        })


async def _run_scan_pipeline(topic: Topic, run_id: str, db: AsyncIOMotorDatabase) -> None:
    """The actual collection→classify→cluster→score→compose pipeline, updating
    the run created by create_scan_run(). See execute_scan() for the crash
    safety net wrapping this."""
    # Written now, before a single source has started, so a client polling
    # GET /scans/{id} sees the real source plan immediately — every entry
    # "pending" — rather than nothing at all until the whole run ends.
    # Each source flips its own entry to "collecting" then "done"/"failed"
    # as it personally finishes, independently of the others still running
    # (same live-progress pattern as listening/collector.py's per_query).
    per_source_seed = [{"provider": s.provider, "status": "pending", "items": None, "cost": None, "error": None}
                       for s in topic.sources]
    await db["mi_scans"].update_one(
        {"id": run_id}, {"$set": {"status": ScanStatus.COLLECTING.value, "started_at": datetime.utcnow(),
                                  "per_source": per_source_seed}}
    )

    async def _mark_source(provider: str, **fields) -> None:
        sets = {f"per_source.$.{k}": v for k, v in fields.items()}
        await db["mi_scans"].update_one({"id": run_id, "per_source.provider": provider}, {"$set": sets})

    business_context = await _fetch_business_context(db, topic.user_id, topic.brand_id)
    # The topic's own scope, so labelling can tell a competitor's news from a
    # random brand, and a post from the market area from one elsewhere.
    business_context["competitors"] = list(topic.competitors or [])
    business_context["market_area"] = topic.geographic_scope
    business_context["topic_question"] = topic.question

    # Real (word-searching) sources get a search plan: many real-world
    # variations OR-ed into a few queries, sharpened by what earlier scans
    # learned. The sample-data source doesn't search, so it doesn't need one.
    plan: Optional[dict] = None
    if any(isinstance(ADAPTER_REGISTRY.get(s.provider), ApifyXAdapter) for s in topic.sources):
        if not query_planner.plan_is_current(topic):
            topic.search_plan = await query_planner.build_base_plan(topic, business_context)
            await db["mi_topics"].update_one(
                {"id": topic.id}, {"$set": {"search_plan": topic.search_plan, "plan_updated_at": datetime.utcnow()}})
        plan = query_planner.assemble(topic)
        await db["mi_scans"].update_one({"id": run_id}, {"$set": {"search_plan": {
            "groups": plan["groups"], "queries": len(plan["expressions"]), "excluded": plan["excluded"],
            "dropped_queries": plan["dropped_queries"],
        }}})

    # Competitors are searched too — otherwise "what competitors are doing"
    # could only ever be found by accident.
    search_keywords = list(dict.fromkeys(
        [k for k in topic.keywords if k.strip()] + [c for c in (topic.competitors or []) if c.strip()]))
    # Only a single, explicit language narrows the search; several (e.g. en +
    # pcm) means "don't filter", since X's own language tags are unreliable.
    search_language = topic.languages[0] if len(topic.languages or []) == 1 else None

    until = datetime.utcnow()

    # A repeat scan only needs what's new since the last good one (with a
    # small overlap for late-indexed posts) — re-requesting the whole window
    # just re-buys posts the dedup below throws away. PRD §18's bounded
    # overlap window.
    try:
        previous = await db["mi_scans"].find_one(
            {"topic_id": topic.id, "id": {"$ne": run_id},
             "status": {"$in": [ScanStatus.COMPLETED.value, ScanStatus.PARTIAL.value]},
             "started_at": {"$ne": None}},
            sort=[("started_at", -1)],
        )
    except Exception as e:  # can't tell -> collect the full window, never fail the scan over it
        print(f"[MI][scan] could not read the previous scan for {topic.id}: {e}")
        previous = None
    started = (previous or {}).get("started_at")
    resume_from = (started - INCREMENTAL_OVERLAP) if isinstance(started, datetime) else None

    all_new_evidence: list[Evidence] = []
    gaps: list[str] = []
    provider_run_ids: dict[str, str] = {}
    actual_costs: dict[str, float] = {}

    # The account's real, currently-saved Listening location (Settings ->
    # Listening location), not topic.geographic_scope — see
    # resolve_topic_location_hint's own docstring for why these two were
    # never the same thing and why that caused real, live-confirmed wrong
    # results (e.g. an India conference surfacing for a Nigeria-scoped
    # account). None for global/continent scope — collection stays
    # unfiltered, same as before this existed.
    location_hint = await resolve_topic_location_hint(db, topic.brand_id)

    for source in topic.sources:
        adapter = ADAPTER_REGISTRY.get(source.provider)
        if adapter is None:
            gaps.append(f"source '{source.provider}' has no registered adapter — skipped")
            await _mark_source(source.provider, status="failed", error="no registered adapter")
            continue

        await _mark_source(source.provider, status="collecting")

        # PRD §9: a shorter available period must never silently replace the
        # requested one — every clamp is recorded as a gap on THIS run, not
        # just shown once at preview time and then forgotten.
        accessible_days, coverage_note = clamp_requested_days(topic.requested_days, adapter.capabilities())
        if coverage_note:
            gaps.append(f"source '{source.provider}': {coverage_note}")
        since = until - timedelta(days=accessible_days)
        if resume_from and resume_from > since:
            since = resume_from

        try:
            if isinstance(adapter, ApifyXAdapter):
                provider_run_id = await adapter.start_collection(
                    plan["expressions"] if plan else search_keywords,
                    plan["excluded"] if plan else topic.excluded_keywords,
                    since, until, language=search_language, location=location_hint)
            else:
                provider_run_id = await adapter.start_collection(
                    search_keywords, topic.excluded_keywords, since, until, location=location_hint)
        except Exception as e:
            gaps.append(f"source '{source.provider}' collection failed: {e}")
            await _mark_source(source.provider, status="failed", error=str(e)[:300])
            continue

        # PRD §17: "save the provider run ID before fetching results" — saved
        # to the run doc immediately, not only held in memory, so it survives
        # even if fetch_page below fails.
        provider_run_ids[source.provider] = provider_run_id
        await db["mi_scans"].update_one({"id": run_id}, {"$set": {f"provider_run_ids.{source.provider}": provider_run_id}})

        # Every page, not just the first: a real run returns hundreds of
        # posts. fetch_page waits for a still-running provider run itself.
        cursor: Optional[str] = None
        seen_here: set[str] = set()
        items_this_source = 0
        source_error: Optional[str] = None
        for _page_no in range(MAX_PAGES_PER_SOURCE):
            try:
                page = await adapter.fetch_page(provider_run_id, cursor)
            except Exception as e:
                gaps.append(f"source '{source.provider}' collection failed: {e}")
                source_error = str(e)[:300]
                break

            fresh = [raw for raw in page.evidence if raw.source_id not in seen_here]
            seen_here.update(raw.source_id for raw in fresh)
            existing_ids = await _dedupe_against_existing(db, topic.id, [raw.source_id for raw in fresh])

            for raw in fresh:
                if raw.source_id in existing_ids:
                    continue
                evidence = Evidence(
                    **raw.dict(),
                    id=str(uuid.uuid4()),
                    brand_id=topic.brand_id,
                    user_id=topic.user_id,
                    topic_id=topic.id,
                    collection_run_id=run_id,
                )
                all_new_evidence.append(evidence)
                items_this_source += 1

            gaps.extend(page.gaps)
            if not page.has_more or not page.next_cursor:
                break
            cursor = page.next_cursor
        else:
            gaps.append(f"source '{source.provider}': stopped after {MAX_PAGES_PER_SOURCE} pages")

        # What the provider actually charged, when it reports it (Apify does).
        actual = getattr(adapter, "actual_cost", None)
        if actual is not None:
            try:
                cost = await actual(provider_run_id)
                if cost is not None:
                    actual_costs[source.provider] = cost
            except Exception as e:
                print(f"[MI][scan] could not read actual cost for {source.provider}: {e}")

        # Flips to "done" even when source_error is set (a page mid-way
        # failed) — whatever was collected before the failure is still real
        # and kept; the error is surfaced alongside it, not instead of it,
        # same as listening's per_query entries do.
        await _mark_source(source.provider, status="done", items=items_this_source,
                           cost=actual_costs.get(source.provider), error=source_error)

    if all_new_evidence:
        await db["mi_evidence"].insert_many([e.dict() for e in all_new_evidence])

    await db["mi_scans"].update_one({"id": run_id}, {"$set": {"status": ScanStatus.ANALYSING.value}})

    # ── Classify every new piece of evidence ────────────────────────────────
    # Deterministic noise (promo/bot boilerplate) is filtered before the LLM
    # call — PRD §23 cost control. A noise verdict here is still a real,
    # queryable Classification record (not a discard), just stamped with a
    # deterministic model_name instead of an LLM one.
    classifications: dict[str, Classification] = {}
    for evidence in all_new_evidence:
        noise_reason = deterministic_noise_reason(evidence)
        if noise_reason is not None:
            classifications[evidence.id] = Classification(
                evidence_id=evidence.id,
                primary_type=EvidenceType.NOISE,
                evidence_span=evidence.text[:200],
                uncertain=False,
                reasoning=f"Deterministic filter: {noise_reason}",
                model_name=NOISE_FILTER_MODEL_NAME,
                prompt_version=NOISE_FILTER_PROMPT_VERSION,
            )
            continue

        # PRD §23: "Cache classification by content and model version" —
        # checked/populated around the unchanged classify_evidence() call so
        # an exact repost or syndicated copy already classified for this
        # brand skips a second LLM call entirely.
        # Keyed per topic too: the relevance verdict depends on the question
        # being researched, so another topic's verdict can't be reused.
        cache_scope = f"{topic.brand_id}:{topic.id}"
        result = await get_cached_classification(db, evidence.text, cache_scope)
        if result is None:
            result = await classify_evidence(evidence, business_context)
            if result is not None:
                await store_classification_cache(db, evidence.text, cache_scope, result)

        if result is None:
            gaps.append(f"evidence {evidence.source_id} could not be classified — left for manual review")
            continue
        result.evidence_id = evidence.id
        classifications[evidence.id] = result

    if classifications:
        await db["mi_classifications"].insert_many([c.dict() for c in classifications.values()])

    # ── Cluster the types that cluster, score, compose insights ─────────────
    # Learn which search terms work: retire junk terms, pick up the wording
    # real customers used. Never allowed to fail the scan.
    if plan:
        try:
            learning = await term_learning.update_after_scan(db, topic, plan, all_new_evidence, classifications)
            off_topic = sum(1 for c in classifications.values() if getattr(c, "on_topic", None) is False)
            await db["mi_scans"].update_one({"id": run_id}, {"$set": {
                "learning": learning, "off_topic_filtered": off_topic}})
        except Exception as e:
            print(f"[MI][scan] term learning failed for {topic.id}: {e}")

    primary_types = {eid: c.primary_type for eid, c in classifications.items()}
    clusterable_evidence = [e for e in all_new_evidence if e.id in primary_types and primary_types[e.id] in CLUSTERED_TYPES]

    insights: list[InsightVersion] = []

    if clusterable_evidence:
        candidate_clusters = await cluster_evidence(
            clusterable_evidence, topic_id=topic.id, brand_id=topic.brand_id, primary_types=primary_types
        )

        # Reconcile against clusters already persisted for this topic — merge
        # into a matching one instead of always inserting a fresh row, so a
        # recurring (e.g. scheduled) scan accumulates evidence onto the SAME
        # cluster rather than spawning a lookalike every run.
        candidate_types = {c.primary_type.value for c in candidate_clusters}
        existing_docs = [
            doc async for doc in db["mi_clusters"].find({"topic_id": topic.id, "primary_type": {"$in": list(candidate_types)}})
        ]
        existing_clusters = [Cluster(**{k: v for k, v in d.items() if k != "_id"}) for d in existing_docs]

        clusters: list[Cluster] = []
        for candidate in candidate_clusters:
            match = match_existing_cluster(candidate, existing_clusters)
            if match is not None:
                merged = await _merge_cluster(db, match, candidate)
                clusters.append(merged)
                # A second new candidate in this same scan must not also
                # match the pre-merge snapshot of the same existing cluster.
                existing_clusters = [merged if c.id == match.id else c for c in existing_clusters]
            else:
                clusters.append(candidate)
                existing_clusters.append(candidate)

        for cluster in clusters:
            # Full membership (old + new) — a merged cluster's evidence_ids
            # include evidence from earlier scans that isn't in this scan's
            # own clusterable_evidence list.
            member_docs = [doc async for doc in db["mi_evidence"].find({"id": {"$in": cluster.evidence_ids}})]
            member_evidence = [Evidence(**{k: v for k, v in d.items() if k != "_id"}) for d in member_docs]

            if cluster.primary_type == EvidenceType.EMERGING_TREND:
                # PRD §12's trend bar, now meaningful because clusters persist
                # full historical membership across scans (not just this
                # scan's batch) — see clustering.match_existing_cluster.
                eligible, reason = is_trend_eligible(member_evidence)
                if eligible:
                    cluster.eligible_evaluation_count += 1
                    cluster.consecutive_ineligible_count = 0
                else:
                    cluster.consecutive_ineligible_count += 1
                cluster.lifecycle = compute_lifecycle(
                    cluster.lifecycle, eligible, cluster.eligible_evaluation_count, cluster.consecutive_ineligible_count
                )
            else:
                # PRD only specifies bespoke eligibility bars for concern/
                # inquiry/development/trend explicitly (§12) — unmet_need and
                # competitor_movement have none defined at all yet, so they
                # reuse the concern bar rather than inventing an unstated
                # threshold; tighten per-type once real eval data (PRD §26)
                # shows this pilot default is wrong for a given type.
                eligible, reason = is_concern_eligible(member_evidence, cluster.original_thread_count)

            await db["mi_clusters"].replace_one({"id": cluster.id}, cluster.dict(), upsert=True)

            if not eligible:
                gaps.append(f"cluster '{cluster.theme}' not surfaced: {reason}")
                continue
            urgency = urgency_for_concern(member_evidence)

            confidence = confidence_breakdown(member_evidence, cluster.original_thread_count)
            relevance = relevance_breakdown(member_evidence, business_context)
            member_classifications = await _resolve_classifications(db, member_evidence, classifications)

            insight = await compose_insight(cluster, member_evidence, member_classifications, confidence, relevance, urgency)
            insight.language = _majority_language(member_evidence)
            insight.action_readiness = _action_readiness_from(relevance)
            if cluster.primary_type == EvidenceType.REPUTATION_RISK and not insight.coverage_note:
                # PRD §14: "High-consequence reputation claims require human
                # review before an external alert. They remain available
                # internally with an unverified label." queue_notification()
                # separately makes sure this never auto-triggers an alert;
                # this note is the "unverified label" surfaced in the UI.
                insight.coverage_note = (
                    "Reputation-risk finding — unverified pending human review. Not yet suitable for any external claim or alert."
                )
            insight = await _apply_revision(db, cluster.id, insight)
            insights.append(insight)
            await queue_notification(db, insight, topic)
            _track_insight_published(topic, insight)

    # ── Individual (non-clustered) inquiries ────────────────────────────────
    for evidence in all_new_evidence:
        classification = classifications.get(evidence.id)
        if classification is None or classification.primary_type != EvidenceType.PURCHASE_INQUIRY:
            continue
        eligible, reason = is_inquiry_eligible(evidence)
        if not eligible:
            gaps.append(f"inquiry {evidence.source_id} not surfaced: {reason}")
            continue

        confidence = confidence_breakdown([evidence], original_thread_count=1)
        relevance = relevance_breakdown([evidence], business_context)
        urgency = urgency_for_inquiry(evidence)

        pseudo_cluster = Cluster(
            id=str(uuid.uuid4()),
            brand_id=topic.brand_id,
            topic_id=topic.id,
            primary_type=EvidenceType.PURCHASE_INQUIRY,
            theme=evidence.text[:60],
            evidence_ids=[evidence.id],
            independent_account_count=1,
            original_thread_count=1,
            first_seen=evidence.published_at or datetime.utcnow(),
            last_updated=datetime.utcnow(),
            lifecycle=Lifecycle.UNKNOWN,
        )
        insight = await compose_insight(pseudo_cluster, [evidence], [classification], confidence, relevance, urgency)
        insight.language = evidence.language
        insight.action_readiness = _action_readiness_from(relevance)
        insights.append(insight)
        await queue_notification(db, insight, topic)
        _track_insight_published(topic, insight)

    if insights:
        await db["mi_insights"].insert_many([i.dict() for i in insights])

    # ── Upcoming developments (PRD §13 "Upcoming development workflow") ─────
    developments: list[Development] = []
    for evidence in all_new_evidence:
        classification = classifications.get(evidence.id)
        if classification is None or classification.primary_type != EvidenceType.UPCOMING_DEVELOPMENT:
            continue

        extraction = await extract_development(evidence, business_context)
        if extraction is None:
            gaps.append(f"development {evidence.source_id} could not be extracted — left for manual review")
            continue

        eligible, reason = is_development_eligible(
            evidence, extraction.has_verifiable_source, extraction.event_date, extraction.preparation_action
        )
        if not eligible:
            gaps.append(f"development {evidence.source_id} not surfaced: {reason}")
            continue

        now = datetime.utcnow()
        developments.append(Development(
            id=str(uuid.uuid4()),
            brand_id=topic.brand_id,
            topic_id=topic.id,
            evidence_id=evidence.id,
            issuer=extraction.issuer,
            headline=extraction.headline,
            event_date=extraction.event_date,
            event_date_range_end=extraction.event_date_range_end,
            location=extraction.location,
            registration_deadline=extraction.registration_deadline,
            preparation_action=extraction.preparation_action,
            source_url=evidence.url,
            status=DevelopmentStatus.SCHEDULED,
            first_seen=now,
            last_updated=now,
        ))

    if developments:
        await db["mi_developments"].insert_many([d.dict() for d in developments])

    # PRD §23: "reconcile actual charges and release unused reservation."
    # Metered sources (Apify) report what the run really cost; the rest are
    # booked at their estimate.
    run_doc = await db["mi_scans"].find_one({"id": run_id})
    reserved_amount = (run_doc or {}).get("estimated_cost_usd", 0.0)
    if actual_costs:
        previews = {p.provider: p.estimated_cost_usd for p in await preview_topic_coverage(topic)}
        actual_amount = sum(actual_costs.values()) + sum(
            v for k, v in previews.items() if k not in actual_costs)
    else:
        actual_amount = reserved_amount
    await reconcile_spend(db, topic.brand_id, reserved_amount, actual_amount)
    await db["mi_scans"].update_one({"id": run_id}, {"$set": {"actual_cost_usd": round(actual_amount, 4)}})

    completed_at = datetime.utcnow()
    final_status = ScanStatus.PARTIAL if gaps else ScanStatus.COMPLETED
    await db["mi_scans"].update_one(
        {"id": run_id},
        {"$set": {
            "status": final_status.value,
            "completed_at": completed_at,
            "evidence_collected": len(all_new_evidence),
            "gaps": gaps,
        }},
    )
    # PRD §24 "scheduler lag" / "spend" — duration and cost were already
    # computed here for storage; attaching them as properties on the event
    # that already fires means a dashboard can chart them with no new event
    # to add later, just a breakdown on this one.
    started_at = (run_doc or {}).get("started_at")
    duration_seconds = (completed_at - started_at).total_seconds() if started_at else None
    track_event(topic.user_id, "scan_completed" if final_status == ScanStatus.COMPLETED else "scan_partial", {
        "brand_id": topic.brand_id, "topic_id": topic.id, "scan_id": run_id,
        "evidence_collected": len(all_new_evidence), "insights_count": len(insights), "gaps_count": len(gaps),
        "gaps": gaps, "duration_seconds": duration_seconds, "estimated_cost_usd": reserved_amount,
    })

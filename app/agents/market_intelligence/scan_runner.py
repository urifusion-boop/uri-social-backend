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
from .adapters.mock import MockSourceAdapter
from .budget import reconcile_spend, reserve_budget
from .classification.classify import classify_evidence
from .classification.scoring import (
    confidence_breakdown,
    is_concern_eligible,
    is_development_eligible,
    is_inquiry_eligible,
    relevance_breakdown,
    urgency_for_concern,
    urgency_for_inquiry,
)
from .clustering import cluster_evidence
from .development_extractor import extract_development
from .insight_composer import compose_insight
from .noise_filter import deterministic_noise_reason
from .models import (
    Classification,
    Cluster,
    CollectionRun,
    Development,
    DevelopmentStatus,
    Evidence,
    EvidenceType,
    InsightVersion,
    Lifecycle,
    ScanStatus,
    SourceCoveragePreview,
    Topic,
)

NOISE_FILTER_MODEL_NAME = "deterministic-filter"
NOISE_FILTER_PROMPT_VERSION = "mi-noise-filter-v1"

# One adapter instance per provider — new real adapters register here.
ADAPTER_REGISTRY: dict[str, SourceAdapter] = {
    "mock": MockSourceAdapter(),
}

CLUSTERED_TYPES = {
    EvidenceType.CUSTOMER_CONCERN,
    EvidenceType.UNMET_NEED,
    EvidenceType.EMERGING_TREND,
    EvidenceType.COMPETITOR_MOVEMENT,
}


async def _fetch_business_context(db: AsyncIOMotorDatabase, user_id: str, brand_id: str) -> dict:
    """Only reads fields that already exist on brand_profiles — never invents
    stock/delivery facts (PRD §7: 'unknown business facts remain unknown').
    Fields this doesn't find (service_locations, stock_availability,
    delivery_capability) simply stay absent, which scoring.py already treats
    as unknown rather than crashing on."""
    from app.agents.social_media_manager.services.brand_profile_service import BrandProfileService

    try:
        result = await BrandProfileService.get(user_id, db, brand_id=brand_id)
        profile = (result.get("responseData") or {}) if result.get("status") else {}
    except Exception as e:
        print(f"[MI][scan] could not load business context: {e}")
        profile = {}

    return {
        "brand_name": profile.get("brand_name"),
        "industry": profile.get("industry"),
        "key_products_services": profile.get("key_products_services") or [],
        "primary_goal": profile.get("primary_goal"),
        # Not yet fields on brand_profiles — left absent deliberately rather
        # than guessed, per PRD §7.
        "service_locations": None,
        "stock_availability": None,
        "delivery_capability": None,
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
    """The actual collection→classify→cluster→score→compose pipeline, updating
    the run created by create_scan_run(). Runs as a background task — nothing
    here returns to an HTTP caller, it only ever mutates `mi_scans`/`mi_evidence`/
    etc, which the GET /scans/{id} endpoint polls."""
    await db["mi_scans"].update_one(
        {"id": run_id}, {"$set": {"status": ScanStatus.COLLECTING.value, "started_at": datetime.utcnow()}}
    )

    business_context = await _fetch_business_context(db, topic.user_id, topic.brand_id)

    until = datetime.utcnow()

    all_new_evidence: list[Evidence] = []
    gaps: list[str] = []

    for source in topic.sources:
        adapter = ADAPTER_REGISTRY.get(source.provider)
        if adapter is None:
            gaps.append(f"source '{source.provider}' has no registered adapter — skipped")
            continue

        # PRD §9: a shorter available period must never silently replace the
        # requested one — every clamp is recorded as a gap on THIS run, not
        # just shown once at preview time and then forgotten.
        accessible_days, coverage_note = clamp_requested_days(topic.requested_days, adapter.capabilities())
        if coverage_note:
            gaps.append(f"source '{source.provider}': {coverage_note}")
        since = until - timedelta(days=accessible_days)

        try:
            provider_run_id = await adapter.start_collection(topic.keywords, topic.excluded_keywords, since, until)
            page = await adapter.fetch_page(provider_run_id)
        except Exception as e:
            gaps.append(f"source '{source.provider}' collection failed: {e}")
            continue

        candidate_ids = [raw.source_id for raw in page.evidence]
        existing_ids = await _dedupe_against_existing(db, topic.id, candidate_ids)

        for raw in page.evidence:
            if raw.source_id in existing_ids:
                continue
            evidence = Evidence(
                **raw.dict(),
                id=str(uuid.uuid4()),
                brand_id=topic.brand_id,
                user_id=topic.user_id,
                topic_id=topic.id,
            )
            all_new_evidence.append(evidence)

        gaps.extend(page.gaps)

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

        result = await classify_evidence(evidence, business_context)
        if result is None:
            gaps.append(f"evidence {evidence.source_id} could not be classified — left for manual review")
            continue
        result.evidence_id = evidence.id
        classifications[evidence.id] = result

    if classifications:
        await db["mi_classifications"].insert_many([c.dict() for c in classifications.values()])

    # ── Cluster the types that cluster, score, compose insights ─────────────
    primary_types = {eid: c.primary_type for eid, c in classifications.items()}
    clusterable_evidence = [e for e in all_new_evidence if e.id in primary_types and primary_types[e.id] in CLUSTERED_TYPES]

    insights: list[InsightVersion] = []

    if clusterable_evidence:
        clusters = await cluster_evidence(
            clusterable_evidence, topic_id=topic.id, brand_id=topic.brand_id, primary_types=primary_types
        )
        if clusters:
            await db["mi_clusters"].insert_many([c.dict() for c in clusters])

        for cluster in clusters:
            member_evidence = [e for e in clusterable_evidence if e.id in cluster.evidence_ids]

            # PRD only specifies bespoke eligibility bars for concern/inquiry/
            # development explicitly (§12) — trend has its own bar the PRD
            # describes but this pilot doesn't yet implement (scope note in
            # models.py), and unmet_need/competitor_movement have none defined
            # at all yet. All clusterable types reuse the concern bar for now
            # rather than inventing unstated thresholds; tighten per-type once
            # real eval data (PRD §26) shows this pilot default is wrong for a
            # given type.
            eligible, reason = is_concern_eligible(member_evidence, cluster.original_thread_count)
            if not eligible:
                gaps.append(f"cluster '{cluster.theme}' not surfaced: {reason}")
                continue
            urgency = urgency_for_concern(cluster.original_thread_count)

            confidence = confidence_breakdown(member_evidence, cluster.original_thread_count)
            relevance = relevance_breakdown(member_evidence, business_context)
            member_classifications = [classifications[e.id] for e in member_evidence if e.id in classifications]

            insight = await compose_insight(cluster, member_evidence, member_classifications, confidence, relevance, urgency)
            insights.append(insight)

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
        insights.append(insight)

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
    # This pilot's adapters only ever report an upfront estimate, not a
    # metered actual cost, so the reserved amount IS the actual charge here —
    # documented simplification, not a placeholder to silently forget about
    # once a metered adapter exists.
    run_doc = await db["mi_scans"].find_one({"id": run_id})
    reserved_amount = (run_doc or {}).get("estimated_cost_usd", 0.0)
    await reconcile_spend(db, topic.brand_id, reserved_amount, reserved_amount)

    final_status = ScanStatus.PARTIAL if gaps else ScanStatus.COMPLETED
    await db["mi_scans"].update_one(
        {"id": run_id},
        {"$set": {
            "status": final_status.value,
            "completed_at": datetime.utcnow(),
            "evidence_collected": len(all_new_evidence),
            "gaps": gaps,
        }},
    )

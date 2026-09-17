"""
Uri Market Intelligence — API router (PRD §20).

Thin by design: every endpoint validates brand ownership then delegates to
scan_runner.py or a direct Mongo read. No business logic lives here.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.dependencies import get_active_brand_context, get_db_dependency

# aws/dev doesn't have get_flexible_brand_context (the API-key/SDK-aware brand
# resolver added on aws/prod) yet — this feature is dev-only while testing and
# only ever needs regular JWT auth, so get_active_brand_context (JWT-only,
# already on dev) is the correct dependency here, not something to backport.
get_flexible_brand_context = get_active_brand_context
from app.domain.responses.uri_response import UriResponse
from app.services.PostHogService import track_event

from .models import (
    AccessGrantRequest,
    ActionBrief,
    BriefCreateRequest,
    DevelopmentUpdateRequest,
    Evidence,
    FeedbackOutcome,
    FeedbackRequest,
    InsightVersion,
    KeywordSuggestionRequest,
    MIAccessGrant,
    MIAccessLevel,
    PreferencesUpdateRequest,
    ScanStatus,
    SourceConfig,
    Topic,
    TopicCreateRequest,
)
from .access import can_manage_mi_access, require_mi_write_access
from .budget import get_or_create_budget
from .deletion import delete_evidence_cascade
from .notification_delivery import get_or_create_preferences
from .keyword_suggestion import suggest_keywords
from .scan_runner import ADAPTER_REGISTRY, create_scan_run, execute_scan, preview_topic_coverage

router = APIRouter(prefix="/market-intelligence", tags=["Market Intelligence"])


async def _get_owned_topic(topic_id: str, brand_id: str, db: AsyncIOMotorDatabase) -> dict:
    topic_doc = await db["mi_topics"].find_one({"id": topic_id})
    if not topic_doc:
        raise HTTPException(status_code=404, detail="Topic not found")
    if topic_doc["brand_id"] != brand_id:
        # Same response as "not found" — never confirm a topic exists for a
        # brand that isn't the caller's (PRD §21: "No user may retrieve
        # another tenant's insight by guessing an identifier").
        raise HTTPException(status_code=404, detail="Topic not found")
    return topic_doc


async def _get_owned_insight(insight_id: str, brand_id: str, db: AsyncIOMotorDatabase) -> dict:
    insight_doc = await db["mi_insights"].find_one({"id": insight_id})
    if not insight_doc or insight_doc["brand_id"] != brand_id:
        raise HTTPException(status_code=404, detail="Insight not found")
    return insight_doc


STALE_SCAN_TIMEOUT_MINUTES = 10


async def _self_heal_stale_scan(scan_doc: dict, db: AsyncIOMotorDatabase) -> dict:
    """execute_scan()'s own crash-safety net (scan_runner.py) guarantees a
    NEW scan always reaches a terminal status even if its pipeline raises —
    but it can't help a run whose entire background task died some other
    way (e.g. the worker process itself was killed or redeployed mid-run).
    A caller polling GET /scans/{id} against a run that will genuinely
    never change again would otherwise poll forever, which is exactly what
    showed up as "scan is taking longer than expected" with nothing
    actually running. Mirrors this codebase's existing self-heal-on-read
    pattern for stale Jane Ads campaign status.

    Deliberately does not cover a run stuck in QUEUED with no started_at at
    all — that would mean the background task was scheduled but never
    began, which realistically only happens if the process died in the gap
    between accepting the request and starting the task; too rare to be
    worth a second timestamp field for."""
    non_terminal = {ScanStatus.QUEUED.value, ScanStatus.COLLECTING.value, ScanStatus.ANALYSING.value}
    started_at = scan_doc.get("started_at")
    if scan_doc.get("status") not in non_terminal or started_at is None:
        return scan_doc
    if datetime.utcnow() - started_at < timedelta(minutes=STALE_SCAN_TIMEOUT_MINUTES):
        return scan_doc

    healed_fields = {
        "status": ScanStatus.FAILED.value,
        "completed_at": datetime.utcnow(),
        "gaps": (scan_doc.get("gaps") or []) + [
            "scan did not complete within the expected time and was marked failed — please retry"
        ],
    }
    await db["mi_scans"].update_one({"id": scan_doc["id"]}, {"$set": healed_fields})
    scan_doc.update(healed_fields)
    return scan_doc


async def _get_owned_development(development_id: str, brand_id: str, db: AsyncIOMotorDatabase) -> dict:
    dev_doc = await db["mi_developments"].find_one({"id": development_id})
    if not dev_doc or dev_doc["brand_id"] != brand_id:
        raise HTTPException(status_code=404, detail="Development not found")
    return dev_doc


@router.get("/sources")
async def list_sources(
    ctx: dict = Depends(get_flexible_brand_context),
):
    """PRD §8: 'The frontend obtains available controls from these
    [capability] records' — real registered providers today, not a
    hardcoded list the frontend has to keep in sync by hand. Grows
    automatically the moment a real adapter is registered."""
    sources = [adapter.capabilities().dict() for adapter in ADAPTER_REGISTRY.values()]
    return UriResponse.get_list_data_response("source", sources)


@router.post("/topics/suggest-keywords")
async def suggest_topic_keywords(
    body: KeywordSuggestionRequest,
    ctx: dict = Depends(get_flexible_brand_context),
):
    """PRD §9: 'Uri suggests keywords, related phrases and exclusions. The
    owner reviews them before starting collection.' Read-only — never
    creates anything; the frontend shows these as editable chips before
    POSTing the actual topic."""
    result = await suggest_keywords(body.question)
    if result is None:
        # Fail-open with the same simple heuristic create_topic falls back
        # to, rather than blocking the flow on an LLM hiccup — the owner is
        # reviewing these anyway.
        fallback_keywords = [w.strip() for w in body.question.split() if len(w.strip()) > 3]
        return UriResponse.get_single_data_response("suggestion", {"keywords": fallback_keywords, "excluded_keywords": []})
    return UriResponse.get_single_data_response("suggestion", {"keywords": result.keywords, "excluded_keywords": result.excluded_keywords})


@router.post("/topics")
async def create_topic(
    body: TopicCreateRequest,
    ctx: dict = Depends(require_mi_write_access),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    # PRD §9: "Uri suggests keywords... the owner reviews them before
    # starting collection" — real suggestion now happens via
    # POST /topics/suggest-keywords, reviewed client-side before this call;
    # this fallback only fires if the caller genuinely sent nothing.
    keywords = body.keywords or [w.strip() for w in body.question.split() if len(w.strip()) > 3]
    topic = Topic(
        id=str(uuid.uuid4()),
        brand_id=ctx["brand_id"],
        user_id=ctx["user_id"],
        question=body.question,
        keywords=keywords,
        excluded_keywords=body.excluded_keywords,
        sources=[SourceConfig(provider=p, platform=p) for p in body.sources],
        geographic_scope=body.geographic_scope,
        requested_days=body.requested_days,
        keep_updating=body.keep_updating,
        competitors=body.competitors,
        languages=body.languages,
        notification_sensitivity=body.notification_sensitivity,
    )
    await db["mi_topics"].insert_one(topic.dict())
    track_event(ctx["user_id"], "topic_created", {
        "brand_id": ctx["brand_id"], "topic_id": topic.id, "source_count": len(topic.sources),
        "requested_days": topic.requested_days, "keep_updating": topic.keep_updating,
    })
    return UriResponse.get_single_data_response("topic", topic.dict())


@router.get("/topics")
async def list_topics(
    ctx: dict = Depends(get_flexible_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    cursor = db["mi_topics"].find({"brand_id": ctx["brand_id"]}).sort("created_at", -1)
    topics = [doc async for doc in cursor]
    for t in topics:
        t.pop("_id", None)
    return UriResponse.get_list_data_response("topic", topics)


@router.get("/budget")
async def get_budget(
    ctx: dict = Depends(get_flexible_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """PRD §23/§5 (G5): lets the business (or an operations analyst) see
    this workspace's current monthly allowance, reservation and spend
    before it becomes a surprise. Read-only — allowance changes aren't
    exposed yet since role-based permissions (Owner vs Editor, PRD §21)
    aren't implemented in this pilot, and letting any authenticated brand
    member raise their own spending cap would defeat the control."""
    budget = await get_or_create_budget(db, ctx["brand_id"])
    budget.pop("_id", None)
    return UriResponse.get_single_data_response("budget", budget)


@router.get("/topics/{topic_id}/coverage-preview")
async def get_topic_coverage_preview(
    topic_id: str,
    ctx: dict = Depends(get_flexible_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """PRD §9: 'Before running, show the accessible period, limits, collection
    scope and estimated usage.' Called by the frontend before POSTing a scan —
    uses the same clamp_requested_days logic execute_scan itself uses, so this
    can never promise a period the scan doesn't actually honor."""
    topic_doc = await _get_owned_topic(topic_id, ctx["brand_id"], db)
    topic_doc.pop("_id", None)
    topic = Topic(**topic_doc)
    previews = await preview_topic_coverage(topic)
    return UriResponse.get_list_data_response("coverage", [p.dict() for p in previews])


@router.post("/topics/{topic_id}/scans")
async def start_scan(
    topic_id: str,
    background_tasks: BackgroundTasks,
    ctx: dict = Depends(require_mi_write_access),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    topic_doc = await _get_owned_topic(topic_id, ctx["brand_id"], db)
    topic_doc.pop("_id", None)
    topic = Topic(**topic_doc)

    run = await create_scan_run(topic, db)
    # PRD §23/P0-15: a run that couldn't reserve its estimated cost is
    # recorded (visibly, as BUDGET_LIMITED) but never actually executed —
    # nothing here spends against a reservation that was never granted.
    if run.status != ScanStatus.BUDGET_LIMITED:
        background_tasks.add_task(execute_scan, topic, run.id, db)

    track_event(ctx["user_id"], "scan_requested", {
        "brand_id": ctx["brand_id"], "topic_id": topic_id, "scan_id": run.id, "status": run.status.value,
    })
    return UriResponse.get_single_data_response("scan", run.dict())


@router.get("/scans/{scan_id}")
async def get_scan(
    scan_id: str,
    ctx: dict = Depends(get_flexible_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    scan_doc = await db["mi_scans"].find_one({"id": scan_id})
    if not scan_doc or scan_doc["brand_id"] != ctx["brand_id"]:
        raise HTTPException(status_code=404, detail="Scan not found")
    scan_doc = await _self_heal_stale_scan(scan_doc, db)
    scan_doc.pop("_id", None)
    return UriResponse.get_single_data_response("scan", scan_doc)


@router.get("/insights")
async def list_insights(
    ctx: dict = Depends(get_flexible_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    # Ranked by relevance, confidence, then recency (PRD §13) — Mongo can't
    # easily sort by a nested-document computed field across two dimensions
    # at once here, so this sorts by relevance total first, confidence
    # second, both already stored as plain ints on each document.
    cursor = (
        db["mi_insights"]
        .find({"brand_id": ctx["brand_id"], "status": "active"})
        .sort([("relevance.total", -1), ("confidence.total", -1), ("last_updated", -1)])
    )
    insights = [doc async for doc in cursor]
    for i in insights:
        i.pop("_id", None)
    return UriResponse.get_list_data_response("insight", insights)


@router.get("/insights/{insight_id}")
async def get_insight(
    insight_id: str,
    ctx: dict = Depends(get_flexible_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    insight_doc = await _get_owned_insight(insight_id, ctx["brand_id"], db)
    insight_doc.pop("_id", None)
    return UriResponse.get_single_data_response("insight", insight_doc)


@router.get("/insights/{insight_id}/evidence")
async def get_insight_evidence(
    insight_id: str,
    ctx: dict = Depends(get_flexible_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    insight_doc = await _get_owned_insight(insight_id, ctx["brand_id"], db)
    evidence_ids = insight_doc.get("evidence_ids", [])
    cursor = db["mi_evidence"].find({"id": {"$in": evidence_ids}, "brand_id": ctx["brand_id"]})
    evidence = [doc async for doc in cursor]
    for e in evidence:
        e.pop("_id", None)
    track_event(ctx["user_id"], "evidence_opened", {
        "brand_id": ctx["brand_id"], "insight_id": insight_id, "evidence_count": len(evidence),
    })
    return UriResponse.get_list_data_response("evidence", evidence)


@router.get("/insights/{insight_id}/trace")
async def get_insight_trace(
    insight_id: str,
    ctx: dict = Depends(get_flexible_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """PRD §16, P0-16: 'A finding can be traced to topic, collection,
    evidence and model versions.' Walks the real chain stored on the
    records themselves — evidence -> the scan(s) that collected it ->
    provider run ids, and classification -> model/prompt versions — rather
    than a separate log a change elsewhere could silently drift from."""
    insight_doc = await _get_owned_insight(insight_id, ctx["brand_id"], db)

    evidence_ids = insight_doc.get("evidence_ids", [])
    evidence_docs = [
        doc async for doc in db["mi_evidence"].find({"id": {"$in": evidence_ids}, "brand_id": ctx["brand_id"]})
    ]
    for e in evidence_docs:
        e.pop("_id", None)

    classification_docs = [
        doc async for doc in db["mi_classifications"].find({"evidence_id": {"$in": evidence_ids}})
    ]
    for c in classification_docs:
        c.pop("_id", None)

    collection_run_ids = sorted({e.get("collection_run_id") for e in evidence_docs if e.get("collection_run_id")})
    scan_docs = [doc async for doc in db["mi_scans"].find({"id": {"$in": collection_run_ids}})]
    for s in scan_docs:
        s.pop("_id", None)

    model_versions = sorted({
        f"{c.get('model_name')}@{c.get('prompt_version')}" for c in classification_docs if c.get("model_name")
    })

    trace = {
        "insight_id": insight_id,
        "insight_revision": insight_doc.get("revision"),
        "topic_id": insight_doc.get("topic_id"),
        "cluster_id": insight_doc.get("cluster_id"),
        "evidence_ids": evidence_ids,
        "collection_runs": scan_docs,
        "classifications": classification_docs,
        "model_versions": model_versions,
    }
    return UriResponse.get_single_data_response("trace", trace)


@router.post("/insights/{insight_id}/feedback")
async def submit_feedback(
    insight_id: str,
    body: FeedbackRequest,
    ctx: dict = Depends(require_mi_write_access),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    await _get_owned_insight(insight_id, ctx["brand_id"], db)
    feedback = FeedbackOutcome(
        id=str(uuid.uuid4()),
        insight_id=insight_id,
        user_id=ctx["user_id"],
        verdict=body.verdict,
        reason=body.reason,
    )
    await db["mi_feedback"].insert_one(feedback.dict())

    # PRD §16: "Incorrect feedback opens a reason selector and queues review."
    # "One user's dismissal must not globally suppress a public topic" — this
    # only ever marks THIS insight for THIS tenant; it never touches the
    # underlying evidence/cluster shared with any other brand.
    if body.verdict.value == "not_relevant":
        await db["mi_insights"].update_one({"id": insight_id}, {"$set": {"status": "dismissed"}})

    track_event(ctx["user_id"], "feedback_submitted", {
        "brand_id": ctx["brand_id"], "insight_id": insight_id, "verdict": body.verdict.value,
    })
    return UriResponse.get_single_data_response("feedback", feedback.dict())


@router.delete("/evidence/{evidence_id}")
async def delete_evidence(
    evidence_id: str,
    ctx: dict = Depends(require_mi_write_access),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """PRD §21/§16, P0-14: 'removed evidence disappears from all serving
    paths.' Cascades into classifications, clusters and insights — see
    deletion.py for exactly what that means for each."""
    result = await delete_evidence_cascade(evidence_id, ctx["brand_id"], db)
    if result is None:
        raise HTTPException(status_code=404, detail="Evidence not found")
    return UriResponse.get_single_data_response("deletion", result)


@router.post("/insights/{insight_id}/briefs")
async def create_brief(
    insight_id: str,
    body: BriefCreateRequest,
    ctx: dict = Depends(require_mi_write_access),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    insight_doc = await _get_owned_insight(insight_id, ctx["brand_id"], db)

    # Idempotent: re-submitting the same insight_id returns the existing
    # brief rather than creating a duplicate — PRD §25 P0-12.
    existing = await db["mi_briefs"].find_one({"insight_id": insight_id})
    if existing:
        existing.pop("_id", None)
        return UriResponse.get_single_data_response("brief", existing)

    message = body.proposed_message or insight_doc.get("suggested_next_step") or ""
    brief = ActionBrief(
        id=str(uuid.uuid4()),
        insight_id=insight_id,
        insight_revision=insight_doc.get("revision", 1),
        brand_id=ctx["brand_id"],
        customer_need=insight_doc.get("business_implication", ""),
        proposed_message=message,
        evidence_ids=insight_doc.get("evidence_ids", []),
    )
    await db["mi_briefs"].insert_one(brief.dict())
    track_event(ctx["user_id"], "brief_created", {"brand_id": ctx["brand_id"], "insight_id": insight_id, "brief_id": brief.id})
    return UriResponse.get_single_data_response("brief", brief.dict())


@router.patch("/insights/{insight_id}/briefs")
async def update_brief(
    insight_id: str,
    body: BriefCreateRequest,
    ctx: dict = Depends(require_mi_write_access),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """PRD §13: 'Where a destination module is unavailable, save an editable
    brief within Intelligence.' No real Jane Ads/content-calendar handoff
    exists in this pilot yet (deliberately not rushed — the safe version of
    that integration needs its own careful, separate work given the real
    spend/publish risk either module carries), so this is that fallback
    made complete: the brief this pilot DOES create must actually be
    editable, not just create-once-and-done."""
    await _get_owned_insight(insight_id, ctx["brand_id"], db)
    existing = await db["mi_briefs"].find_one({"insight_id": insight_id})
    if not existing:
        raise HTTPException(status_code=404, detail="No brief exists for this insight yet")
    if body.proposed_message is not None:
        await db["mi_briefs"].update_one({"insight_id": insight_id}, {"$set": {"proposed_message": body.proposed_message}})
        existing["proposed_message"] = body.proposed_message
    existing.pop("_id", None)
    return UriResponse.get_single_data_response("brief", existing)


@router.get("/developments")
async def list_developments(
    ctx: dict = Depends(get_flexible_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    # Soonest event first; undated ("date to confirm") items sort last rather
    # than first — Mongo puts missing/null fields first in an ascending sort
    # by default, which would otherwise bury dated, actionable items under
    # undated ones.
    cursor = (
        db["mi_developments"]
        .find({"brand_id": ctx["brand_id"]})
        .sort([("event_date", 1), ("last_updated", -1)])
    )
    developments = [doc async for doc in cursor]
    for d in developments:
        d.pop("_id", None)
    return UriResponse.get_list_data_response("development", developments)


@router.get("/developments/{development_id}")
async def get_development(
    development_id: str,
    ctx: dict = Depends(get_flexible_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    dev_doc = await _get_owned_development(development_id, ctx["brand_id"], db)
    dev_doc.pop("_id", None)
    return UriResponse.get_single_data_response("development", dev_doc)


@router.patch("/developments/{development_id}")
async def update_development(
    development_id: str,
    body: DevelopmentUpdateRequest,
    ctx: dict = Depends(require_mi_write_access),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """PRD §13: 'Postponement or cancellation updates the SAME item and any
    linked preparation task' — this revises the existing Development record
    in place, it never creates a new one."""
    await _get_owned_development(development_id, ctx["brand_id"], db)
    update_fields = {"status": body.status.value, "last_updated": datetime.utcnow()}
    if body.event_date is not None:
        update_fields["event_date"] = body.event_date
    if body.verification_note is not None:
        update_fields["verification_note"] = body.verification_note
    await db["mi_developments"].update_one({"id": development_id}, {"$set": update_fields})
    dev_doc = await _get_owned_development(development_id, ctx["brand_id"], db)
    dev_doc.pop("_id", None)
    return UriResponse.get_single_data_response("development", dev_doc)


@router.get("/preferences")
async def get_preferences(
    ctx: dict = Depends(get_flexible_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """PRD §14: 'Users can mute a topic or type, snooze an insight, change
    sensitivity, disable channels.'"""
    prefs = await get_or_create_preferences(db, ctx["user_id"], ctx["brand_id"])
    prefs.pop("_id", None)
    return UriResponse.get_single_data_response("preferences", prefs)


@router.patch("/preferences")
async def update_preferences(
    body: PreferencesUpdateRequest,
    ctx: dict = Depends(require_mi_write_access),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    prefs = await get_or_create_preferences(db, ctx["user_id"], ctx["brand_id"])

    update: dict = {"updated_at": datetime.utcnow()}
    for field in (
        "email_enabled", "timezone", "digest_hour_local",
        "quiet_hours_start_local", "quiet_hours_end_local", "urgent_override",
    ):
        value = getattr(body, field)
        if value is not None:
            update[field] = value

    muted_topics = set(prefs.get("muted_topic_ids", []))
    if body.mute_topic_id:
        muted_topics.add(body.mute_topic_id)
    if body.unmute_topic_id:
        muted_topics.discard(body.unmute_topic_id)
    if body.mute_topic_id or body.unmute_topic_id:
        update["muted_topic_ids"] = list(muted_topics)

    muted_categories = set(prefs.get("muted_categories", []))
    if body.mute_category:
        muted_categories.add(body.mute_category.value)
    if body.unmute_category:
        muted_categories.discard(body.unmute_category.value)
    if body.mute_category or body.unmute_category:
        update["muted_categories"] = list(muted_categories)

    await db["mi_preferences"].update_one({"user_id": ctx["user_id"], "brand_id": ctx["brand_id"]}, {"$set": update})
    updated = await get_or_create_preferences(db, ctx["user_id"], ctx["brand_id"])
    updated.pop("_id", None)
    return UriResponse.get_single_data_response("preferences", updated)


@router.post("/insights/{insight_id}/snooze")
async def snooze_insight(
    insight_id: str,
    ctx: dict = Depends(require_mi_write_access),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """PRD §16: 'Snoozing suppresses delivery, not evidence updates' — the
    insight stays active and visible in-app, this only stops future outbox
    entries for it from actually sending (see notification_delivery.py)."""
    await _get_owned_insight(insight_id, ctx["brand_id"], db)
    prefs = await get_or_create_preferences(db, ctx["user_id"], ctx["brand_id"])
    snoozed = set(prefs.get("snoozed_insight_ids", []))
    snoozed.add(insight_id)
    await db["mi_preferences"].update_one(
        {"user_id": ctx["user_id"], "brand_id": ctx["brand_id"]},
        {"$set": {"snoozed_insight_ids": list(snoozed), "updated_at": datetime.utcnow()}},
    )
    return UriResponse.get_single_data_response("preferences", {"snoozed_insight_ids": list(snoozed)})


@router.get("/access")
async def list_access_grants(
    ctx: dict = Depends(get_flexible_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """PRD §21. Only an agency admin (for an agency-owned brand) or the
    brand's own owner (for a personal brand) can see who's been
    restricted — everyone else gets a plain 403, not a distinguishing
    empty list."""
    if not await can_manage_mi_access(db, ctx["brand_id"], ctx["user_id"]):
        raise HTTPException(status_code=403, detail="Only the brand owner or an agency admin can manage access")
    cursor = db["mi_access"].find({"brand_id": ctx["brand_id"]})
    grants = [doc async for doc in cursor]
    for g in grants:
        g.pop("_id", None)
    return UriResponse.get_list_data_response("access", grants)


@router.post("/access")
async def set_access_grant(
    body: AccessGrantRequest,
    ctx: dict = Depends(get_flexible_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Setting level=FULL deletes any restriction rather than storing a
    redundant row — FULL is simply what "no record" already means (see
    access.py), so a grant document only ever exists to restrict someone."""
    if not await can_manage_mi_access(db, ctx["brand_id"], ctx["user_id"]):
        raise HTTPException(status_code=403, detail="Only the brand owner or an agency admin can manage access")

    if body.level == MIAccessLevel.FULL:
        await db["mi_access"].delete_one({"brand_id": ctx["brand_id"], "user_id": body.user_id})
        return UriResponse.get_single_data_response("access", {"user_id": body.user_id, "level": "full"})

    grant = MIAccessGrant(
        id=str(uuid.uuid4()), brand_id=ctx["brand_id"], user_id=body.user_id,
        level=body.level, granted_by=ctx["user_id"],
    )
    await db["mi_access"].update_one(
        {"brand_id": ctx["brand_id"], "user_id": body.user_id},
        {"$set": grant.dict()},
        upsert=True,
    )
    return UriResponse.get_single_data_response("access", grant.dict())

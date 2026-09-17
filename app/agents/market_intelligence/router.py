"""
Uri Market Intelligence — API router (PRD §20).

Thin by design: every endpoint validates brand ownership then delegates to
scan_runner.py or a direct Mongo read. No business logic lives here.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.dependencies import get_active_brand_context, get_db_dependency

# aws/dev doesn't have get_flexible_brand_context (the API-key/SDK-aware brand
# resolver added on aws/prod) yet — this feature is dev-only while testing and
# only ever needs regular JWT auth, so get_active_brand_context (JWT-only,
# already on dev) is the correct dependency here, not something to backport.
get_flexible_brand_context = get_active_brand_context
from app.domain.responses.uri_response import UriResponse

from .models import (
    ActionBrief,
    BriefCreateRequest,
    Evidence,
    FeedbackOutcome,
    FeedbackRequest,
    InsightVersion,
    SourceConfig,
    Topic,
    TopicCreateRequest,
)
from .scan_runner import create_scan_run, execute_scan, preview_topic_coverage

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


@router.post("/topics")
async def create_topic(
    body: TopicCreateRequest,
    ctx: dict = Depends(get_flexible_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
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
    )
    await db["mi_topics"].insert_one(topic.dict())
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
    ctx: dict = Depends(get_flexible_brand_context),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    topic_doc = await _get_owned_topic(topic_id, ctx["brand_id"], db)
    topic_doc.pop("_id", None)
    topic = Topic(**topic_doc)

    run = await create_scan_run(topic, db)
    background_tasks.add_task(execute_scan, topic, run.id, db)

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
    return UriResponse.get_list_data_response("evidence", evidence)


@router.post("/insights/{insight_id}/feedback")
async def submit_feedback(
    insight_id: str,
    body: FeedbackRequest,
    ctx: dict = Depends(get_flexible_brand_context),
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

    return UriResponse.get_single_data_response("feedback", feedback.dict())


@router.post("/insights/{insight_id}/briefs")
async def create_brief(
    insight_id: str,
    body: BriefCreateRequest,
    ctx: dict = Depends(get_flexible_brand_context),
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
    return UriResponse.get_single_data_response("brief", brief.dict())

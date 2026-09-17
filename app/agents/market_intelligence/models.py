"""
Uri Market Intelligence — the interface contract (the seam between collection,
intelligence, and the API/frontend layers).

Structured the same way as jane_ads/models.py: every domain object a Pydantic
model, nothing here depends on a live source adapter, so classification/scoring
can be built and tested against MockSourceAdapter's fixtures before any real
collection exists.

Scope note (PRD "Uri Market Intelligence" v1.0): this first pass covers the two
evidence types worth building end-to-end first — purchase_inquiry and
customer_concern — plus the full evidence taxonomy as an enum so later types
slot in without a schema change. Confidence/relevance scoring follows PRD §12's
5-component 0-2-point rubric; adapters follow PRD §17's contract.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Enums ─────────────────────────────────────────────────────────────────────

class EvidenceType(str, Enum):
    """PRD §10 evidence taxonomy. Only PURCHASE_INQUIRY and CUSTOMER_CONCERN are
    actively scored/clustered in this first pass (see classification/scoring.py);
    the rest exist so later phases don't need a migration to add them."""
    PURCHASE_INQUIRY = "purchase_inquiry"
    CUSTOMER_CONCERN = "customer_concern"
    UNMET_NEED = "unmet_need"
    PRODUCT_PRAISE = "product_praise"
    EMERGING_TREND = "emerging_trend"
    COMPETITOR_MOVEMENT = "competitor_movement"
    UPCOMING_DEVELOPMENT = "upcoming_development"
    REPUTATION_RISK = "reputation_risk"
    GENERAL_DISCUSSION = "general_discussion"
    NOISE = "noise"


class ScanStatus(str, Enum):
    """PRD §9 engineering note's required state set."""
    QUEUED = "queued"
    COLLECTING = "collecting"
    ANALYSING = "analysing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_LIMITED = "budget_limited"


class Lifecycle(str, Enum):
    """PRD §12 lifecycle states for a trend/cluster."""
    EARLY = "early"
    EMERGING = "emerging"
    ESTABLISHED = "established"
    COOLING = "cooling"
    UNKNOWN = "unknown"


class ConfidenceBand(str, Enum):
    LOW = "low"        # 0-4
    MEDIUM = "medium"  # 5-7
    HIGH = "high"      # 8-10


class InsightStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"   # a newer revision replaced this one
    RETRACTED = "retracted"     # evidence removed / claim invalidated
    DISMISSED = "dismissed"     # user feedback


class FeedbackVerdict(str, Enum):
    USEFUL = "useful"
    NOT_RELEVANT = "not_relevant"
    INCORRECT = "incorrect"


# ── Provenance / geography (PRD §11, §7) ───────────────────────────────────────

class Geography(BaseModel):
    """PRD §11: 'store explicitly stated geography separately from profile
    geography and model inference.' A mention of a city in the text does NOT
    populate `explicit_location` — only the classifier setting it after reading
    genuine location context does."""
    explicit_location: Optional[str] = None
    source_field_location: Optional[str] = None  # e.g. the platform account's own location field
    inferred_location: Optional[str] = None       # model guess — never used for eligibility


# ── Evidence (PRD §11, §19) ─────────────────────────────────────────────────────

class RawEvidence(BaseModel):
    """Adapter output — deliberately has NO tenant fields. A public post isn't
    inherently owned by any brand/workspace; it only becomes tenant-scoped
    `Evidence` once the collection orchestrator attaches it to the topic that
    requested it. Keeping adapters tenant-agnostic means the same adapter output
    shape works whether one topic or fifty topics happen to match the same post,
    and rules out a whole class of "forgot to stamp the real brand_id" bugs."""

    provider: str            # e.g. "apify", "mock"
    platform: str            # e.g. "x", "instagram", "news"
    source_id: str           # stable id from the platform/provider — dedup key
    url: Optional[str] = None

    content_type: str = "post"  # post | reply | comment | article
    parent_id: Optional[str] = None  # thread/parent post this replies to, if any

    text: str
    language: str = "en"
    original_text: Optional[str] = None  # set when `text` is a translation

    author_handle: Optional[str] = None
    author_is_verified: Optional[bool] = None

    published_at: Optional[datetime] = None
    collected_at: datetime

    geography: Geography = Field(default_factory=Geography)
    duplicate_of: Optional[str] = None  # another record's source_id, if this is a repost/near-dup
    raw_metrics: dict[str, int] = Field(default_factory=dict)  # likes/replies/etc, source-defined keys only


class Evidence(RawEvidence):
    """A `RawEvidence` record once attached to a topic — PRD §19's Evidence
    entity. Required-field discipline: tenant scope, provider, platform, a
    stable source id/url, and collected_at are non-negotiable; published_at may
    be absent (undated context only, never freshness-sensitive alerts —
    enforced in scoring.py)."""

    id: str
    brand_id: str
    user_id: str
    topic_id: str


class EvidenceSnapshot(BaseModel):
    """PRD §19: engagement metrics are only ever recorded at the moment they were
    observed — a historical post fetched today does not reveal its metrics at
    publication."""
    evidence_id: str
    observed_at: datetime
    metrics: dict[str, int] = Field(default_factory=dict)


# ── Classification (PRD §10, §19) ──────────────────────────────────────────────

class Classification(BaseModel):
    """Structured LLM output for one piece of evidence. `evidence_span` is the
    exact substring the model based its label on — required so every downstream
    claim can point at real text, not a paraphrase."""
    evidence_id: str
    primary_type: EvidenceType
    secondary_tags: list[EvidenceType] = Field(default_factory=list)
    evidence_span: str
    uncertain: bool = False
    reasoning: str

    # Concern-specific structured fields (PRD §10 "Concern structure") — populated
    # only when primary_type == CUSTOMER_CONCERN, else left None.
    desired_outcome: Optional[str] = None
    obstacle: Optional[str] = None
    current_alternative: Optional[str] = None

    model_name: str = "gpt-4o-mini"
    prompt_version: str = "mi-classify-v1"
    classified_at: datetime = Field(default_factory=datetime.utcnow)


# ── Scoring (PRD §12) ───────────────────────────────────────────────────────────

class ComponentScore(BaseModel):
    """One 0-2-point component of the confidence or relevance rubric."""
    name: str
    points: int = Field(ge=0, le=2)
    reason: str


class ScoreBreakdown(BaseModel):
    components: list[ComponentScore]
    total: int = Field(ge=0, le=10)
    band: ConfidenceBand

    @staticmethod
    def band_for(total: int) -> ConfidenceBand:
        if total <= 4:
            return ConfidenceBand.LOW
        if total <= 7:
            return ConfidenceBand.MEDIUM
        return ConfidenceBand.HIGH


class UrgencyAssessment(BaseModel):
    is_urgent: bool
    deadline: Optional[datetime] = None
    reason: str


# ── Clustering (PRD §11, §19) ───────────────────────────────────────────────────

class Cluster(BaseModel):
    id: str
    brand_id: str
    topic_id: str
    primary_type: EvidenceType
    theme: str  # short human label, e.g. "Delivery delays to Lekki"
    evidence_ids: list[str]
    independent_account_count: int
    original_thread_count: int
    first_seen: datetime
    last_updated: datetime
    lifecycle: Lifecycle = Lifecycle.UNKNOWN


# ── Insight (PRD §13, §19) ──────────────────────────────────────────────────────

class InsightVersion(BaseModel):
    id: str
    revision: int = 1
    brand_id: str
    topic_id: str
    cluster_id: Optional[str] = None

    type: EvidenceType
    headline: str
    observed_change: str        # "what the evidence says"
    business_implication: str   # "what Uri infers"
    suggested_next_step: str    # "what Uri recommends"
    assumptions: list[str] = Field(default_factory=list)  # required if any forecast language is used

    evidence_ids: list[str]
    confidence: ScoreBreakdown
    relevance: ScoreBreakdown
    urgency: UrgencyAssessment

    lifecycle: Lifecycle = Lifecycle.UNKNOWN
    status: InsightStatus = InsightStatus.ACTIVE

    coverage_note: Optional[str] = None  # set when comparability/coverage is degraded

    first_seen: datetime
    last_updated: datetime


# ── Topic / source config / collection run (PRD §9, §17, §19) ─────────────────

class SourceConfig(BaseModel):
    """PRD §8 capability gate, trimmed to what MockSourceAdapter and the first
    real adapter actually need. Extend before adding a second real provider."""
    provider: str
    platform: str
    enabled: bool = True
    verified_lookback_days: int = 30
    refresh_cadence_hours: int = 1


class Topic(BaseModel):
    id: str
    brand_id: str
    user_id: str
    question: str
    keywords: list[str]
    excluded_keywords: list[str] = Field(default_factory=list)
    sources: list[SourceConfig]
    geographic_scope: Optional[str] = None
    requested_days: int = 30
    keep_updating: bool = False
    active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CollectionRun(BaseModel):
    id: str
    topic_id: str
    brand_id: str
    source_provider: str
    status: ScanStatus = ScanStatus.QUEUED
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    evidence_collected: int = 0
    gaps: list[str] = Field(default_factory=list)  # human-readable partial-coverage notes
    estimated_cost_usd: float = 0.0


# ── Action handoff (PRD §13) ────────────────────────────────────────────────────

class ActionBrief(BaseModel):
    id: str
    insight_id: str
    insight_revision: int
    brand_id: str
    customer_need: str
    proposed_message: str
    evidence_ids: list[str]
    destination: str = "market_intelligence"  # "campaign" once handoff is wired
    destination_ref_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class FeedbackOutcome(BaseModel):
    id: str
    insight_id: str
    user_id: str
    verdict: FeedbackVerdict
    reason: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ── API request/response shapes ────────────────────────────────────────────────

class TopicCreateRequest(BaseModel):
    question: str
    keywords: Optional[list[str]] = None
    excluded_keywords: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=lambda: ["mock"])
    geographic_scope: Optional[str] = None
    requested_days: int = 30
    keep_updating: bool = False


class ScanRequest(BaseModel):
    pass  # nothing required yet — budget/limit checks read from the topic + brand


class SourceCoveragePreview(BaseModel):
    """PRD §9: 'Before running, show the accessible period, limits, collection
    scope and estimated usage. A shorter available period must never silently
    replace the requested one.' One entry per configured source, computed
    from that source's own AdapterCapabilities.verified_lookback_days —
    never mutates the topic's own requested_days."""
    provider: str
    requested_days: int
    accessible_days: int
    capped: bool
    note: Optional[str] = None
    estimated_cost_usd: float = 0.0


class FeedbackRequest(BaseModel):
    verdict: FeedbackVerdict
    reason: Optional[str] = None


class BriefCreateRequest(BaseModel):
    proposed_message: Optional[str] = None  # None → draft one from the insight

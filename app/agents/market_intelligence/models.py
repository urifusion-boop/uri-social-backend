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


class NotificationCategory(str, Enum):
    """PRD §14's delivery-rules table. EARLY_SIGNAL deliberately never
    produces an outbox entry (PRD: 'Watchlist only') — an early-signal
    insight is already visible the moment the user opens Intelligence home,
    so there's nothing additional to queue."""
    ACT_SOON = "act_soon"
    QUALIFIED_INQUIRY = "qualified_inquiry"
    PREPARE = "prepare"
    USEFUL_PATTERN = "useful_pattern"
    EARLY_SIGNAL = "early_signal"
    MATERIAL_UPDATE = "material_update"
    COOLING = "cooling"


class OutboxDeliveryMode(str, Enum):
    IMMEDIATE = "immediate"
    DIGEST = "digest"


class OutboxStatus(str, Enum):
    QUEUED = "queued"
    SUPPRESSED = "suppressed"
    SENT = "sent"
    FAILED = "failed"


class DevelopmentStatus(str, Enum):
    """PRD §13: 'Unknown dates are "Date to confirm," with no invented
    countdown.' DATE_TO_CONFIRM is the honest default — SCHEDULED requires an
    actual known event_date, never a guess."""
    DATE_TO_CONFIRM = "date_to_confirm"
    SCHEDULED = "scheduled"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    OCCURRED = "occurred"


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

    # Internal — never shown to the frontend. The average embedding of this
    # cluster's own members, kept so a LATER scan can recognize "this new
    # batch of evidence is the same ongoing conversation" without re-fetching
    # and re-embedding every old member's text (PRD §11/§19: "keep a stable
    # cluster ID through ordinary updates").
    embedding_centroid: Optional[list[float]] = None

    # Internal lifecycle-tracking counters (PRD §12) — see
    # classification/scoring.py's compute_lifecycle() docstring for exactly
    # what each counts. Only meaningfully used for EMERGING_TREND clusters
    # today; left at 0 for other clusterable types until they get their own
    # eligibility bars.
    eligible_evaluation_count: int = 0
    consecutive_ineligible_count: int = 0


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


class Development(BaseModel):
    """PRD §13 'Upcoming development workflow' + §19 Development entity.
    One per originating evidence record — 'Postponement or cancellation
    updates the SAME item' (§13), so this is never re-created, only PATCHed.
    occurrence date (event_date) is kept explicitly separate from the
    evidence's own published_at (publication date) — conflating the two is
    exactly the mistake §13 calls out."""
    id: str
    brand_id: str
    topic_id: str
    evidence_id: str  # the original announcement this was extracted from

    issuer: Optional[str] = None
    headline: str
    event_date: Optional[datetime] = None       # None -> DevelopmentStatus.DATE_TO_CONFIRM
    event_date_range_end: Optional[datetime] = None
    location: Optional[str] = None
    registration_deadline: Optional[datetime] = None
    preparation_action: Optional[str] = None    # PRD §12: "a business-relevant preparation action"
    source_url: Optional[str] = None

    status: DevelopmentStatus = DevelopmentStatus.DATE_TO_CONFIRM
    verification_note: Optional[str] = None

    first_seen: datetime
    last_updated: datetime


class DevelopmentUpdateRequest(BaseModel):
    """PATCH body for postponement/cancellation/occurrence updates — PRD
    §13: these revise the existing item, never create a new one."""
    status: DevelopmentStatus
    event_date: Optional[datetime] = None  # set when postponing to a new date
    verification_note: Optional[str] = None


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


class MIOutboxEntry(BaseModel):
    """PRD §14 engineering note: 'Use an outbox written in the same database
    transaction as the insight revision. A delivery worker applies recipient
    preferences, quiet hours, caps and expiry at send time.' This row only
    ever records WHAT should be considered for email delivery and WHY
    (dedupe_key) — every eligibility decision (muted, snoozed, quiet hours,
    daily cap, topic cooldown, expiry) is made later, at send time, by the
    delivery worker, never here at write time. 'In-app' isn't a separate
    entry: PRD's defaults section makes in-app availability the baseline
    ("available by default") satisfied simply by the insight being active
    and queryable — only the additive email channel needs this machinery."""
    id: str
    brand_id: str
    user_id: str  # recipient — the topic's owner in this pilot
    topic_id: str
    insight_id: str
    insight_revision: int
    category: NotificationCategory
    delivery_mode: OutboxDeliveryMode
    dedupe_key: str  # tenant + insight + revision (material-change signature) + category + recipient
    status: OutboxStatus = OutboxStatus.QUEUED
    suppression_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    sent_at: Optional[datetime] = None
    failed_reason: Optional[str] = None


class MINotificationPreferences(BaseModel):
    """PRD §14: 'Users manage their own notification preferences; workspace
    policies set maximum delivery and spending limits.' Scoped per
    (user_id, brand_id) since agency staff can hold different preferences
    per client workspace."""
    user_id: str
    brand_id: str
    email_enabled: bool = False  # PRD: "Email is opt-in per recipient"
    timezone: str = "Africa/Lagos"  # PRD: "initialise Nigerian workspaces to Africa/Lagos"
    digest_hour_local: int = 8  # PRD default: 08:00 workspace timezone
    quiet_hours_start_local: int = 21
    quiet_hours_end_local: int = 8
    urgent_override: bool = False  # PRD: lets an explicit change bypass quiet hours too
    muted_topic_ids: list[str] = Field(default_factory=list)
    muted_categories: list[NotificationCategory] = Field(default_factory=list)
    snoozed_insight_ids: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class PreferencesUpdateRequest(BaseModel):
    email_enabled: Optional[bool] = None
    timezone: Optional[str] = None
    digest_hour_local: Optional[int] = None
    quiet_hours_start_local: Optional[int] = None
    quiet_hours_end_local: Optional[int] = None
    urgent_override: Optional[bool] = None
    mute_topic_id: Optional[str] = None
    unmute_topic_id: Optional[str] = None
    mute_category: Optional[NotificationCategory] = None
    unmute_category: Optional[NotificationCategory] = None


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


class BrandBudget(BaseModel):
    """PRD §23: 'Before a scan, reserve a conservative maximum cost from the
    workspace allowance... reconcile actual charges and release unused
    reservation.' Tracked per calendar month (period = 'YYYY-MM'), not a
    rolling 30-day window — simpler and matches how a billing allowance is
    normally understood. monthly_allowance_usd's pilot default is
    deliberately small; this is a cost-control ceiling, not a spend target."""
    brand_id: str
    monthly_allowance_usd: float = 10.0
    period: str  # "YYYY-MM"
    reserved_usd: float = 0.0
    spent_usd: float = 0.0
    updated_at: datetime = Field(default_factory=datetime.utcnow)


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

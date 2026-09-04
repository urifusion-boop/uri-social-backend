"""
Jane + Ads — corpus retrieval (ASC-SPEC-01 v2 §6–§8, ASC-ENG-01 v1 §4–§5).

Exclusion runs before scoring, always. ENG §4: scoring a record you are about to
exclude wastes work and invites someone to "just lower the threshold" instead of
fixing a precondition.

The confidence gate produces OMISSION, not a weak answer. A user who learns to
ignore Jane's reasoning is a worse outcome than a shorter plan (spec §8.3).

Empty retrieval is data, not an error. Which stage / tier / business-type /
conversion-location combinations return nothing IS the seeding roadmap, and
before outcomes exist it is the most useful signal the system produces (§8.4).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .entities import (
    ConsumedBy,
    ConversionLocation,
    EvidenceGrade,
    ExecutableVia,
    LocalTestStatus,
    MarketOrigin,
    PooledAccountSafety,
    Requirement,
    Strategy,
    StrategyPlatform,
    StrategyStatus,
    TransferVerdict,
)

# ── Thresholds (ENG §2). Configuration, not constants — expect to tune. ──────
CONFIDENCE_THRESHOLD = 0.40
MAX_RECORDS_PER_STAGE = 5
TIER_FOR_PARALLEL_ADSETS = 3


class ExclusionReason(str, Enum):
    """ENG §5.1. The FIRST failing check wins, in declaration order — the operator
    view is more useful saying "budget too low" than "not approved" when both hold."""
    NOT_APPROVED = "not_approved"
    DOES_NOT_TRANSFER = "does_not_transfer"
    UNDERPERFORMED_LOCALLY = "underperformed_locally"
    RETIRED = "retired"
    IMPLIES_PRODUCT_CHANGE = "implies_product_change"
    POOLED_ACCOUNT_UNSAFE = "pooled_account_unsafe"
    POOLED_ACCOUNT_UNKNOWN = "pooled_account_unknown"
    REQUIRES_ISOLATION_UNAVAILABLE = "requires_isolation_unavailable"
    NOT_API_EXECUTABLE = "not_api_executable"
    STAGE_MISMATCH = "stage_mismatch"
    PLATFORM_MISMATCH = "platform_mismatch"
    CONVERSION_LOCATION_MISMATCH = "conversion_location_mismatch"
    BUDGET_FLOOR_EXCEEDS_DAILY_SPEND = "budget_floor_exceeds_daily_spend"
    SUSTAINED_CAPACITY_UNKNOWN = "sustained_capacity_unknown"
    SUSTAINED_CAPACITY_INSUFFICIENT = "sustained_capacity_insufficient"
    TIER_GATE_FAILED = "tier_gate_failed"
    INFRASTRUCTURE_MISSING = "infrastructure_missing"
    BELOW_CONFIDENCE_THRESHOLD = "below_confidence_threshold"


@dataclass
class BusinessProfile:
    conversion_location: ConversionLocation = ConversionLocation.MESSAGING
    has_website: bool = False
    has_customer_list: bool = False
    records_outcomes: bool = False
    has_video_asset: bool = False
    has_winning_creative: bool = False
    isolated_ad_account: bool = False
    # VSG-01 v3 §1.2/§6 — whether a real photo of the actual product exists to
    # build an upload_as_is/recomposite format from. Distinct from
    # has_video_asset: a business can have a product photo with no video, or
    # vice versa.
    has_product_photo: bool = False
    # Whether a real customer's photo + permission-on-file exists, for formats
    # that pair an image with a first-person claim (§1.2).
    has_real_customer_photo: bool = False


@dataclass
class BudgetContext:
    daily_spend_ngn: float
    budget_tier: int
    sustained_daily_ngn: Optional[float] = None
    sustained_known: bool = False


@dataclass
class RetrievalRequest:
    stage: ConsumedBy
    platforms: list[StrategyPlatform]
    budget: BudgetContext
    profile: BusinessProfile = field(default_factory=BusinessProfile)

    def __post_init__(self):
        # Spec §9.2 / ENG fixture 13 — retrieving before the platform decision
        # returns records for platforms that will not be used.
        if not self.platforms:
            raise ValueError("platforms is required — retrieval fires AFTER platform selection")


@dataclass
class Excluded:
    strategy_id: str
    reason: ExclusionReason
    detail: Optional[str] = None

    def __str__(self) -> str:
        return f"{self.reason.value}:{self.detail}" if self.detail else self.reason.value


@dataclass
class RetrievalResult:
    records: list[Strategy]
    coverage: str                      # 'none' | 'partial' | 'full'
    excluded: list[Excluded]
    scores: dict[str, float] = field(default_factory=dict)


# ── Infrastructure preconditions (spec §7.3) ─────────────────────────────────
def _requirement_met(req: Requirement, p: BusinessProfile, tier: int) -> bool:
    return {
        Requirement.OUTCOME_CAPTURE: p.records_outcomes,
        Requirement.WEBSITE_OR_PIXEL: p.has_website,
        Requirement.CUSTOMER_LIST: p.has_customer_list,
        Requirement.PARALLEL_ADSET_BUDGET: tier >= TIER_FOR_PARALLEL_ADSETS,
        Requirement.CREATIVE_PRODUCTION: tier >= TIER_FOR_PARALLEL_ADSETS,
        Requirement.VIDEO_ASSET: p.has_video_asset,
        Requirement.EXISTING_WINNING_CREATIVE: p.has_winning_creative,
        Requirement.PRODUCT_PHOTO: p.has_product_photo,
        Requirement.REAL_CUSTOMER_PHOTO: p.has_real_customer_photo,
    }[req]


def exclusion_reason(rec: Strategy, req: RetrievalRequest) -> Optional[Excluded]:
    """Spec §7.1 — first failing check wins. Order matches ENG §5.1."""
    sid = rec.strategy_id

    if rec.status is not StrategyStatus.APPROVED:
        return Excluded(sid, ExclusionReason.NOT_APPROVED)

    if rec.transfer_verdict is TransferVerdict.DOES_NOT_TRANSFER:
        # Retained for anti-duplication; never retrieves.
        return Excluded(sid, ExclusionReason.DOES_NOT_TRANSFER)

    if rec.local.test_status is LocalTestStatus.UNDERPERFORMED_LOCALLY:
        return Excluded(sid, ExclusionReason.UNDERPERFORMED_LOCALLY)
    if rec.local.test_status is LocalTestStatus.RETIRED:
        return Excluded(sid, ExclusionReason.RETIRED)

    if rec.implies_product_change:
        # Routes to the product backlog, never to a plan.
        return Excluded(sid, ExclusionReason.IMPLIES_PRODUCT_CHANGE)

    if rec.pooled_account_safe is PooledAccountSafety.NO:
        return Excluded(sid, ExclusionReason.POOLED_ACCOUNT_UNSAFE)
    if rec.pooled_account_safe is PooledAccountSafety.UNKNOWN:
        return Excluded(sid, ExclusionReason.POOLED_ACCOUNT_UNKNOWN)
    if (
        rec.pooled_account_safe is PooledAccountSafety.REQUIRES_ISOLATION
        and not req.profile.isolated_ad_account
    ):
        return Excluded(sid, ExclusionReason.REQUIRES_ISOLATION_UNAVAILABLE)

    # Team knowledge is legitimately useful to an operator at diagnostics (§7.1.10).
    if rec.executable_via is not ExecutableVia.API and req.stage is not ConsumedBy.DIAGNOSTICS:
        return Excluded(sid, ExclusionReason.NOT_API_EXECUTABLE)

    if req.stage not in rec.consumed_by:
        return Excluded(sid, ExclusionReason.STAGE_MISMATCH)

    # CROSS_PLATFORM is a wildcard, not a seventh platform: a tactic that applies
    # everywhere must not be excluded from a Meta-only request. Treating it as a
    # literal value silently dropped 20 of 55 records — the same failure class as a
    # precondition that excludes the accounts it was written for.
    if StrategyPlatform.CROSS_PLATFORM not in rec.platforms:
        if not set(rec.platforms) & set(req.platforms):
            return Excluded(sid, ExclusionReason.PLATFORM_MISMATCH)

    if rec.conversion_location and ConversionLocation.ANY not in rec.conversion_location:
        if req.profile.conversion_location not in rec.conversion_location:
            return Excluded(sid, ExclusionReason.CONVERSION_LOCATION_MISMATCH)

    floor = rec.budget_floor_ngn_daily
    if floor is not None and floor > req.budget.daily_spend_ngn:
        return Excluded(sid, ExclusionReason.BUDGET_FLOOR_EXCEEDS_DAILY_SPEND,
                        f"₦{floor:,.0f} > ₦{req.budget.daily_spend_ngn:,.0f}")

    if rec.requires_sustained_days > 1:
        if not req.budget.sustained_known:
            return Excluded(sid, ExclusionReason.SUSTAINED_CAPACITY_UNKNOWN)
        sustained = req.budget.sustained_daily_ngn or 0.0
        if floor is not None and sustained < floor:
            return Excluded(sid, ExclusionReason.SUSTAINED_CAPACITY_INSUFFICIENT,
                            f"₦{sustained:,.0f} < ₦{floor:,.0f}")

    for r in rec.requires:
        if not _requirement_met(r, req.profile, req.budget.budget_tier):
            return Excluded(sid, ExclusionReason.INFRASTRUCTURE_MISSING, r.value)

    if Requirement.PARALLEL_ADSET_BUDGET in rec.requires and req.budget.budget_tier < TIER_FOR_PARALLEL_ADSETS:
        return Excluded(sid, ExclusionReason.TIER_GATE_FAILED)

    return None


def recency_weight(due: Optional[datetime], now: Optional[datetime] = None) -> float:
    """1.0 fresh -> 0.6 approaching the review date -> 0.3 past due (spec §8.1).
    Overdue records stay approved: platform behaviour changing does not make a
    record wrong, only unverified (ENG §1.4)."""
    if due is None:
        return 1.0
    now = now or datetime.now(timezone.utc)
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    days_left = (due - now).days
    if days_left < 0:
        return 0.3
    if days_left <= 30:
        return 0.6
    return 1.0


def score(rec: Strategy, now: Optional[datetime] = None) -> float:
    """grade × transfer × recency × origin (spec §8.1).

    The inversion: a locally-confirmed C-grade takes effective grade A, and the
    origin bonus — which only a Nigerian-observed record earns — breaks the tie
    against an untested A-grade. v1's additive modifier produced a tie instead.
    """
    gw = rec.effective_grade.weight
    tw = 1.0 if rec.transfer_verdict is TransferVerdict.APPLIES_AS_IS else 0.85
    rw = recency_weight(rec.staleness_review_due, now)
    ob = 1.2 if rec.market_origin is MarketOrigin.NIGERIA else 1.0
    return round(gw * tw * rw * ob, 3)


def retrieve(
    candidates: list[Strategy],
    req: RetrievalRequest,
    *,
    threshold: float = CONFIDENCE_THRESHOLD,
    limit: int = MAX_RECORDS_PER_STAGE,
    now: Optional[datetime] = None,
) -> RetrievalResult:
    excluded: list[Excluded] = []
    survivors: list[Strategy] = []

    for rec in candidates:
        why = exclusion_reason(rec, req)
        if why:
            excluded.append(why)
        else:
            survivors.append(rec)

    scores = {r.strategy_id: score(r, now) for r in survivors}

    ranked = sorted(survivors, key=lambda r: (-scores[r.strategy_id], r.strategy_id))
    kept = []
    for r in ranked:
        if scores[r.strategy_id] >= threshold:
            kept.append(r)
        else:
            excluded.append(Excluded(r.strategy_id, ExclusionReason.BELOW_CONFIDENCE_THRESHOLD,
                                     f"{scores[r.strategy_id]:.3f}"))

    if not kept:
        # Caller logs the gap — do not swallow (ENG §5.3).
        return RetrievalResult([], "none", excluded, scores)

    return RetrievalResult(
        kept[:limit],
        "partial" if len(kept) < 3 else "full",
        excluded,
        scores,
    )


def gap_record(req: "RetrievalRequest", now: Optional[datetime] = None) -> dict:
    """The row ENG §5.3 wants logged on an empty retrieval. Full context, so the
    seeding roadmap can be read straight off the gap table."""
    return {
        "stage": req.stage.value,
        "budget_tier": req.budget.budget_tier,
        "daily_spend_ngn": req.budget.daily_spend_ngn,
        "conversion_location": req.profile.conversion_location.value,
        "platforms": [p.value for p in req.platforms],
        "occurred_at": now or datetime.now(timezone.utc),
    }


def top_exclusions(excluded: list[Excluded], n: int = 10) -> list[tuple[str, int]]:
    """ENG §5.2 — the operator view. If pooled_account_unknown dominates, the Phase 0
    pass is incomplete; that is the signal, not a bug report."""
    counts: dict[str, int] = {}
    for e in excluded:
        counts[str(e)] = counts.get(str(e), 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:n]


def corpus_directive(result: Optional[RetrievalResult], *, applies_to: str,
                     forbid: str = "") -> str:
    """Render retrieved records as binding house rules for a prompt.

    Worded as rules that override the model's defaults, not as background reading:
    the plan-generation stage proved that an advisory framing gets ignored even
    when the same constraint already appears elsewhere in the prompt.

    `applies_to` names what the records may shape at this stage; `forbid` names what
    they must not touch, which differs per stage — at creative_brief the hard line is
    Zone A (spec §10: a record must never introduce a targeting parameter into
    creative instruction), at plan_generation it is trade_off (§9.3).
    """
    if not result or not result.records:
        return ""
    lines = []
    for r in result.records:
        line = f"- {r.claim.rstrip('.')}. Why: {r.mechanism}"
        if r.modification_required:
            line += f" (applies here only with this change: {r.modification_required})"
        lines.append(line)
    out = (
        "## HOUSE RULES — these override your defaults\n"
        "Our own validated findings at Nigerian SME budgets. Apply them, do not "
        "merely consider them.\n\n" + "\n".join(lines) + "\n\n"
        f"They apply to: {applies_to}\n"
    )
    if forbid:
        out += f"They must NOT influence: {forbid}\n"
    return out + "\n"

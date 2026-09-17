"""
Uri Market Intelligence — scoring (PRD §12).

PRD §12 specifies the component structure (5 components, 0-2 points each, for
both confidence and relevance) and the eligibility bars per type, but
deliberately leaves the exact per-component scoring logic as "configurable,
versioned pilot defaults" rather than a fixed formula — this file is that
implementation, documented component-by-component so it can be tuned without
guessing what the original intent was.

Confidence is scored deterministically from evidence/cluster facts (no LLM
call) — PRD's own framing calls it "an ordinal heuristic, not a calibrated
probability," and evidence integrity is exactly the place NOT to add another
point of LLM fallibility. Relevance is a documented heuristic for this first
pass (see relevance_breakdown) — matching business context semantically is a
judgment call that may warrant an LLM in a later version, called out explicitly
rather than silently hardcoded as if it were rigorous.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from ..models import (
    ComponentScore,
    Evidence,
    EvidenceType,
    Lifecycle,
    ScoreBreakdown,
    UrgencyAssessment,
)

SCORING_VERSION = "mi-scoring-v1"

# PRD §12 type-specific eligibility bars (pilot defaults).
CONCERN_MIN_ACCOUNTS = 5
CONCERN_MIN_THREADS = 3
CONCERN_WINDOW_DAYS = 7
INQUIRY_FRESHNESS_HOURS = 48

# PRD §12 trend eligibility bar (pilot defaults).
TREND_MIN_LATEST_ACCOUNTS = 10
TREND_MIN_BASELINE_PER_DAY = 3.0
TREND_MIN_INCREASE = 5
TREND_BASELINE_DAYS = 7
TREND_DOMINANT_CASCADE_SHARE = 0.5  # a single account driving >50% of latest-day volume


def _score(total: int, components: list[ComponentScore]) -> ScoreBreakdown:
    return ScoreBreakdown(components=components, total=total, band=ScoreBreakdown.band_for(total))


def confidence_breakdown(cluster_evidence: list[Evidence], original_thread_count: int) -> ScoreBreakdown:
    """5 components, 0-2 points each, over the evidence backing ONE cluster."""
    n = len(cluster_evidence)
    if n == 0:
        return _score(0, [ComponentScore(name="provenance", points=0, reason="no evidence")])

    with_provenance = sum(1 for e in cluster_evidence if e.url and e.author_handle)
    provenance_pts = 2 if with_provenance == n else (1 if with_provenance > 0 else 0)

    # contextual_clarity needs the classification's uncertain flag, which isn't
    # on Evidence itself — callers pass it in via cluster_evidence's ordering
    # matching a parallel `uncertain_flags` list where needed; for this first
    # pass, clarity is inferred from whether the evidence has real body text
    # (a proxy until classification results are threaded through here too).
    with_substance = sum(1 for e in cluster_evidence if len(e.text.strip()) >= 20)
    clarity_pts = 2 if with_substance == n else (1 if with_substance > 0 else 0)

    independent_accounts = len({e.author_handle for e in cluster_evidence if e.author_handle})
    independence_pts = 2 if independent_accounts >= n else (1 if independent_accounts > 1 else 0)

    with_real_dates = sum(1 for e in cluster_evidence if e.published_at is not None)
    time_quality_pts = 2 if with_real_dates == n else (1 if with_real_dates > 0 else 0)

    corroboration_pts = 2 if original_thread_count >= 3 else (1 if original_thread_count == 2 else 0)

    components = [
        ComponentScore(name="provenance", points=provenance_pts, reason=f"{with_provenance}/{n} records have both url and author"),
        ComponentScore(name="contextual_clarity", points=clarity_pts, reason=f"{with_substance}/{n} records have substantive text"),
        ComponentScore(name="independence", points=independence_pts, reason=f"{independent_accounts} independent accounts across {n} records"),
        ComponentScore(name="time_quality", points=time_quality_pts, reason=f"{with_real_dates}/{n} records have a real publication date"),
        ComponentScore(name="corroboration", points=corroboration_pts, reason=f"{original_thread_count} original threads"),
    ]
    return _score(sum(c.points for c in components), components)


def relevance_breakdown(cluster_evidence: list[Evidence], business_context: dict) -> ScoreBreakdown:
    """Heuristic, not semantic — flagged explicitly in this module's docstring.
    Keyword-overlap against business_context fields, not an LLM judgement call.
    Tune or replace with a model-based scorer once precision is measured
    against the PRD §26 eval set; don't assume this is "the real" relevance
    logic."""
    products = [p.lower() for p in (business_context.get("key_products_services") or [])]
    served_locations = [l.lower() for l in (business_context.get("service_locations") or [])]
    goal = (business_context.get("primary_goal") or "").lower()

    combined_text = " ".join(e.text.lower() for e in cluster_evidence)

    product_fit_pts = 2 if any(p in combined_text for p in products) else (1 if products else 0)

    mentioned_locations = {e.geography.explicit_location.lower() for e in cluster_evidence if e.geography.explicit_location}
    geo_fit_pts = (
        2 if served_locations and mentioned_locations and any(loc in " ".join(served_locations) for loc in mentioned_locations)
        else (1 if mentioned_locations else 0)
    )

    # customer_need_fit: crude proxy — presence of concern/inquiry language at all
    # already passed the classifier, so this component rewards having MULTIPLE
    # corroborating records over a single anecdote.
    need_fit_pts = 2 if len(cluster_evidence) >= CONCERN_MIN_ACCOUNTS else (1 if len(cluster_evidence) >= 2 else 0)

    # PRD §12: "Unknown stock or delivery capability scores 0 for feasibility."
    fulfilment_known = business_context.get("stock_availability") is not None or business_context.get("delivery_capability") is not None
    feasibility_pts = 1 if fulfilment_known else 0  # never 2 — feasibility alone can't be fully confirmed by evidence

    alignment_pts = 1 if goal else 0  # no goal on record → can't claim alignment

    components = [
        ComponentScore(name="product_fit", points=product_fit_pts, reason="keyword overlap with key_products_services"),
        ComponentScore(name="served_geography", points=geo_fit_pts, reason="explicit location overlap with service_locations"),
        ComponentScore(name="customer_need_fit", points=need_fit_pts, reason=f"{len(cluster_evidence)} corroborating records"),
        ComponentScore(name="fulfilment_feasibility", points=feasibility_pts, reason="stock/delivery facts on record" if fulfilment_known else "stock/delivery unknown"),
        ComponentScore(name="alignment_with_goal", points=alignment_pts, reason="business goal on record" if goal else "no stated business goal"),
    ]
    return _score(sum(c.points for c in components), components)


def _naive_utc(dt: datetime) -> datetime:
    """Normalise to a naive UTC datetime so age comparisons never raise on a
    mix of aware/naive inputs — adapters may hand back either."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def urgency_for_inquiry(evidence: Evidence, now: Optional[datetime] = None) -> UrgencyAssessment:
    now = _naive_utc(now or datetime.utcnow())
    if evidence.published_at is None:
        return UrgencyAssessment(is_urgent=False, reason="undated — cannot assess freshness")
    published_at = _naive_utc(evidence.published_at)
    age = now - published_at
    deadline = published_at + timedelta(hours=INQUIRY_FRESHNESS_HOURS)
    if age > timedelta(hours=INQUIRY_FRESHNESS_HOURS):
        return UrgencyAssessment(is_urgent=False, deadline=deadline, reason=f"older than {INQUIRY_FRESHNESS_HOURS}h freshness window")
    return UrgencyAssessment(is_urgent=True, deadline=deadline, reason=f"within {INQUIRY_FRESHNESS_HOURS}h freshness window")


def urgency_for_concern(original_thread_count: int) -> UrgencyAssessment:
    # Concerns don't have a hard deadline the way inquiries do — PRD treats
    # urgency for a cluster as "how quickly a useful action window closes,"
    # which for a concern is a judgment call this pilot doesn't automate.
    return UrgencyAssessment(is_urgent=False, reason="concern clusters are not deadline-driven in this pilot")


def is_concern_eligible(evidence_list: list[Evidence], original_thread_count: int, now: Optional[datetime] = None) -> tuple[bool, str]:
    """PRD §12: at least 5 independent accounts across 3 original threads within 7 days."""
    now = _naive_utc(now or datetime.utcnow())
    window_start = now - timedelta(days=CONCERN_WINDOW_DAYS)
    in_window = [e for e in evidence_list if e.published_at and _naive_utc(e.published_at) >= window_start]
    independent_accounts = len({e.author_handle for e in in_window if e.author_handle})

    if independent_accounts < CONCERN_MIN_ACCOUNTS:
        return False, f"only {independent_accounts}/{CONCERN_MIN_ACCOUNTS} independent accounts in the last {CONCERN_WINDOW_DAYS} days"
    if original_thread_count < CONCERN_MIN_THREADS:
        return False, f"only {original_thread_count}/{CONCERN_MIN_THREADS} original threads"
    return True, "eligible"


def is_inquiry_eligible(evidence: Evidence, now: Optional[datetime] = None) -> tuple[bool, str]:
    """PRD §12: one clear, unresolved-looking request within the 48h freshness limit."""
    urgency = urgency_for_inquiry(evidence, now)
    if evidence.published_at is None:
        return False, "undated — routes to review, not the active inquiry queue"
    if not urgency.is_urgent:
        return False, f"expired — {urgency.reason}"
    return True, "eligible"


def is_trend_eligible(cluster_evidence: list[Evidence], now: Optional[datetime] = None) -> tuple[bool, str]:
    """PRD §12: 'at least ten independent accounts in the latest complete
    24-hour window, a minimum baseline of three accounts per day, at least
    twice the seven-day baseline, and an increase of at least five
    accounts... a check for a single dominant repost cascade.' Requires the
    cluster's FULL historical membership (not one scan's batch) — only
    meaningful now that clusters persist evidence across scans."""
    now = _naive_utc(now or datetime.utcnow())
    latest_window_start = now - timedelta(hours=24)
    baseline_start = now - timedelta(days=1 + TREND_BASELINE_DAYS)
    baseline_end = latest_window_start

    dated = [e for e in cluster_evidence if e.published_at is not None]
    latest_evidence = [e for e in dated if _naive_utc(e.published_at) >= latest_window_start]
    baseline_evidence = [e for e in dated if baseline_start <= _naive_utc(e.published_at) < baseline_end]

    latest_accounts = {e.author_handle for e in latest_evidence if e.author_handle}
    baseline_accounts = {e.author_handle for e in baseline_evidence if e.author_handle}
    baseline_per_day = len(baseline_accounts) / TREND_BASELINE_DAYS

    if len(latest_accounts) < TREND_MIN_LATEST_ACCOUNTS:
        return False, (
            f"only {len(latest_accounts)}/{TREND_MIN_LATEST_ACCOUNTS} independent accounts "
            "in the latest complete 24h window"
        )

    if baseline_per_day < TREND_MIN_BASELINE_PER_DAY:
        return False, (
            f"baseline of {baseline_per_day:.1f} accounts/day is below the "
            f"{TREND_MIN_BASELINE_PER_DAY:.0f}/day minimum needed to assess a trend"
        )

    if len(latest_accounts) < 2 * baseline_per_day:
        return False, (
            f"latest-day volume ({len(latest_accounts)}) is not at least twice "
            f"the 7-day baseline ({baseline_per_day:.1f}/day)"
        )

    if (len(latest_accounts) - baseline_per_day) < TREND_MIN_INCREASE:
        return False, (
            f"increase of {len(latest_accounts) - baseline_per_day:.1f} accounts "
            f"is below the minimum {TREND_MIN_INCREASE}"
        )

    if latest_evidence:
        author_counts: dict[str, int] = {}
        for e in latest_evidence:
            if e.author_handle:
                author_counts[e.author_handle] = author_counts.get(e.author_handle, 0) + 1
        if author_counts:
            dominant_share = max(author_counts.values()) / len(latest_evidence)
            if dominant_share > TREND_DOMINANT_CASCADE_SHARE:
                return False, (
                    f"a single account accounts for {dominant_share:.0%} of latest-day volume "
                    "— likely a repost cascade, not independent demand"
                )

    return True, "eligible"


def compute_lifecycle(
    current: Lifecycle, eligible_now: bool, eligible_evaluation_count: int, consecutive_ineligible_count: int
) -> Lifecycle:
    """Pilot interpretation of PRD §12's lifecycle rule, documented plainly
    since the PRD itself leaves the exact counting scheme unspecified:

    - Each call represents one DISTINCT evaluation where new evidence
      actually merged into this cluster — callers must only invoke this
      from that merge path, never on every scan regardless of whether
      anything changed, so "rerunning identical data cannot qualify it"
      holds structurally rather than by convention alone.
    - EARLY: currently trend-eligible, fewer than 2 total eligible
      evaluations so far.
    - EMERGING: >=2 eligible evaluations (PRD: "two eligible evaluations
      using a genuinely advanced evidence window").
    - ESTABLISHED: >=3 eligible evaluations (PRD: "persistence across three
      complete daily windows" — this pilot treats one evaluation as one
      window rather than tracking calendar-day granularity separately).
    - COOLING: was EMERGING/ESTABLISHED and has now had >=2 consecutive
      evaluations that failed the trend bar (PRD: "two healthy complete
      windows below the trend threshold").
    - UNKNOWN is deliberately never returned here — it's reserved for real
      collection gaps, which by definition never reach this function (no
      evaluation happened), so the prior lifecycle is left untouched by the
      caller instead."""
    if eligible_now:
        if eligible_evaluation_count >= 3:
            return Lifecycle.ESTABLISHED
        if eligible_evaluation_count >= 2:
            return Lifecycle.EMERGING
        return Lifecycle.EARLY
    if current in (Lifecycle.EMERGING, Lifecycle.ESTABLISHED) and consecutive_ineligible_count >= 2:
        return Lifecycle.COOLING
    return current


def is_development_eligible(
    evidence: Evidence,
    has_verifiable_source: bool,
    event_date: Optional[datetime],
    preparation_action: Optional[str],
) -> tuple[bool, str]:
    """PRD §12: 'require a source, a verifiable event date or date range,
    current status and a business-relevant preparation action. One original
    authoritative announcement may qualify without high mention volume' —
    unlike concern/trend, this never needs corroborating volume."""
    if not evidence.url:
        return False, "no source URL on the originating evidence"
    if not has_verifiable_source:
        return False, "extraction could not verify this as an original/sourced announcement"
    if event_date is None:
        return False, "no verifiable event date — stays 'date to confirm', not surfaced as a tracked development"
    if not preparation_action:
        return False, "no concrete business-relevant preparation action stated"
    return True, "eligible"

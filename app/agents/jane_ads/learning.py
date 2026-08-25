"""
Jane + Ads — the corpus feedback loop (ASC-SPEC-01 v2 §15, ASC-ENG-01 §1.2, §2, §6).

Closes the inversion the corpus exists for: imported strategies are a cold-start
prior, and as campaigns run at real Nigerian SME budgets, locally measured outcomes
progressively outrank the sources that suggested them.

    campaign ends
        -> record_campaign_outcome() credits the records that shaped it
        -> a human promotes once thresholds are met
        -> confirmed_locally REPLACES the evidence grade with A

Promotion is deliberately not automatic. confirmed_locally grants grade A, so a
record promoted on three campaigns is noise wearing the authority of local evidence
— worse than an honest imported claim, because the score believes it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .entities import LocalTestStatus, Strategy
from .store import StrategyStore

# ── Thresholds (ENG §2). Configuration, not constants — expect to tune. ──────
MIN_DEPLOYMENTS_FOR_PROMOTION = 12   # a floor against three lucky campaigns
MIN_OUTCOMES_RECORDED = 8            # deployments without outcomes do not count
MIN_OUTCOME_RATE_FOR_PROMOTION = 0.60
DEMOTION_RATE = 0.25
CONFIRMED_REVIEW_WINDOW_DEPLOYMENTS = 20
CONFIRMED_DEGRADE_THRESHOLD = 0.40


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class PromotionVerdict:
    """What the evidence supports. Never applied automatically — a human confirms."""
    strategy_id: str
    version: int
    eligible_for: Optional[LocalTestStatus]
    reason: str

    @property
    def actionable(self) -> bool:
        return self.eligible_for is not None


async def record_campaign_outcome(
    store: StrategyStore, db, event: dict, now: Optional[datetime] = None
) -> list[str]:
    """Credit every record that shaped this campaign (ENG §6.1).

    `unmarked` matters: a campaign with 58 conversations and 16 unmarked has
    incomplete evidence, so it counts as a deployment but its outcomes only count
    to the extent they were actually marked. Otherwise a half-tracked campaign
    would look like a clean result.

    Only CONFIRMED outcomes count — Jane's inferences are excluded upstream by the
    caller (§14.3), because an inferred win is a labour saver, not evidence.
    """
    now = now or _now()
    outcomes = event.get("outcomes") or {}
    recorded = int(outcomes.get("qualified", 0)) + int(outcomes.get("won", 0)) + int(outcomes.get("lost", 0))
    positive = int(outcomes.get("qualified", 0)) + int(outcomes.get("won", 0))

    touched: list[str] = []
    for cite in event.get("strategy_record_ids") or []:
        sid, ver = cite.get("id"), int(cite.get("version", 1))
        if not sid:
            continue
        rec = await store.get(sid, ver)
        if rec is None:
            continue
        rec.local.deployments += 1
        rec.local.outcomes_recorded += recorded
        rec.local.positive_outcomes += positive
        rec.local.last_reviewed = now
        await _persist_local(db, rec)
        touched.append(f"{sid} v{ver}")
    return touched


async def _persist_local(db, rec: Strategy) -> None:
    """Write only the local{} block — the record itself is immutable once approved
    (§3.3), and accumulating evidence is not an edit to the tactic."""
    await db.jane_ads_strategies.update_one(
        {"strategy_id": rec.strategy_id, "version": rec.version},
        {"$set": {"local": rec.local.model_dump(mode="json")}},
    )


def assess(rec: Strategy) -> PromotionVerdict:
    """What the accumulated evidence supports for this record. Read-only."""
    L = rec.local
    rate = L.outcome_rate

    if L.test_status is LocalTestStatus.CONFIRMED_LOCALLY:
        if L.deployments >= CONFIRMED_REVIEW_WINDOW_DEPLOYMENTS and rate is not None \
                and rate < CONFIRMED_DEGRADE_THRESHOLD:
            return PromotionVerdict(rec.strategy_id, rec.version, LocalTestStatus.RETIRED,
                                    f"confirmed record degraded to {rate:.0%} over "
                                    f"{L.deployments} deployments")
        return PromotionVerdict(rec.strategy_id, rec.version, None, "already confirmed")

    if L.deployments < MIN_DEPLOYMENTS_FOR_PROMOTION:
        return PromotionVerdict(rec.strategy_id, rec.version, None,
                                f"{L.deployments}/{MIN_DEPLOYMENTS_FOR_PROMOTION} deployments")
    if L.outcomes_recorded < MIN_OUTCOMES_RECORDED:
        # A record can have 20 deployments and still be unpromotable (ENG §2).
        return PromotionVerdict(rec.strategy_id, rec.version, None,
                                f"only {L.outcomes_recorded}/{MIN_OUTCOMES_RECORDED} "
                                "deployments have a recorded outcome")
    if rate is None:
        return PromotionVerdict(rec.strategy_id, rec.version, None, "no outcome rate")

    if rate >= MIN_OUTCOME_RATE_FOR_PROMOTION:
        return PromotionVerdict(rec.strategy_id, rec.version, LocalTestStatus.CONFIRMED_LOCALLY,
                                f"{rate:.0%} positive over {L.outcomes_recorded} outcomes")
    if rate <= DEMOTION_RATE:
        return PromotionVerdict(rec.strategy_id, rec.version,
                                LocalTestStatus.UNDERPERFORMED_LOCALLY,
                                f"{rate:.0%} positive over {L.outcomes_recorded} outcomes")
    return PromotionVerdict(rec.strategy_id, rec.version, None,
                            f"{rate:.0%} — between demotion and promotion thresholds")


async def review_queue(store: StrategyStore) -> list[PromotionVerdict]:
    """Records whose evidence now supports a status change. The internal tool's
    aggregate view — what is ready for a human to confirm."""
    return [v for v in (assess(r) for r in await store.fetch_approved()) if v.actionable]


async def apply_verdict(store: StrategyStore, db, verdict: PromotionVerdict,
                        confirmed_by: str, now: Optional[datetime] = None) -> Strategy:
    """Apply a status change a human confirmed. `confirmed_by` is required — there is
    no path that promotes without a name attached (§15.1)."""
    if not verdict.actionable:
        raise ValueError(f"{verdict.strategy_id}: nothing to apply — {verdict.reason}")
    if not confirmed_by:
        raise ValueError("confirmed_by is required: promotion is never automatic")
    rec = await store.get(verdict.strategy_id, verdict.version)
    if rec is None:
        raise KeyError(f"{verdict.strategy_id} v{verdict.version} not found")
    rec.local.test_status = verdict.eligible_for
    rec.local.last_reviewed = now or _now()
    rec.local.result_notes = f"{verdict.reason} (confirmed by {confirmed_by})"
    await _persist_local(db, rec)
    return rec

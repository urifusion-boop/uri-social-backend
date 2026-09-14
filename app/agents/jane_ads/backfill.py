"""
Jane + Ads — v2 backfill rules (ASC-SPEC-01 v2 §4.3, §4.4).

None of the seven v2 fields exist in the seed workbook, so import has to supply
them. The spec is explicit about which may be machine-applied and which may not:

    consumed_by[]          -> derive from Category (§4.4). Machine, human spot-check.
    conversion_location[]  -> "Human pass required. Cannot be safely defaulted."
    requires[]             -> "Human pass. Partially derivable from Modification
                              Required text but not reliably."
    requires_sustained_days-> human pass on three categories
    guardrails[]           -> derive candidates, human confirms

This module therefore derives ONLY `consumed_by`. Everything else is read from the
workbook when the column exists and otherwise left at its fail-closed default. It
deliberately does not text-heuristic `requires` or `conversion_location`.

That restraint is not theoretical. A heuristic backfill produced four classes of
error in review:
  · SEED-068 / SEED-060 tagged `website_or_pixel` — records that exist *because*
    the user has no website. As tagged, the filter excludes them from exactly the
    accounts they were written for. An inverted precondition is worse than none.
  · `parallel_adset_budget` attached to nearly every record, including ₦1,000
    free-toggle ones, which would exclude most of the corpus for every
    micro-budget user — i.e. the entire book.
  · `conversion_location` keyed on incidental words: Meta account-settings records
    tagged `calls` / `app` / `website` when the right value is `any`.
  · `consumed_by = vce` on records with no creative implication.

The first two fail closed in the wrong direction, which is why this is the one
place a "reasonable guess" is more dangerous than a blank.
"""
from __future__ import annotations

from .entities import ConsumedBy, StrategyCategory

# Spec §4.4. Topical category -> flow stages that consume it.
CATEGORY_TO_CONSUMED_BY: dict[StrategyCategory, list[ConsumedBy]] = {
    StrategyCategory.OFFER_POSITIONING: [ConsumedBy.PLAN_GENERATION, ConsumedBy.CREATIVE_BRIEF],
    StrategyCategory.AUDIENCE_CONSTRUCTION: [ConsumedBy.PLAN_GENERATION],
    StrategyCategory.CREATIVE_FORMATS: [ConsumedBy.CREATIVE_BRIEF, ConsumedBy.VCE],
    StrategyCategory.COPY_ANGLES: [ConsumedBy.CREATIVE_BRIEF],
    StrategyCategory.BUDGET_PACING: [ConsumedBy.CAMPAIGN_STRUCTURE],
    StrategyCategory.MICRO_BUDGET_TESTING: [ConsumedBy.CAMPAIGN_STRUCTURE],
    StrategyCategory.CONVERSION_MECHANICS: [ConsumedBy.PLAN_GENERATION, ConsumedBy.CAMPAIGN_STRUCTURE],
    StrategyCategory.RETARGETING: [ConsumedBy.PLAN_GENERATION, ConsumedBy.CAMPAIGN_STRUCTURE],
    StrategyCategory.PLATFORM_MECHANICS: [ConsumedBy.CAMPAIGN_STRUCTURE, ConsumedBy.VCE],
    StrategyCategory.DIAGNOSTICS: [ConsumedBy.DIAGNOSTICS],
}

# Spec §4.3 — human pass required on these three categories, where multi-day
# dependence actually lives. Flagged for review, never auto-populated.
SUSTAINED_DAYS_REVIEW_CATEGORIES = {
    StrategyCategory.RETARGETING,
    StrategyCategory.MICRO_BUDGET_TESTING,
    StrategyCategory.BUDGET_PACING,
}


def derive_consumed_by(category: StrategyCategory) -> list[ConsumedBy]:
    """The one v2 field the spec sanctions machine-deriving."""
    return list(CATEGORY_TO_CONSUMED_BY[category])


def needs_sustained_days_review(category: StrategyCategory) -> bool:
    return category in SUSTAINED_DAYS_REVIEW_CATEGORIES

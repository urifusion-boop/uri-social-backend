"""
Jane + Ads — platform constants (verified 2026 floors).

Source: Master PRD v4.0, Part A2. FX baseline 1 USD ≈ ₦1,550.

These are the ONLY place platform economics live — the decision engine reads them,
never hard-codes them. Confirm against live platform docs at integration time
(Ibukun's scope); tuning these numbers must never require touching engine logic.
"""
from __future__ import annotations

# ── FX ──────────────────────────────────────────────────────────────────────
USD_TO_NGN: float = 1550.0

# ── Useful minimums (TOTAL campaign budget, Naira) ───────────────────────────
# The smallest total budget at which a platform can run a meaningful short
# campaign (4–7 days). Below this a campaign technically runs but can't learn.
# Derived from PRD A2 + the worked examples in Part C1 (₦5,000 food vendor → Meta).
USEFUL_MIN_NGN: dict[str, float] = {
    "meta":   5_000.0,
    "google": 5_000.0,
    "tiktok": 50_000.0,   # PRD: only route ₦50,000+ wallets to TikTok
}

# ── Hard platform floors (daily, Naira) — informational guardrails ───────────
# The decision engine gates on USEFUL_MIN, not these; kept for reference/validation.
HARD_FLOOR_DAILY_NGN: dict[str, float] = {
    "meta":   1_610.0,    # FB; IG ~2,176–2,500
    "google": 0.0,        # CPC-driven, no hard floor
    "tiktok": 31_000.0,   # ad-group level
}

# TikTok is video-only; no video → no TikTok regardless of budget (PRD C1).
TIKTOK_REQUIRES_VIDEO: bool = True

# ── Meta minimum daily budget ─────────────────────────────────────────────────
# Meta rejects an ad set whose daily budget is at/below its per-currency minimum
# (observed live 2026-07 as "must be more than NGN1,400.00", API error subcode
# 1885272 "Budget is too low"). We clear it by CAPPING campaign DURATION so
# total/days stays above this — never by inflating the daily budget past what the
# user authorised. Set a touch above the observed floor for headroom, matching the
# FB hard floor already noted in HARD_FLOOR_DAILY_NGN.
META_MIN_DAILY_NGN: float = 1_610.0

# ── Campaign duration (PRD C: 4–7 days) ──────────────────────────────────────
MIN_CAMPAIGN_DAYS: int = 4
MAX_CAMPAIGN_DAYS: int = 7
DEFAULT_CAMPAIGN_DAYS: int = 5

# ── Wallet / billing (PRD A4, B3) ────────────────────────────────────────────
MIN_TOPUP_NGN: float = 5_000.0
# URI's service fee, as a share of the budget the CLIENT states. The stated budget is
# the whole of what leaves their wallet: the fee comes out of it first and the
# remainder is what Meta actually spends (see ad_spend_from_budget below). ₦20,000
# stated = ₦2,000 fee + ₦18,000 of ads, and the duration is worked out from the
# ₦18,000 — not from the ₦20,000, which would plan a campaign the wallet can't fund.
#
# This replaced a fee charged ON TOP of the stated budget ("₦20,000 ad spend +
# ₦2,000 service fee = ₦22,000 from your wallet"), which asked the client to fund
# more than the number they had just given.
SERVICE_FEE_RATE: float = 0.10

# Production billing meter: recoup real Meta ad spend × this markup from the
# customer's prepaid wallet (see billing.py). >1 guarantees URI is made whole on
# every campaign plus margin — no basis risk from underperforming campaigns, unlike
# the per-conversation meter below.
#
# DERIVED from SERVICE_FEE_RATE, never set by hand: the wallet has to land at exactly
# zero when Meta finishes spending. Ad spend is budget × (1 - rate), so recouping it
# in full needs ÷ (1 - rate) — at a 10% rate, 18,000 × 1.1111 = the ₦20,000 stated.
# Hard-coding 1.10 here instead would have collected 19,800 and quietly left ₦200 of
# every ₦20,000 campaign uncollected.
AD_SPEND_MARKUP: float = round(1.0 / (1.0 - SERVICE_FEE_RATE), 6)


# The markup campaigns launched BEFORE the fee moved inside the stated budget were
# planned and wallet-gated under. billing.py bills each campaign at the markup stamped
# on its own record and falls back to this for records that predate the stamp, so a
# live campaign is never re-based mid-flight onto maths it wasn't sold under.
LEGACY_AD_SPEND_MARKUP: float = 1.10


def ad_spend_from_budget(budget_ngn: float) -> float:
    """What Meta actually gets to spend out of a client's stated budget — the budget
    less URI's fee. The ONE place this split is computed, so the plan, the duration,
    the wallet gate and the ad set can't disagree about it."""
    return round(budget_ngn * (1.0 - SERVICE_FEE_RATE), 2)


def service_fee_from_budget(budget_ngn: float) -> float:
    """URI's fee out of a client's stated budget. Complement of ad_spend_from_budget,
    subtracted rather than multiplied so the two always sum to the budget exactly."""
    return round(budget_ngn - ad_spend_from_budget(budget_ngn), 2)
CONVERSATION_PRICE_FLOOR_NGN: float = 400.0   # MAX(₦400, trailing-7d cost × 1.5)
CONVERSATION_PRICE_MULTIPLIER: float = 1.5
TRAILING_COST_WINDOW_DAYS: int = 7
SERVICE_FEE_PER_CONVERSATION_NGN: float = 100.0
VAT_RATE: float = 0.075

# ── A/B test tiers (PRD C2) — total budget on a single platform ──────────────
# Below LIGHT → 1 variant (splitting starves both).
# LIGHT → 2 variants, same creative, different audiences (cheapest to learn).
# FULL  → test audiences AND creative.
AB_LIGHT_TEST_NGN: float = 10_000.0
AB_FULL_TEST_NGN: float = 20_000.0

# ── Plan-variant selection tiers (Multi-Plan Audience Variants spec §6.1) ────
# How many DIFFERENT AUDIENCE STRATEGIES a client can run at once — a distinct
# question from the A/B tiers above (those govern creative/audience testing
# WITHIN one platform's ad set; these govern how many separate ad sets, each a
# different audience with its own creative brief, the total budget can support
# without starving any of them below USEFUL_MIN_NGN).
PLAN_VARIANT_TIER_2_NGN: float = 15_000.0    # below this: one plan only
PLAN_VARIANT_TIER_3_NGN: float = 50_000.0    # at/above: two or three selectable
PLAN_VARIANT_TIER_4_NGN: float = 250_000.0   # at/above: multiple, with proper structure

# ── Sustained capacity (ASC-SPEC-01 v2 §5.2, ASC-ENG-01 §2) ──────────────────
# Ninety days rather than thirty: top-ups arrive staggered and small, and thirty
# days is too few events for the rate to be stable. Below the minimum event count
# the number is not trusted and multi-day tactics are excluded — fail closed.
SUSTAINED_WINDOW_DAYS: int = 90
SUSTAINED_MIN_TOPUP_EVENTS: int = 2

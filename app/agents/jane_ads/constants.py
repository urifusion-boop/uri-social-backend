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

"""
TEMP (2026-09-17, per explicit user request — revert when real TikTok testing is
done): coverage for the TikTok-only fee bypass in router.py's `_total_due_ngn`.

Scope reminder: the bypass is gated exclusively on
MetaLaunchFromMessageBody.preferred_platform == "tiktok" at the one place it's
computed (_build_campaign_plan) — never on what Jane herself would have picked —
so a Meta plan can never end up with fee_bypassed=True. These tests only exercise
the pure helper; the gating itself is covered by the existing preferred_platform
tests elsewhere.
"""
from app.agents.jane_ads import constants as C
from app.agents.jane_ads.router import _total_due_ngn, _wallet_shortfall_message


def test_normal_path_unchanged():
    # Byte-for-byte the pre-existing behaviour when fee_bypassed isn't passed at all.
    assert _total_due_ngn(45_000) == round(45_000 * C.AD_SPEND_MARKUP, 2)
    assert _total_due_ngn(45_000, fee_bypassed=False) == _total_due_ngn(45_000)


def test_bypassed_path_charges_exactly_the_ad_spend():
    # No markup at all — the wallet only ever needs to cover what's actually
    # submitted to TikTok as the ad group's lifetime budget.
    assert _total_due_ngn(45_000, fee_bypassed=True) == 45_000
    assert _total_due_ngn(31_000, fee_bypassed=True) == 31_000


def test_bypassed_total_due_clears_tiktoks_real_floor_at_a_lower_number():
    # The whole point: bypassing the fee means a stated budget just above TikTok's
    # real ₦31,000/day floor is enough, not ~₦34,444 (31,000 / 0.9).
    bypassed_minimum = C.HARD_FLOOR_DAILY_NGN["tiktok"]
    normal_minimum = round(bypassed_minimum / (1.0 - C.SERVICE_FEE_RATE), 2)
    assert _total_due_ngn(bypassed_minimum, fee_bypassed=True) < normal_minimum


def test_shortfall_message_reflects_the_bypassed_total():
    normal_msg = _wallet_shortfall_message(0.0, 45_000)
    bypassed_msg = _wallet_shortfall_message(0.0, 45_000, fee_bypassed=True)
    assert "49,999" in normal_msg or "50,000" in normal_msg
    assert "45,000" in bypassed_msg
    assert normal_msg != bypassed_msg

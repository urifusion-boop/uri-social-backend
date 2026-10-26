"""
TikTok pays URI's normal fee, like every other platform (the temporary 2026-09-17 fee
bypass is gone). The fee comes out of the client's stated budget, so TikTok's own
₦31,000-a-day floor needs a stated budget of ₦34,445 — and a client who states less is
told so BEFORE launch instead of being rejected by TikTok after the wallet check.
"""
from app.agents.jane_ads import constants as C
from app.agents.jane_ads.router import (
    _tiktok_budget_too_low_message,
    _total_due_ngn,
    _wallet_shortfall_message,
)

TIKTOK_FLOOR = C.HARD_FLOOR_DAILY_NGN["tiktok"]


def test_the_wallet_must_cover_the_whole_stated_budget():
    # Ad spend is what's left after the fee; the wallet pays it back up to the stated figure.
    stated = 40_000
    ad_spend = C.ad_spend_from_budget(stated)
    assert abs(_total_due_ngn(ad_spend) - stated) < 0.05


def test_a_stated_budget_equal_to_the_floor_is_not_enough():
    # ₦31,000 stated is only ₦27,900 of ad spend — under TikTok's ₦31,000 floor.
    assert C.ad_spend_from_budget(31_000) < TIKTOK_FLOOR
    message = _tiktok_budget_too_low_message(C.ad_spend_from_budget(31_000))
    assert "34,445" in message
    assert "31,000" in message
    assert "10%" in message


def test_the_minimum_stated_budget_clears_the_floor_by_a_naira():
    assert C.ad_spend_from_budget(34_445) >= TIKTOK_FLOOR
    assert C.ad_spend_from_budget(34_444) < TIKTOK_FLOOR


def test_a_budget_that_clears_the_floor_gets_no_message():
    assert _tiktok_budget_too_low_message(C.ad_spend_from_budget(34_445)) == ""
    assert _tiktok_budget_too_low_message(C.ad_spend_from_budget(100_000)) == ""


def test_shortfall_message_names_the_full_amount_leaving_the_wallet():
    message = _wallet_shortfall_message(0.0, C.ad_spend_from_budget(34_445))
    assert "34,445" in message or "34,444" in message


def test_no_fee_bypass_path_remains():
    import inspect

    from app.agents.jane_ads import router

    assert "fee_bypass" not in inspect.getsource(router)

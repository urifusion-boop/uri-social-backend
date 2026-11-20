"""Credits for an ad's images by style (ad_image_billing.py), shadow by default."""
import asyncio

import pytest

from app.agents.jane_ads import ad_image_billing as b
from app.core.config import settings
from app.utils.image_usage import bump, counting


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.mark.parametrize("fid,credits", [
    ("SEED-097", 1),   # Text-Only: drawn, no AI image
    ("SEED-099", 1),   # Event Promo: one scene
    ("SEED-080", 2),   # Problem / Solution: two scenes
    ("SEED-088", 3),   # Starter Pack: up to six item photos
    (None, 1),         # no style: one generated image
])
def test_each_style_is_priced_by_the_images_it_needs(fid, credits):
    assert b.price(fid) == credits


def test_shadow_is_the_default_and_still_charges_the_flat_credit(monkeypatch):
    monkeypatch.delattr(settings, "AD_IMAGE_BILLING_MODE", raising=False)
    assert b.mode() == "shadow"
    assert b.credits_to_charge("SEED-088") == 1


def test_on_charges_the_styles_price(monkeypatch):
    monkeypatch.setattr(settings, "AD_IMAGE_BILLING_MODE", "on", raising=False)
    assert b.credits_to_charge("SEED-088") == 3 and b.credits_to_charge("SEED-097") == 1


def test_image_calls_are_counted_only_inside_a_counter_and_across_tasks():
    bump()  # outside: counts nowhere, never fails

    async def two_calls():
        await asyncio.gather(asyncio.sleep(0), asyncio.sleep(0))
        bump()
        bump()

    with counting() as calls:
        _run(asyncio.gather(two_calls(), two_calls()))
    assert calls.n == 4


class _Col:
    def __init__(self):
        self.rows = []

    async def insert_one(self, row):
        self.rows.append(row)


def test_every_ad_is_recorded_in_shadow_with_its_price_and_real_image_calls(monkeypatch):
    monkeypatch.setattr(settings, "AD_IMAGE_BILLING_MODE", "shadow", raising=False)
    col = _Col()
    _run(b.record({b.COLLECTION: col}, user_id="u", brand_id="br", campaign_id="c",
                  format_id="SEED-088", image_calls=7, charged=1))
    row = col.rows[0]
    assert (row["priced_credits"], row["charged_credits"], row["image_calls"], row["mode"]) == (3, 1, 7, "shadow")


def test_off_records_nothing(monkeypatch):
    monkeypatch.setattr(settings, "AD_IMAGE_BILLING_MODE", "off", raising=False)
    col = _Col()
    _run(b.record({b.COLLECTION: col}, user_id="u", brand_id=None, campaign_id="c",
                  format_id="SEED-099", image_calls=1, charged=1))
    assert col.rows == []


def test_shadow_needs_only_the_flat_credit_and_excludes_nothing(monkeypatch):
    monkeypatch.setattr(settings, "AD_IMAGE_BILLING_MODE", "shadow", raising=False)
    assert b.credits_needed("SEED-088") == 1
    assert b.styles_costing_more_than(0) == []


def test_on_a_picked_style_needs_its_price(monkeypatch):
    monkeypatch.setattr(settings, "AD_IMAGE_BILLING_MODE", "on", raising=False)
    assert b.credits_needed("SEED-088") == 3
    assert b.credits_needed("SEED-099") == 1
    assert b.credits_needed(None) == 1          # Jane chooses: see styles_costing_more_than


def test_on_jane_never_picks_a_style_the_balance_cant_pay_for(monkeypatch):
    monkeypatch.setattr(settings, "AD_IMAGE_BILLING_MODE", "on", raising=False)
    left_out = b.styles_costing_more_than(1)
    assert "SEED-088" in left_out and "SEED-080" in left_out and "SEED-099" not in left_out
    assert b.styles_costing_more_than(3) == []

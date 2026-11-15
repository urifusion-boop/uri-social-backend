"""PRD WS5 "Initial Budget Configuration Hypothesis": the daily budget decides how many ads a
Meta campaign tests and how many ad sets they run in — and the launch builds exactly that."""
import pytest

from app.agents.jane_ads import brief
from app.agents.jane_ads import concepts as K
from app.agents.jane_ads import constants as C


@pytest.mark.parametrize("daily, ads, ad_sets", [
    (3_000, 1, 1),        # under the table: one ad
    (5_000, 2, 1),        # ₦5k–15k: 1 ad set, 2 creatives
    (14_999, 2, 1),
    (15_000, 3, 1),       # ₦15k–30k: 1–2 ad sets, 3 creatives
    (25_000, 3, 2),
    (30_000, 4, 2),       # ₦30k–75k: 2 ad sets, 3–4 creatives
    (80_000, 4, 2),       # ₦75k–150k: 2–3 ad sets, 4–6 creatives
    (120_000, 6, 3),
    (500_000, 6, 3),      # ₦150k+: strategy-dependent, capped at what one campaign can test
])
def test_budget_table(daily, ads, ad_sets):
    st = K.budget_structure(daily)
    assert (st["ads"], st["ad_sets"]) == (ads, ad_sets)
    assert daily / st["ad_sets"] >= C.MIN_DAILY_SPEND_NGN or st["ad_sets"] == 1


@pytest.mark.parametrize("daily, ads, expected", [
    (5_000, 2, (K.SINGLE_AD_SET, 1)),     # table says one ad set
    (25_000, 2, (K.ABO, 2)),              # 2 ad sets, 2 ads: one each
    (30_000, 4, (K.GROUPED, 2)),          # 2 ad sets sharing 4 ads
    (120_000, 6, (K.GROUPED, 3)),
    (120_000, 2, (K.ABO, 2)),             # never more ad sets than ads
    (30_000, 1, (K.SINGLE_AD_SET, 1)),
])
def test_layout_for_budget(daily, ads, expected):
    assert K.layout_for_budget(daily * 7, 7, ads) == expected


def test_grouped_ads_are_spread_evenly():
    slots = [K.ad_set_for(i, 4, K.GROUPED, 2) for i in range(4)]
    assert slots == [0, 1, 0, 1]
    assert K.ad_set_total(4, K.GROUPED, 2) == 2
    assert K.ad_set_total(3, K.ABO, 0) == 3
    assert K.ad_set_total(3, K.SINGLE_AD_SET, 0) == 1


def test_plan_summary_describes_the_grouped_layout():
    plan = {"platforms": [{"budget_ngn": 30_000 * 7, "days": 7}], "concepts": [{}, {}, {}],
            "ad_layout": K.GROUPED, "ad_set_count": 2}
    out = brief.structure(plan, {"budget_ngn": 30_000 * 7})
    assert out["ad_sets"] == 2 and out["per_ad_set_daily_ngn"] == 15_000
    assert "4 different ads across 2 ad sets" in out["why"]
    assert out["prd_budget_table"]["ads"] == 4


def test_owner_choice_is_kept_and_budget_table_applies_otherwise():
    from app.agents.jane_ads.models import CampaignPlan
    from app.agents.jane_ads.router import _apply_budget_layout

    def plan(ads, daily, **kw):
        p = CampaignPlan.model_construct(
            platforms=[type("P", (), {"budget_ngn": daily * 7, "days": 7})()],
            concepts=[object()] * (ads - 1), ad_layout=K.SINGLE_AD_SET, ad_set_count=0,
            ad_layout_chosen=False)
        for k, v in kw.items():
            setattr(p, k, v)
        return p

    p = plan(4, 30_000)
    _apply_budget_layout(p)
    assert (p.ad_layout, p.ad_set_count) == (K.GROUPED, 2)

    chosen = plan(4, 30_000, ad_layout_chosen=True)
    _apply_budget_layout(chosen)
    assert chosen.ad_layout == K.SINGLE_AD_SET

    too_thin = plan(4, 6_000, ad_layout=K.ABO, ad_layout_chosen=True)
    _apply_budget_layout(too_thin)
    assert too_thin.ad_layout == K.SINGLE_AD_SET

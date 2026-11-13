"""Suggested budgets (PRD WS5): every option plans and launches under the planner's own rules."""
from app.agents.jane_ads import constants as C
from app.agents.jane_ads.budget_suggestions import suggest


def test_meta_offers_one_two_and_three_ads_with_two_recommended():
    out = suggest("meta", 7)
    assert [o["ads"] for o in out["options"]] == [1, 2, 3]
    assert [o["recommended"] for o in out["options"]] == [False, True, False]
    assert out["options"][1]["what"] == "Tests 2 ads · 7 days"
    for o in out["options"]:
        ad_spend = C.ad_spend_from_budget(o["amount_ngn"])
        assert ad_spend / 7 / o["ads"] >= C.MIN_DAILY_SPEND_NGN        # every ad clears the daily floor
        assert o["amount_ngn"] >= C.USEFUL_MIN_NGN["meta"] and o["amount_ngn"] % 1000 == 0


def test_tiktok_options_clear_the_useful_minimum_and_daily_floor():
    out = suggest("tiktok", 7)
    for o in out["options"]:
        assert o["amount_ngn"] >= C.USEFUL_MIN_NGN["tiktok"]
        assert o["amount_ngn"] / o["days"] >= C.HARD_FLOOR_DAILY_NGN["tiktok"]
    assert [o["days"] for o in out["options"]] == [2, 3, 7]
    assert sum(o["recommended"] for o in out["options"]) == 1


def test_long_durations_are_capped_and_explained():
    out = suggest("meta", 30)
    assert out["days"] == C.MAX_CAMPAIGN_DAYS
    assert "Keep it running" in out["note"]
    assert suggest("meta", 7)["note"] == ""


def test_google_options_are_round_and_above_the_minimum():
    out = suggest("google", 7)
    assert all(o["amount_ngn"] >= C.USEFUL_MIN_NGN["google"] for o in out["options"])
    assert out["options"][1]["recommended"]

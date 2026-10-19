"""
Found testing the 30-day calendar on prod (Oct 2026):
  1. A brand key date inside the plan's period (a launch anniversary on
     5 November) got no post — the prompt asked, the model didn't.
  2. Banning "revolutionary" let "revolutionized" through — a related form
     the banned-word matcher didn't treat as the same word.
"""
from app.agents.content_calendar_v2.services import content_calendar_v2_service as svc
from app.agents.social_media_manager.services.brand_brief import find_banned

DATES = [f"2026-10-{d:02d}" for d in range(19, 32)] + [f"2026-11-{d:02d}" for d in range(1, 18)]


def _plan():
    return [{"date": d, "day_index": i, "subject": f"topic {i}", "angle": "tips", "concept_name": f"Idea {i}"}
            for i, d in enumerate(DATES)]


# ── 1. key dates ────────────────────────────────────────────────────────────

def test_key_date_in_period_gets_its_post_from_the_string_form():
    dated = _plan()
    brand = {"key_dates": "Uri Social launch anniversary (2026-11-05), Old event (2025-01-01)"}
    placed = svc._enforce_key_dates(dated, brand, DATES)
    assert placed == [{"label": "Uri Social launch anniversary", "date": "2026-11-05"}]
    item = next(i for i in dated if i["date"] == "2026-11-05")
    assert item["subject"] == "Uri Social launch anniversary"
    assert item["key_date"]["date"] == "2026-11-05" and item["holiday_tie_in"]["source"] == "brand_key_date"
    assert sum(1 for i in dated if i.get("key_date")) == 1          # nothing else touched


def test_yearly_dates_and_list_form():
    brand = {"key_dates": [{"label": "Store birthday", "date": "11-10"},
                           {"label": "Founder's day", "date": "1 November"},
                           {"label": "Black Friday sale", "date": "Nov 27"}]}   # outside the period
    placed = svc._enforce_key_dates(_plan(), brand, DATES)
    assert {(p["label"], p["date"]) for p in placed} == {("Store birthday", "2026-11-10"),
                                                         ("Founder's day", "2026-11-01")}


def test_a_day_already_about_the_date_is_left_alone():
    dated = _plan()
    day = next(i for i in dated if i["date"] == "2026-11-05")
    day.update({"subject": "Celebrating our launch anniversary", "concept_name": "Anniversary"})
    assert svc._enforce_key_dates(dated, {"key_dates": "Uri Social launch anniversary (2026-11-05)"}, DATES) == []
    assert day["subject"] == "Celebrating our launch anniversary"


def test_no_key_dates_changes_nothing():
    dated = _plan()
    assert svc._enforce_key_dates(dated, {}, DATES) == [] and not any(i.get("key_date") for i in dated)


def test_plan_crossing_new_year_uses_the_right_year():
    dates = ["2026-12-30", "2026-12-31", "2027-01-01", "2027-01-02"]
    dated = [{"date": d, "day_index": i, "subject": "x"} for i, d in enumerate(dates)]
    placed = svc._enforce_key_dates(dated, {"key_dates": [{"label": "New Year sale", "date": "01-01"}]}, dates)
    assert placed == [{"label": "New Year sale", "date": "2027-01-01"}]


# ── 2. word families ────────────────────────────────────────────────────────

def test_banned_word_family_is_caught():
    assert find_banned("How digital trends have revolutionized marketing", ["revolutionary"]) == ["revolutionary"]
    assert find_banned("A revolution in content", ["revolutionary"]) == ["revolutionary"]
    assert find_banned("Synergies everywhere", ["synergy"]) == ["synergy"]
    assert find_banned("Guaranteed results", ["guarantee"]) == ["guarantee"]
    assert find_banned("Leveraging AI", ["leverage"]) == ["leverage"]
    assert find_banned("Cheapest prices, cheaply made", ["cheap"]) == ["cheap"]


def test_unrelated_words_are_not_caught():
    assert find_banned("Book now", ["ok"]) == []
    assert find_banned("Grab a smartphone", ["smart"]) == []
    assert find_banned("The counterrevolution", ["revolutionary"]) == []
    assert find_banned("elitism", ["elite"]) == []
    assert find_banned("Act fast", ["free trial"]) == []   # phrases stay exact

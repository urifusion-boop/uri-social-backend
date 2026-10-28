"""
Uri Market Intelligence — relevance_breakdown's served_geography component
(classification/scoring.py). Confirmed live (full audit of Social
Listening): geo_fit did a bare substring check of a mentioned CITY
("lagos") against the Brand Playbook's region chips joined into one
lowercased string ("nigeria") — "lagos" is never a substring of
"nigeria", so a real Nigeria-based account's own evidence could never
earn the top geo_fit point even when the city genuinely was in a country
the business serves. Both sides now resolve to a real country code
(listening/geo.py) before comparing.
"""
from datetime import datetime

from app.agents.market_intelligence.classification.scoring import relevance_breakdown
from app.agents.market_intelligence.models import Evidence, Geography


def _evidence(explicit_location=None, text="some text"):
    return Evidence(
        id="e1", brand_id="b1", user_id="u1", topic_id="t1",
        provider="fake", platform="fake", source_id="s1", text=text,
        collected_at=datetime.utcnow(),
        geography=Geography(explicit_location=explicit_location),
    )


def _ctx(region):
    return {"service_locations": [region] if region else None}


def test_the_literal_live_bug_lagos_now_scores_full_points_for_a_nigeria_business():
    score = relevance_breakdown([_evidence(explicit_location="Lagos")], _ctx("Nigeria"))
    geo = next(c for c in score.components if c.name == "served_geography")
    assert geo.points == 2


def test_a_confidently_wrong_country_scores_zero_not_a_soft_credit():
    score = relevance_breakdown([_evidence(explicit_location="New York")], _ctx("Nigeria"))
    geo = next(c for c in score.components if c.name == "served_geography")
    assert geo.points == 0


def test_an_unplaceable_mentioned_location_gets_the_old_soft_credit():
    score = relevance_breakdown([_evidence(explicit_location="some small town")], _ctx("Nigeria"))
    geo = next(c for c in score.components if c.name == "served_geography")
    assert geo.points == 1


def test_no_mentioned_location_scores_zero():
    score = relevance_breakdown([_evidence(explicit_location=None)], _ctx("Nigeria"))
    geo = next(c for c in score.components if c.name == "served_geography")
    assert geo.points == 0


def test_global_business_matches_any_real_location():
    score = relevance_breakdown([_evidence(explicit_location="Lagos")], _ctx("Global / International"))
    geo = next(c for c in score.components if c.name == "served_geography")
    assert geo.points == 2


def test_no_region_on_record_gets_the_soft_credit_not_full_points():
    score = relevance_breakdown([_evidence(explicit_location="Lagos")], _ctx(None))
    geo = next(c for c in score.components if c.name == "served_geography")
    assert geo.points == 1

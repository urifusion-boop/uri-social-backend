"""Answering "which city?" with just "Lagos" covers Lagos — and keeps covering it."""
from app.agents.jane_ads.jane_consultant import (
    ConsultantBrief, _enforce_hard_requirements, _whole_area_answered,
)
from app.agents.jane_ads.models import GeoPin, GeoPlan, GeoMode, PinSource
from app.agents.jane_ads.plan_fields import _location_names


def test_a_bare_city_answer_survives_the_next_turn():
    assert _whole_area_answered("promote uri social. 5500. Lagos. 5000") == "Lagos"


def test_a_later_neighbourhood_narrows_it_again():
    assert _whole_area_answered("promote uri social. Lagos. Ikeja and Lekki") == ""


def test_jane_does_not_ask_for_pockets_after_lagos():
    brief = ConsultantBrief(business_name="Uri", category="software", budget_ngn=5500,
                            geo_mode="watering_hole",
                            clarify="Which areas or pockets in Lagos have worked before?")
    out = _enforce_hard_requirements(brief, "promote uri social, budget 5500. Lagos",
                                     [{"role": "user", "content": "budget 5500"}], None)
    assert out.clarify == ""
    assert out.whole_area and out.city == "Lagos" and out.geo_areas == []


def test_a_whole_city_plan_shows_the_city_not_a_dash():
    plan = GeoPlan(mode=GeoMode.WATERING_HOLE, city="Lagos", pins=[], fallback_area="Lagos")
    assert _location_names(plan) == ["Lagos"]
    pinned = plan.model_copy(update={"pins": [GeoPin(name="Ikeja", lat=6.6, lng=3.35,
                                                     radius_km=3, source=PinSource.GEOCODED)]})
    assert _location_names(pinned) == ["Ikeja"]
    assert _location_names(None) == []

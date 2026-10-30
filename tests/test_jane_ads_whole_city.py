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


def test_two_cities_are_both_kept():
    from app.agents.jane_ads.geo import split_areas, whole_area_answer, whole_area_request
    assert _whole_area_answered("promote uri social. Lagos and Abuja. yes") == "Lagos, Abuja"
    assert whole_area_answer("Lagos & FCT") == "Lagos, Abuja"
    assert whole_area_request("All of Lagos, Abuja") == "Lagos, Abuja"
    assert whole_area_request("All of Lagos") == "Lagos"
    assert whole_area_answer("Ikeja and Lekki") == ""
    assert split_areas("Lagos, Abuja") == ["Lagos", "Abuja"]


def test_two_cities_target_both_on_meta(monkeypatch):
    import asyncio
    from app.agents.jane_ads import geo_names
    from app.agents.jane_ads.geo import meta_targeting_from_geo_named

    async def named(name, region, token=""):
        return {"Lagos": {"key": "L", "type": "region"}, "Abuja": {"key": "A", "type": "city"}}.get(name)

    monkeypatch.setattr(geo_names, "resolve_named_location", named)
    plan = GeoPlan(mode=GeoMode.WATERING_HOLE, city="Lagos, Abuja", pins=[], fallback_area="Lagos, Abuja")
    out = asyncio.run(meta_targeting_from_geo_named(plan))
    locs = out["geo_locations"]
    assert {"key": "L"} in locs[geo_names.field_for_type("region")]
    assert {"key": "A"} in locs[geo_names.field_for_type("city")]

"""
Unit tests for watering-hole / pin-and-pocket geo targeting (geo.py).

Uses static providers so it's deterministic — no LLM, no Google, no network.
The critical property: NEVER pin a place that can't be validated.
"""
import asyncio

from app.agents.jane_ads.geo import (
    StaticGeocoder,
    StaticPinProposer,
    build_geo_plan,
    decide_geo_mode,
)
from app.agents.jane_ads.models import GeoMode, PinSource


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── Mode: pull vs go-find ──────────────────────────────────────────────────────

def test_restaurant_is_own_radius():
    assert decide_geo_mode("restaurant") == GeoMode.OWN_RADIUS


def test_realtor_is_watering_hole():
    assert decide_geo_mode("luxury real estate") == GeoMode.WATERING_HOLE


def test_b2b_supplier_is_watering_hole():
    assert decide_geo_mode("industrial supplier") == GeoMode.WATERING_HOLE


# ── Pin validation ─────────────────────────────────────────────────────────────

def test_validated_pins_become_targets():
    # Surulere lunch spot → commercial streets (both real in the gazetteer).
    proposer = StaticPinProposer([
        ("Bode Thomas", "commercial street, offices"),
        ("Adeniran Ogunsanya", "foot traffic + offices"),
    ])
    plan = _run(build_geo_plan("Mama's Kitchen", "restaurant", "Surulere",
                               proposer, StaticGeocoder()))
    assert plan.mode == GeoMode.OWN_RADIUS
    assert [p.name for p in plan.pins] == ["Bode Thomas", "Adeniran Ogunsanya"]
    assert all(p.lat and p.lng for p in plan.pins)        # geocoded coordinates present
    assert all(p.source == PinSource.GEOCODED for p in plan.pins)
    assert not plan.fallback_area


def test_unvalidated_place_is_dropped_not_pinned():
    # One real, one invented. The invented one must be dropped, not pinned.
    proposer = StaticPinProposer([
        ("Banana Island", "wealth pocket"),
        ("Nonexistent Imaginary Estate", "hallucinated"),
    ])
    plan = _run(build_geo_plan("VI Realtor", "luxury real estate", "Lagos",
                               proposer, StaticGeocoder()))
    names = [p.name for p in plan.pins]
    assert "Banana Island" in names
    assert "Nonexistent Imaginary Estate" not in names   # never pin the unvalidated one


def test_nothing_validates_falls_back_to_broad_area():
    # All proposals imaginary → fall back to the city, and SAY so.
    proposer = StaticPinProposer([
        ("Fake Street One", "x"),
        ("Made Up Estate Two", "y"),
    ])
    plan = _run(build_geo_plan("Shop", "shop", "Surulere", proposer, StaticGeocoder()))
    assert plan.pins == []
    assert plan.fallback_area == "Surulere"
    assert "couldn't confirm" in plan.explanation.lower()


def test_luxury_realtor_pockets_are_the_audience():
    proposer = StaticPinProposer([
        ("Banana Island", "wealth lives here"),
        ("Dolphin Estate", "wealth lives here"),
        ("Victoria Island", "buyers work here"),
    ])
    plan = _run(build_geo_plan("Prime Homes", "luxury real estate", "Lagos",
                               proposer, StaticGeocoder()))
    assert plan.mode == GeoMode.WATERING_HOLE
    assert len(plan.pins) == 3
    # Self-contained estates get tight radii.
    bi = next(p for p in plan.pins if p.name == "Banana Island")
    assert bi.radius_km <= 2.0


def test_explanation_names_the_pockets():
    proposer = StaticPinProposer([("Bode Thomas", "commercial street where offices are")])
    plan = _run(build_geo_plan("Lunch", "restaurant", "Surulere", proposer, StaticGeocoder()))
    assert "Bode Thomas" in plan.explanation
    assert "Surulere" in plan.explanation


def test_loose_match_resolves_axis_phrasing():
    # "the Adeniran Ogunsanya axis" should still geocode via contains-match.
    proposer = StaticPinProposer([("the Adeniran Ogunsanya axis", "commercial")])
    plan = _run(build_geo_plan("Lunch", "restaurant", "Surulere", proposer, StaticGeocoder()))
    assert len(plan.pins) == 1
    assert plan.pins[0].lat is not None

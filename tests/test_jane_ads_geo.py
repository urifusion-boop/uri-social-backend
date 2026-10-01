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
    geo_plan_from_named_areas,
    whole_area_plan,
    whole_area_request,
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


# ── geo_plan_from_named_areas — the consultant's own §7 judgment, geocoded ─────

def test_named_areas_builds_a_plan_with_consultant_mode_and_reasoning():
    plan = _run(geo_plan_from_named_areas(
        "watering_hole", "Lekki",
        [{"name": "Lekki Phase 1", "reason": "new estates fitting out"}],
        "targeting where new construction happens",
        geocoder=StaticGeocoder(),
    ))
    assert plan.mode == GeoMode.WATERING_HOLE
    assert len(plan.pins) == 1
    assert plan.pins[0].name == "Lekki Phase 1"
    assert plan.explanation == "targeting where new construction happens"


def test_named_areas_non_local_returns_none():
    plan = _run(geo_plan_from_named_areas(
        "non_local", "", [{"name": "anywhere", "reason": "n/a"}], "", geocoder=StaticGeocoder(),
    ))
    assert plan is None


def test_named_areas_rejects_invalid_mode():
    plan = _run(geo_plan_from_named_areas("not_a_mode", "Lagos", [], "", geocoder=StaticGeocoder()))
    assert plan is None


def test_named_areas_never_pins_unvalidated_place():
    plan = _run(geo_plan_from_named_areas(
        "own_radius", "Surulere", [{"name": "Definitely Not A Real Street Xyz", "reason": "made up"}],
        "", geocoder=StaticGeocoder(),
    ))
    assert plan.pins == []
    assert plan.fallback_area == "Surulere"


def test_named_areas_skips_entries_with_no_name():
    plan = _run(geo_plan_from_named_areas(
        "own_radius", "Surulere", [{"reason": "no name"}, {"name": "Bode Thomas", "reason": "ok"}],
        "", geocoder=StaticGeocoder(),
    ))
    assert len(plan.pins) == 1
    assert plan.pins[0].name == "Bode Thomas"


# ── A place named in the client's own audience is the campaign's geography ──

def test_place_named_in_finds_known_areas():
    from app.agents.jane_ads.geo import place_named_in
    assert place_named_in("gym owners lekki aged 20-25") == "Lekki"
    assert place_named_in("brides-to-be in Lekki aged 25-35") == "Lekki"
    assert place_named_in("wedding planners in surulere") == "Surulere"


def test_place_named_in_prefers_the_longest_name():
    from app.agents.jane_ads.geo import place_named_in
    assert place_named_in("Lekki Phase 1 residents") == "Lekki Phase 1"


def test_place_named_in_needs_a_whole_word_and_tolerates_none():
    """Live-observed: the consultant picked Ikeja (from the earlier brief) over the
    Lekki the client had just typed, so this is matched in code rather than prompted.
    An audience naming no known place leaves the consultant's own read alone."""
    from app.agents.jane_ads.geo import place_named_in
    assert place_named_in("people in ikejawhatever") is None
    assert place_named_in("small business owners") is None
    assert place_named_in("") is None
    assert place_named_in(None) is None


# ── "All of Lagos" ────────────────────────────────────────────────────────────
# A client who asks for a whole state has ANSWERED the geography question. Jane used
# to keep asking which pockets to focus on inside it — and the app's own "ALL OF
# LAGOS" chip led straight back into the same question.

def test_a_whole_state_request_is_recognised():
    assert whole_area_request("ALL OF LAGOS") == "Lagos"
    assert whole_area_request("Ogun as a whole") == "Ogun"
    assert whole_area_request("entire Ogun State") == "Ogun"
    assert whole_area_request("everywhere in Kano") == "Kano"


def test_prose_that_merely_mentions_a_place_is_not_a_targeting_instruction():
    """The cost of a false positive is a campaign silently retargeted at a state."""
    assert whole_area_request("all of our customers in Lagos are students") == ""
    assert whole_area_request("all of it") == ""
    assert whole_area_request("Lagos") == ""


def test_a_whole_area_plan_carries_no_pins():
    """meta_targeting_from_geo_named resolves a pinless plan to the city and then its
    state, so the campaign covers exactly what was asked for."""
    plan = whole_area_plan("Lagos")
    assert plan.pins == []
    assert plan.city == "Lagos"
    assert plan.fallback_area == "Lagos"


def test_a_whole_area_plan_does_not_apologise_for_having_no_pockets():
    """It used to explain itself with "I couldn't confirm specific pockets" — an
    apology for doing exactly what the client asked."""
    plan = whole_area_plan("Rivers")
    assert "couldn't confirm" not in plan.explanation
    assert "all of Rivers" in plan.explanation


def test_named_areas_honours_a_whole_area_city_with_no_pockets():
    plan = _run(geo_plan_from_named_areas("watering_hole", "all of Lagos", []))
    assert plan is not None
    assert plan.pins == []
    assert plan.city == "Lagos"
    assert "all of Lagos" in plan.explanation


def test_a_named_pocket_still_wins_over_the_phrasing():
    """"All of Lagos, especially Ikeja" is a pocket request — the areas the consultant
    reasoned about are not discarded because the city string was phrased broadly."""
    plan = _run(geo_plan_from_named_areas(
        "watering_hole", "all of Lagos", [{"name": "Ikeja", "reason": "offices"}],
        geocoder=StaticGeocoder({"ikeja": (6.6018, 3.3515, 3.0)}),
    ))
    assert [p.name for p in plan.pins] == ["Ikeja"]


def test_a_pocket_that_is_itself_a_whole_area_phrase_is_not_geocoded():
    """Variant cards carry "All of Lagos" as their location when the client asked for
    the whole state. Geocoding that as a neighbourhood finds nothing and degraded into
    the apologetic "I couldn't confirm specific pockets" fallback."""
    plan = _run(geo_plan_from_named_areas(
        "watering_hole", "Lagos", [{"name": "All of Lagos", "reason": "as asked"}]))
    assert plan.pins == []
    assert "all of Lagos" in plan.explanation
    assert "couldn't confirm" not in plan.explanation


def test_the_request_is_recognised_inside_a_longer_brief():
    """Clients type it as part of the brief as often as they tap the chip. An anchored
    match silently honoured everything in the brief except the geography."""
    assert whole_area_request(
        "I want to promote my tool, budget 20000, all of Lagos") == "Lagos"
    assert whole_area_request("promote my tool across Ogun") == "Ogun"


def test_trailing_politeness_is_not_part_of_the_place_name():
    assert whole_area_request("target all of Rivers state please") == "Rivers"
    assert whole_area_request("all of Lagos abeg") == "Lagos"


def test_prose_inside_a_longer_message_is_still_not_a_request():
    assert whole_area_request("I spent all of my budget in Lagos") == ""
    assert whole_area_request("all of our customers in Lagos are students") == ""

"""
visual_slots.py (VSG-01 v3 §1.3/§3) — the closed slot vocabularies, the
customer-description builder that has no audience_segment argument to leak
through, and the visual-prompt leakage guard that catches it anyway if a
Zone A value gets in some other way.
"""
import pytest

from app.agents.jane_ads.visual_slots import (
    NIGERIAN_SETTINGS,
    LIGHTING_DEFAULTS,
    DRESS_REGISTERS,
    ZONE_A_FIELDS,
    ZONE_B_FIELDS,
    InvalidSlotValue,
    VisualLeakageDetected,
    resolve_nigerian_setting,
    resolve_lighting,
    resolve_surface_hex,
    build_customer_description,
    visual_leakage_terms,
    check_visual_leakage,
    assert_no_visual_leakage,
)


class TestSlotVocabulary:
    @pytest.mark.parametrize("setting", NIGERIAN_SETTINGS)
    def test_every_enumerated_setting_is_accepted(self, setting):
        assert resolve_nigerian_setting(setting) == setting

    def test_a_resolved_geo_target_is_rejected(self):
        """§3's own named bug: populating {{nigerian_setting}} from
        geo_target instead of the controlled vocabulary."""
        with pytest.raises(InvalidSlotValue):
            resolve_nigerian_setting("Lekki, Lagos")

    @pytest.mark.parametrize("lighting", LIGHTING_DEFAULTS)
    def test_every_enumerated_lighting_default_is_accepted(self, lighting):
        assert resolve_lighting(lighting) == lighting

    def test_northern_hemisphere_lighting_language_is_rejected(self):
        with pytest.raises(InvalidSlotValue):
            resolve_lighting("golden hour in autumn")

    def test_surface_hex_reads_from_the_token_set(self):
        assert resolve_surface_hex({"surface": "#0F766E", "field": "#FFFFFF"}) == "#0F766E"


class TestCustomerDescription:
    def test_dress_registers_match_section_3(self):
        assert set(DRESS_REGISTERS) == {"casual", "workwear", "formal", "traditional"}

    def test_always_includes_skin_tone_and_west_african_features(self):
        desc = build_customer_description(age_range="mid-20s", dress_register="workwear")
        assert "deep brown to dark brown skin tones" in desc
        assert "West African features" in desc

    def test_has_no_audience_segment_parameter_at_all(self):
        """§3: 'Never populated from audience_segment.' Structural, not
        just documented — there is no argument to pass one through."""
        import inspect
        params = inspect.signature(build_customer_description).parameters
        assert "audience_segment" not in params

    def test_invalid_dress_register_rejected(self):
        with pytest.raises(InvalidSlotValue):
            build_customer_description(age_range="30s", dress_register="business_casual")

    def test_gender_included_only_when_given(self):
        with_gender = build_customer_description(age_range="30s", dress_register="formal", gender="woman")
        without_gender = build_customer_description(age_range="30s", dress_register="formal")
        assert "woman" in with_gender
        assert "woman" not in without_gender


class TestZoneFields:
    def test_zone_a_and_zone_b_do_not_overlap(self):
        assert set(ZONE_A_FIELDS).isdisjoint(set(ZONE_B_FIELDS))

    def test_zone_a_matches_the_section_1_3_table(self):
        assert set(ZONE_A_FIELDS) == {
            "geo_target", "audience_segment", "interest_categories",
            "platform", "objective", "budget",
        }

    def test_zone_b_matches_the_section_1_3_table(self):
        assert set(ZONE_B_FIELDS) == {"service_area", "who_its_for", "price", "the_action"}


class TestVisualLeakageGuard:
    def test_the_named_bug_a_bag_on_a_table_in_lekki(self):
        """§1.3's own example of the error arriving through visual
        instruction: 'A bag on a table in Lekki' is the identical error to
        'Lekki creatives' in a headline."""
        prompt = "A bag on a table in Lekki, strong equatorial daylight, documentary style"
        terms = visual_leakage_terms(geo_target="Lekki")
        assert check_visual_leakage(prompt, terms) == ["Lekki"]
        with pytest.raises(VisualLeakageDetected):
            assert_no_visual_leakage(prompt, terms)

    def test_a_clean_prompt_with_controlled_slots_passes(self):
        prompt = (
            f"{resolve_nigerian_setting('an open-air market stall')}, "
            f"{resolve_lighting('strong equatorial daylight')}, "
            f"{build_customer_description(age_range='30s', dress_register='casual')}"
        )
        terms = visual_leakage_terms(geo_target="Lekki", audience_segment="young professionals")
        assert check_visual_leakage(prompt, terms) == []
        assert_no_visual_leakage(prompt, terms)  # does not raise

    def test_geo_pockets_are_checked_same_as_geo_target(self):
        """Live-confirmed leak class on the copy side (creative.py):
        a selected audience-plan variant's own named areas are real
        targeting parameters too, distinct from and often narrower than
        geo_target — the same must hold for visual prompts."""
        prompt = "A tailoring workshop near Yaba market"
        terms = visual_leakage_terms(geo_target="Lagos", geo_pockets=["Yaba"])
        assert "Yaba" in check_visual_leakage(prompt, terms)

    def test_interest_category_leakage_is_caught(self):
        prompt = "A fitness class scene emphasising weight loss journeys"
        terms = visual_leakage_terms(interest_categories="weight loss journeys")
        assert check_visual_leakage(prompt, terms) == ["weight loss journeys"]

    def test_service_area_and_price_zone_b_are_never_flagged(self):
        """Zone B fields aren't run through this guard at all — they're
        meant to appear. This test documents that by simply never passing
        them into visual_leakage_terms, which has no parameter for them."""
        import inspect
        params = inspect.signature(visual_leakage_terms).parameters
        assert "service_area" not in params
        assert "price" not in params

    def test_no_leakage_terms_never_raises(self):
        assert_no_visual_leakage("any prompt text at all", [])

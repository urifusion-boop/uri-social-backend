"""
Uri Market Intelligence — relevance_breakdown's geo_fit component
(classification/scoring.py) compared a mentioned CITY against the Brand
Playbook's "Region / market" chips (a country/zone label) with a plain
substring check — "lagos" is never a substring of "nigeria", so a real
Nigeria-based account's own evidence could never score the top geo_fit
point even when the city genuinely was in a country the business serves.
geo.py's resolve_text_country + region_chips_cover_country fix that by
resolving both sides to a real country code before comparing.
"""
from app.agents.market_intelligence.listening.geo import (
    region_chips_cover_country,
    resolve_text_country,
)


class TestResolveTextCountry:
    def test_resolves_a_major_city(self):
        assert resolve_text_country("Lagos") == "NG"

    def test_resolves_a_country_name(self):
        assert resolve_text_country("somewhere in Nigeria") == "NG"

    def test_unplaceable_text_returns_none(self):
        assert resolve_text_country("some random market stall") is None

    def test_empty_text_returns_none(self):
        assert resolve_text_country(None) is None
        assert resolve_text_country("") is None


class TestRegionChipsCoverCountry:
    def test_the_literal_live_bug_lagos_is_covered_by_the_nigeria_chip(self):
        """The actual bug: a Nigeria-based account's own Lagos evidence
        used to never match because "lagos" isn't a substring of
        "nigeria" — this is the real fix, resolved through country codes."""
        assert region_chips_cover_country("Nigeria", "NG") is True

    def test_west_africa_chip_covers_a_different_west_african_country(self):
        assert region_chips_cover_country("West Africa", "GH") is True

    def test_pan_african_chip_covers_any_african_country(self):
        assert region_chips_cover_country("Pan-African", "KE") is True

    def test_us_only_chip_does_not_cover_nigeria(self):
        assert region_chips_cover_country("United States", "NG") is False

    def test_global_chip_covers_anything(self):
        assert region_chips_cover_country("Global / International", "IN") is True

    def test_multiple_selected_chips_each_get_checked(self):
        assert region_chips_cover_country("United States, Nigeria", "NG") is True
        assert region_chips_cover_country("United States, Nigeria", "GB") is False

    def test_other_alone_cannot_tell(self):
        assert region_chips_cover_country("Other", "NG") is None

    def test_missing_region_or_country_cannot_tell(self):
        assert region_chips_cover_country(None, "NG") is None
        assert region_chips_cover_country("Nigeria", None) is None

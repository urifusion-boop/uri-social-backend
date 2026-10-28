"""
A Nigeria-scoped account (Settings -> Listening location = Nigeria) kept
showing Upcoming Items for events in New Delhi and Las Vegas. Root cause,
confirmed live: Topic.geographic_scope (set once, at topic-creation time,
from the Brand Playbook's loose "customer_location" text, often a whole
continent like "Africa") was never the same thing as the account's real,
canonical location_scope (mi_listening_profiles, written by Settings'
Listening location card) — and neither was ever enforced as a real filter
anywhere downstream, only used as a soft prompt hint.

Three fixes, tested here:
1. A new topic's suggested geographic_scope now prefers the account's real
   location_scope over the Playbook's loose text (location_scope_display_name).
2. scan_runner.py's actual collection calls now resolve and pass the
   account's real location as a hint (resolve_topic_location_hint), reusing
   the same mechanism the shared-market subsystem already proved out.
3. GET /developments backstop-filters out anything whose own extracted
   location clearly names a different country (development_location_conflicts)
   — catches whatever slips through from before the fix, or from a source
   with no real upstream geo lever.
"""
import asyncio

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.market_intelligence.listening.geo import (
    development_location_conflicts,
    location_scope_display_name,
    resolve_topic_location_hint,
)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["topic_location_test"]


# ── location_scope_display_name ─────────────────────────────────────────────

class TestLocationScopeDisplayName:
    def test_country_level(self):
        assert location_scope_display_name({"level": "country", "country": "NG"}) == "Nigeria"

    def test_city_level_with_country(self):
        assert location_scope_display_name(
            {"level": "city", "country": "NG", "city": "Lagos"}
        ) == "Lagos, Nigeria"

    def test_continent_level(self):
        assert location_scope_display_name({"level": "continent", "continent": "AFRICA"}) == "Africa"

    def test_global_level_returns_none(self):
        assert location_scope_display_name({"level": "global"}) is None

    def test_empty_scope_returns_none(self):
        assert location_scope_display_name({}) is None
        assert location_scope_display_name(None) is None

    def test_unknown_country_code_returns_none(self):
        assert location_scope_display_name({"level": "country", "country": "ZZ"}) is None


# ── resolve_topic_location_hint ─────────────────────────────────────────────

class TestResolveTopicLocationHint:
    def test_country_level_resolves_to_a_real_hint(self):
        db = fresh_db()
        run(db["mi_listening_profiles"].insert_one(
            {"brand_id": "b1", "location_scope": {"level": "country", "country": "NG"}}
        ))
        hint = run(resolve_topic_location_hint(db, "b1"))
        assert hint["country"] == "NG"
        assert hint["news_edition"].startswith("NG:")

    def test_no_profile_returns_none(self):
        db = fresh_db()
        assert run(resolve_topic_location_hint(db, "no-such-brand")) is None

    def test_global_scope_returns_none(self):
        db = fresh_db()
        run(db["mi_listening_profiles"].insert_one(
            {"brand_id": "b1", "location_scope": {"level": "global"}}
        ))
        assert run(resolve_topic_location_hint(db, "b1")) is None

    def test_continent_scope_returns_none(self):
        """Continent has no single country to hand an adapter — that's the
        shared-market subsystem's own per-country-explosion job, not a
        single business's one topic scan."""
        db = fresh_db()
        run(db["mi_listening_profiles"].insert_one(
            {"brand_id": "b1", "location_scope": {"level": "continent", "continent": "AFRICA"}}
        ))
        assert run(resolve_topic_location_hint(db, "b1")) is None

    def test_city_level_includes_place_hints_when_known(self):
        db = fresh_db()
        run(db["mi_known_places"].insert_one({
            "id": "lagos_ng", "level": "city", "name": "Lagos", "country": "NG",
            "source_hints": {"x_near": "Lagos, Nigeria", "x_radius_km": 30},
        }))
        run(db["mi_listening_profiles"].insert_one(
            {"brand_id": "b1", "location_scope": {"level": "city", "country": "NG", "place_id": "lagos_ng"}}
        ))
        hint = run(resolve_topic_location_hint(db, "b1"))
        assert hint["country"] == "NG"
        assert hint["x_near"] == "Lagos, Nigeria"
        assert hint["place_id"] == "lagos_ng"


# ── development_location_conflicts ──────────────────────────────────────────

class TestDevelopmentLocationConflicts:
    def test_confirmed_real_mismatch_new_delhi(self):
        assert development_location_conflicts("New Delhi, India", "NG") is True

    def test_confirmed_real_mismatch_las_vegas(self):
        assert development_location_conflicts("Las Vegas, United States", "NG") is True

    def test_matching_country_is_not_a_conflict(self):
        assert development_location_conflicts("Lagos, Nigeria", "NG") is False

    def test_no_location_text_is_not_a_conflict(self):
        """A missed match is far less harmful than hiding something that
        might genuinely be relevant — ambiguous/unknown stays visible."""
        assert development_location_conflicts(None, "NG") is False
        assert development_location_conflicts("", "NG") is False

    def test_bare_city_with_no_country_name_is_not_a_conflict(self):
        """Honest limitation: a city not in either lookup can't be placed
        — stays visible rather than guessing."""
        assert development_location_conflicts("Some Convention Center", "NG") is False

    def test_no_expected_country_is_not_a_conflict(self):
        assert development_location_conflicts("New Delhi, India", "") is False

    def test_bare_city_new_delhi_with_no_country_word_is_a_conflict(self):
        """The actual live bug: a Development's extracted location is a
        bare city name with no country word at all — "New Delhi", not
        "New Delhi, India". MAJOR_CITY_COUNTRY is what makes this catchable."""
        assert development_location_conflicts("New Delhi", "NG") is True

    def test_bare_venue_and_city_las_vegas_with_no_country_word_is_a_conflict(self):
        assert development_location_conflicts("Mandalay Bay, Las Vegas", "NG") is True

    def test_major_city_matching_expected_country_is_not_a_conflict(self):
        assert development_location_conflicts("New Delhi", "IN") is False
        assert development_location_conflicts("Johannesburg", "ZA") is False

"""
Location-scoped listening (Phase F): a business can scope collection to a
continent, a country, or a specific city/region, instead of only the old
AI-inferred single country. Uses an in-memory MongoDB; no network, no AI.
"""
import asyncio
from datetime import datetime

import pytest

mongomock_motor = pytest.importorskip("mongomock_motor")

from app.agents.market_intelligence.listening import geo, markets, profile
from app.agents.market_intelligence.listening.geo import LocationScope

NOW = datetime(2026, 10, 26, 12, 0)


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def fresh_db():
    return mongomock_motor.AsyncMongoMockClient()["location_scope_test"]


# ── geo.py registry ─────────────────────────────────────────────────────────

def test_continent_resolves_to_real_per_country_list_not_one_blob():
    scope = LocationScope(level="continent", continent="AF")
    countries = geo.resolve_countries(scope)
    assert set(countries) == {"NG", "GH", "KE", "ZA"}


def test_country_level_resolves_to_itself():
    assert geo.resolve_countries(LocationScope(level="country", country="ng")) == ["NG"]


def test_global_resolves_to_global():
    assert geo.resolve_countries(LocationScope(level="global")) == ["GLOBAL"]


def test_unknown_continent_resolves_to_nothing():
    assert geo.resolve_countries(LocationScope(level="continent", continent="ZZ")) == []


def test_known_places_seed_and_are_readable():
    async def go():
        db = fresh_db()
        await geo.ensure_seeded(db)
        lagos = await geo.get_place(db, "lagos_ng")
        assert lagos["country"] == "NG" and lagos["source_hints"]["x_near"] == "Lagos, Nigeria"
        ng_places = await geo.list_places(db, country="NG")
        assert {p["id"] for p in ng_places} == {"lagos_ng", "abuja_ng", "port_harcourt_ng", "kano_ng", "ibadan_ng"}
        # seeding twice never duplicates or overwrites
        await geo.ensure_seeded(db)
        assert await db["mi_known_places"].count_documents({"id": "lagos_ng"}) == 1
    run(go())


# ── markets.py: location hints and city-scoped queries ─────────────────────

def _category():
    return {"id": "hair_wigs", "core_terms": ["wig", "frontal wig", "closure wig"], "local_terms": {}}


def test_base_queries_get_a_country_location_hint_with_a_default_news_edition():
    plan = markets.compile_queries(_category(), "NG", {}, [], max_queries=10, source="news")
    assert plan["queries"], "expected at least one query"
    for q in plan["queries"]:
        assert q["location_hint"] == {"country": "NG", "news_edition": "NG:en"}


def test_global_market_gets_no_location_hint():
    plan = markets.compile_queries(_category(), "GLOBAL", {}, [], max_queries=10, source="news")
    assert all(q["location_hint"] is None for q in plan["queries"])


def test_requested_place_adds_extra_geo_tagged_queries_without_removing_base_ones():
    lagos = geo.KNOWN_PLACES[0]
    assert lagos["id"] == "lagos_ng"
    base_plan = markets.compile_queries(_category(), "NG", {}, [], max_queries=10, source="x")
    geo_plan = markets.compile_queries(_category(), "NG", {}, [], max_queries=10, source="x", places=[lagos])
    base_kinds = {q["kind"] for q in base_plan["queries"]}
    geo_kinds = {q["kind"] for q in geo_plan["queries"]}
    assert base_kinds.issubset(geo_kinds), "base (country-wide) queries must still be present"
    new_kinds = geo_kinds - base_kinds
    assert new_kinds and all(k.startswith("geo:lagos_ng") for k in new_kinds)
    geo_queries = [q for q in geo_plan["queries"] if q["kind"].startswith("geo:")]
    assert all(q["location_hint"]["x_near"] == "Lagos, Nigeria" for q in geo_queries)
    assert len(geo_queries) <= markets.CITY_QUERIES_PER_PLACE


def test_request_place_tracks_requesting_brand_and_unrequest_removes_it():
    async def go():
        db = fresh_db()
        m = await markets.ensure_market(db, "hair_wigs", "NG")
        await markets.request_place(db, m["key"], "lagos_ng", "brand1")
        updated = await db["mi_markets"].find_one({"key": m["key"]})
        assert markets.requested_places(updated) == ["lagos_ng"]

        # a second brand requesting the same place doesn't duplicate
        await markets.request_place(db, m["key"], "lagos_ng", "brand2")
        updated = await db["mi_markets"].find_one({"key": m["key"]})
        assert set(updated["city_requests"]["lagos_ng"]) == {"brand1", "brand2"}

        # brand1 leaving doesn't remove brand2's request
        await markets.unrequest_place(db, m["key"], "lagos_ng", "brand1")
        updated = await db["mi_markets"].find_one({"key": m["key"]})
        assert updated["city_requests"]["lagos_ng"] == ["brand2"]
        assert markets.requested_places(updated) == ["lagos_ng"]

        # brand2 leaving too: the place is no longer requested by anyone
        await markets.unrequest_place(db, m["key"], "lagos_ng", "brand2")
        updated = await db["mi_markets"].find_one({"key": m["key"]})
        assert markets.requested_places(updated) == []
    run(go())


# ── profile.py: set_location_scope ──────────────────────────────────────────

async def _seed_profile(db, brand_id="brandA", markets_keys=None):
    await db["mi_listening_profiles"].insert_one({
        "brand_id": brand_id, "markets": markets_keys or ["hair_wigs:GLOBAL"],
        "country": "GLOBAL", "version": 1, "field_origins": {},
    })


def test_set_location_scope_continent_subscribes_to_every_member_country():
    async def go():
        db = fresh_db()
        await _seed_profile(db)
        updated = await profile.set_location_scope(db, "brandA", LocationScope(level="continent", continent="AF"))
        assert set(updated["markets"]) == {"hair_wigs:NG", "hair_wigs:GH", "hair_wigs:KE", "hair_wigs:ZA"}
        assert updated["location_scope"]["level"] == "continent"
        for key in updated["markets"]:
            m = await db["mi_markets"].find_one({"key": key})
            assert "brandA" in m["subscribers"]
        # the old GLOBAL market subscription is gone
        old = await db["mi_markets"].find_one({"key": "hair_wigs:GLOBAL"})
        assert old is None or "brandA" not in (old.get("subscribers") or [])
    run(go())


def test_set_location_scope_city_subscribes_country_market_and_requests_place():
    async def go():
        db = fresh_db()
        await _seed_profile(db)
        updated = await profile.set_location_scope(
            db, "brandA", LocationScope(level="city", place_id="lagos_ng"))
        assert updated["markets"] == ["hair_wigs:NG"]
        assert updated["country"] == "NG"
        m = await db["mi_markets"].find_one({"key": "hair_wigs:NG"})
        assert "brandA" in m["subscribers"]
        assert markets.requested_places(m) == ["lagos_ng"]
    run(go())


def test_set_location_scope_switching_city_to_country_drops_the_place_request():
    async def go():
        db = fresh_db()
        await _seed_profile(db)
        await profile.set_location_scope(db, "brandA", LocationScope(level="city", place_id="lagos_ng"))
        await profile.set_location_scope(db, "brandA", LocationScope(level="country", country="NG"))
        m = await db["mi_markets"].find_one({"key": "hair_wigs:NG"})
        assert markets.requested_places(m) == [], "switching away from a city must drop its place request"
        assert "brandA" in m["subscribers"], "still subscribed to the country market itself"
    run(go())


def test_set_location_scope_rejects_unknown_place():
    async def go():
        db = fresh_db()
        await _seed_profile(db)
        with pytest.raises(ValueError):
            await profile.set_location_scope(db, "brandA", LocationScope(level="city", place_id="nowhere"))
    run(go())


def test_set_location_scope_returns_none_for_brand_with_no_profile():
    async def go():
        db = fresh_db()
        result = await profile.set_location_scope(db, "no_such_brand", LocationScope(level="global"))
        assert result is None
    run(go())

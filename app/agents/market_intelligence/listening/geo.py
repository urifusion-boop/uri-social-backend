"""
Location scoping for listening (PRD extension): a business can scope
collection to a continent, a country, or a specific city/region inside a
country, instead of only today's AI-inferred single country or GLOBAL.

This module is the one canonical place a business picks a location — every
source-specific "shape" (X wants a place name, TikTok wants an ISO-2 code,
Instagram wants a numeric location id, Reddit wants a subreddit, News wants
a combined gl:hl "edition") is resolved from here, so adding a new place
later never means touching adapter code. See `mi_known_places` below.

Markets stay country-level (unchanged) — that's what shared collection and
trend math are built around; fragmenting shared pools into per-city markets
would collapse volume for everything but the biggest cities. Continent scope
is real per-country precision: it explodes into every member country's own
market (`resolve_countries`), each collected and scored properly — cost
scales with how many countries are included, exactly like subscribing to
that many countries individually. Region/city scope stays on the country
market (so country-level subscribers of the same shared market are never
narrowed) and adds its own geo-scoped queries on top — see markets.py's
`city_queries`.

Every real country on Earth is selectable (COUNTRIES below, loaded from
`data/countries.json` — a canonical ISO-3166/region dataset, the same one
the frontend's own country picker is built from, not a hand-typed list —
see `data/fetch_countries.py`), and so is any city — typing a new one
resolves it on the spot (`resolve_or_create_place`) instead of requiring it
to appear on a pre-built list first. The first version of this feature
(23-26 Oct 2026) shipped with only Nigeria's five largest cities
pre-curated — a deliberate, narrow pilot to validate the mechanism on real
data before widening it, the same "test first, then open up" pattern X's
two connectors went through before every other source was added (see
Phase A). It was never meant to stay that narrow: Uri's businesses are not
only in Nigeria, and a city list that only covered one country would have
been a real product gap, not a design choice — this module now covers
every country, and any named city, on purpose.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

_DATA_FILE = Path(__file__).parent / "data" / "countries.json"
_RAW = json.loads(_DATA_FILE.read_text())

# Continent groupings use the dataset's own `region` field directly
# (Africa, Americas, Asia, Europe, Oceania) rather than a hand-split
# North/South America — one less hand-authored judgment call, and "Americas"
# is itself a standard grouping (the same one the UN geoscheme and the
# source dataset both use).
CONTINENTS: dict[str, dict[str, Any]] = {}
for _row in _RAW["countries"]:
    _key = _row["region"].upper().replace(" ", "_")
    CONTINENTS.setdefault(_key, {"label": _row["region"], "countries": []})
    CONTINENTS[_key]["countries"].append(_row["code"])

# Extra local-language terms (beyond English) are only worth adding where
# the ontology actually has local_terms seeded for that language (today:
# Nigerian Pidgin, Swahili) — every other country defaults to English-only
# search, same safe, honest default as GLOBAL used before this module
# existed. Extending this list is additive, data-only work (no code
# changes) once a market's local terms are written for a new language.
_EXTRA_LANGS: dict[str, list[str]] = {"NG": ["pcm"], "GH": ["pcm"], "KE": ["sw"]}

# Google News' real editions are a curated ~87-combination list, not every
# country × every language — guessing a country's spoken language and
# assuming "<country>:<that language>" is a valid edition risks sending a
# combination that doesn't exist. Only set where independently confirmed
# (so far: Nigeria, Ghana, Kenya, South Africa, UK, US, matching this
# phase's own verified research) — everywhere else, News collection simply
# runs without a country-specific edition until confirmed, the same
# "report what's not yet available" pattern used everywhere else in this
# product, rather than a silent guess.
_NEWS_EDITIONS: dict[str, str] = {
    "NG": "NG:en", "GH": "GH:en", "KE": "KE:en", "ZA": "ZA:en", "GB": "GB:en", "US": "US:en",
}

_CONTINENT_OF: dict[str, str] = {row["code"]: row["region"].upper().replace(" ", "_") for row in _RAW["countries"]}

COUNTRIES: dict[str, dict[str, Any]] = {
    row["code"]: {"name": row["name"], "continent": _CONTINENT_OF.get(row["code"]),
                  "langs": ["en"] + _EXTRA_LANGS.get(row["code"], [])}
    for row in _RAW["countries"]
}
COUNTRIES["GLOBAL"] = {"name": "Global", "continent": None, "langs": ["en"]}

COUNTRY_LANGS: dict[str, list[str]] = {c: v["langs"] for c, v in COUNTRIES.items()}
COUNTRY_NAMES: dict[str, str] = {c: v["name"] for c, v in COUNTRIES.items()}


class LocationScope(BaseModel):
    """What a business has explicitly asked listening to be scoped to.
    `level="global"` is today's default (unchanged behavior). For
    region/city level, either `place_id` (an existing known place) or
    `city` + `country` (any free-text city — resolved, and cached for
    reuse, on the spot) must be given; `country` alone is required for
    country level; `continent` for continent level."""
    level: str = Field(description='"global" | "continent" | "country" | "region" | "city"')
    continent: Optional[str] = None
    country: Optional[str] = None
    place_id: Optional[str] = None
    city: Optional[str] = None


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")


# Curated upgrades: places where someone has looked up the two fields no
# city gets automatically (a real TikTok place-URL id, a real Instagram
# numeric location id — see module docstring). Starting with Nigeria's
# five largest cities is a pilot, not a ceiling — `resolve_or_create_place`
# below means a business in any other city is never blocked from city-level
# scoping while waiting for this list to catch up; it just starts without
# those two sources' extra precision until someone curates it, same as
# every city not yet on this list.
KNOWN_PLACES: list[dict[str, Any]] = [
    {"id": "lagos_ng", "level": "city", "name": "Lagos", "country": "NG", "continent": "AFRICA",
     "aliases": ["lagos", "eko"], "coordinates": {"lat": 6.5244, "lon": 3.3792}, "source": "curated",
     "source_hints": {"x_near": "Lagos, Nigeria", "x_radius_km": 30,
                       "tiktok_place_url": None, "instagram_location_id": None,
                       "reddit_subreddits": ["lagos", "Nigeria"], "news_edition": "NG:en"}},
    {"id": "abuja_ng", "level": "city", "name": "Abuja", "country": "NG", "continent": "AFRICA",
     "aliases": ["abuja", "fct"], "coordinates": {"lat": 9.0765, "lon": 7.3986}, "source": "curated",
     "source_hints": {"x_near": "Abuja, Nigeria", "x_radius_km": 25,
                       "tiktok_place_url": None, "instagram_location_id": None,
                       "reddit_subreddits": ["abuja", "Nigeria"], "news_edition": "NG:en"}},
    {"id": "port_harcourt_ng", "level": "city", "name": "Port Harcourt", "country": "NG", "continent": "AFRICA",
     "aliases": ["port harcourt", "ph city"], "coordinates": {"lat": 4.8156, "lon": 7.0498}, "source": "curated",
     "source_hints": {"x_near": "Port Harcourt, Nigeria", "x_radius_km": 25,
                       "tiktok_place_url": None, "instagram_location_id": None,
                       "reddit_subreddits": ["Nigeria"], "news_edition": "NG:en"}},
    {"id": "kano_ng", "level": "city", "name": "Kano", "country": "NG", "continent": "AFRICA",
     "aliases": ["kano"], "coordinates": {"lat": 12.0022, "lon": 8.5920}, "source": "curated",
     "source_hints": {"x_near": "Kano, Nigeria", "x_radius_km": 25,
                       "tiktok_place_url": None, "instagram_location_id": None,
                       "reddit_subreddits": ["Nigeria"], "news_edition": "NG:en"}},
    {"id": "ibadan_ng", "level": "city", "name": "Ibadan", "country": "NG", "continent": "AFRICA",
     "aliases": ["ibadan"], "coordinates": {"lat": 7.3775, "lon": 3.9470}, "source": "curated",
     "source_hints": {"x_near": "Ibadan, Nigeria", "x_radius_km": 25,
                       "tiktok_place_url": None, "instagram_location_id": None,
                       "reddit_subreddits": ["Nigeria"], "news_edition": "NG:en"}},
]


async def ensure_seeded(db) -> None:
    """Seed missing known places; never overwrite an edited one — same
    convention as ontology.ensure_seeded."""
    now = datetime.utcnow()
    for place in KNOWN_PLACES:
        if not await db["mi_known_places"].find_one({"id": place["id"]}):
            await db["mi_known_places"].insert_one({**place, "created_at": now})


async def list_places(db, country: Optional[str] = None) -> list[dict]:
    q: dict[str, Any] = {}
    if country:
        q["country"] = country.upper()
    out = []
    async for p in db["mi_known_places"].find(q):
        p.pop("_id", None)
        out.append(p)
    return out


async def get_place(db, place_id: str) -> Optional[dict]:
    p = await db["mi_known_places"].find_one({"id": place_id})
    if p:
        p.pop("_id", None)
    return p


async def resolve_or_create_place(db, city: str, country: str) -> Optional[dict]:
    """Any named city in any real country can be used for city-level
    scoping immediately — it doesn't need to already be on KNOWN_PLACES.
    Generates reasonable default hints for the sources that need only a
    plain place name (X's near/radius, Reddit's subreddit guess); the two
    sources that need a real looked-up id (a TikTok place URL, an
    Instagram numeric location id) start unset, same as any city not yet
    curated — upgradeable later without the business having to redo
    anything. The generated place is cached in `mi_known_places` (tagged
    `source: "generated"`) so the next business asking for the same city
    reuses it instead of regenerating it, and so it shows up for curation."""
    code = (country or "").upper()
    if code not in COUNTRIES or code == "GLOBAL":
        return None
    city = (city or "").strip()
    if not city:
        return None
    place_id = f"{_slugify(city)}_{code.lower()}"
    await db["mi_known_places"].create_index("id", unique=True)
    existing = await get_place(db, place_id)
    if existing:
        return existing
    country_name = COUNTRY_NAMES.get(code, code)
    doc = {
        "id": place_id, "level": "city", "name": city, "country": code,
        "continent": _CONTINENT_OF.get(code), "aliases": [city.lower()],
        "coordinates": None, "source": "generated",
        "source_hints": {
            "x_near": f"{city}, {country_name}", "x_radius_km": 25,
            "tiktok_place_url": None, "instagram_location_id": None,
            "reddit_subreddits": [_slugify(city), country_name],
            "news_edition": _NEWS_EDITIONS.get(code),
        },
        "created_at": datetime.utcnow(),
    }
    # Atomic upsert, not insert_one: two businesses typing the same
    # brand-new city at nearly the same moment must never create two
    # documents with the same id — $setOnInsert only writes `doc` the
    # first time; a concurrent second call becomes a no-op write that
    # still returns the (first writer's) real, single document below.
    await db["mi_known_places"].update_one({"id": place_id}, {"$setOnInsert": doc}, upsert=True)
    return await get_place(db, place_id)


def resolve_countries(scope: LocationScope) -> list[str]:
    """Which country codes this scope resolves to, for market
    ensure/subscribe. Continent explodes to every member country (real
    per-country precision, per the user's decision — never one shared,
    loosely-filtered continent query)."""
    if scope.level == "global":
        return ["GLOBAL"]
    if scope.level == "continent":
        c = CONTINENTS.get((scope.continent or "").upper().replace(" ", "_"))
        return list(c["countries"]) if c else []
    if scope.level == "country":
        code = (scope.country or "").upper()
        return [code] if code in COUNTRIES and code != "GLOBAL" else []
    # region/city: resolved by the caller via get_place/resolve_or_create_place
    return []

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
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

# Real-world continent membership, trimmed to countries this product can
# actually serve today (COUNTRIES below) — not every country on Earth.
# Extend both together when a new country is added.
CONTINENTS: dict[str, dict[str, Any]] = {
    "AF": {"label": "Africa", "countries": ["NG", "GH", "KE", "ZA"]},
    "EU": {"label": "Europe", "countries": ["GB"]},
    "NA": {"label": "North America", "countries": ["US"]},
}

# Supersedes markets.py's old hardcoded COUNTRY_LANGS/COUNTRY_NAMES — one
# source of truth. `langs` keeps the existing local-language-term selection
# behavior; nothing changes for these 6 countries, just where it's defined.
COUNTRIES: dict[str, dict[str, Any]] = {
    "NG": {"name": "Nigeria", "continent": "AF", "langs": ["en", "pcm"]},
    "GH": {"name": "Ghana", "continent": "AF", "langs": ["en", "pcm"]},
    "KE": {"name": "Kenya", "continent": "AF", "langs": ["en", "sw"]},
    "ZA": {"name": "South Africa", "continent": "AF", "langs": ["en"]},
    "GB": {"name": "United Kingdom", "continent": "EU", "langs": ["en"]},
    "US": {"name": "United States", "continent": "NA", "langs": ["en"]},
    "GLOBAL": {"name": "Global", "continent": None, "langs": ["en"]},
}

COUNTRY_LANGS: dict[str, list[str]] = {c: v["langs"] for c, v in COUNTRIES.items()}
COUNTRY_NAMES: dict[str, str] = {c: v["name"] for c, v in COUNTRIES.items()}


class LocationScope(BaseModel):
    """What a business has explicitly asked listening to be scoped to.
    `level="global"` is today's default (unchanged behavior). `place_id`
    (a `mi_known_places` id) is required for region/city level; `country`
    is required for country level; `continent` for continent level."""
    level: str = Field(description='"global" | "continent" | "country" | "region" | "city"')
    continent: Optional[str] = None
    country: Optional[str] = None
    place_id: Optional[str] = None


# First known places: Nigeria's major cities only (PRD decision — every
# other existing country stays at country-level selection until a city is
# explicitly asked for; don't pre-build speculative coverage). `source_hints`
# fields left None mean that source has no real geo lever for this place yet
# (verified per-actor in Phase F's research) — reported honestly via each
# adapter's own capabilities().notes, never a silent no-op.
KNOWN_PLACES: list[dict[str, Any]] = [
    {"id": "lagos_ng", "level": "city", "name": "Lagos", "country": "NG", "continent": "AF",
     "aliases": ["lagos", "eko"], "coordinates": {"lat": 6.5244, "lon": 3.3792},
     "source_hints": {"x_near": "Lagos, Nigeria", "x_radius_km": 30,
                       "tiktok_place_url": None, "instagram_location_id": None,
                       "reddit_subreddits": ["lagos", "Nigeria"], "news_edition": "NG:en"}},
    {"id": "abuja_ng", "level": "city", "name": "Abuja", "country": "NG", "continent": "AF",
     "aliases": ["abuja", "fct"], "coordinates": {"lat": 9.0765, "lon": 7.3986},
     "source_hints": {"x_near": "Abuja, Nigeria", "x_radius_km": 25,
                       "tiktok_place_url": None, "instagram_location_id": None,
                       "reddit_subreddits": ["abuja", "Nigeria"], "news_edition": "NG:en"}},
    {"id": "port_harcourt_ng", "level": "city", "name": "Port Harcourt", "country": "NG", "continent": "AF",
     "aliases": ["port harcourt", "ph city"], "coordinates": {"lat": 4.8156, "lon": 7.0498},
     "source_hints": {"x_near": "Port Harcourt, Nigeria", "x_radius_km": 25,
                       "tiktok_place_url": None, "instagram_location_id": None,
                       "reddit_subreddits": ["Nigeria"], "news_edition": "NG:en"}},
    {"id": "kano_ng", "level": "city", "name": "Kano", "country": "NG", "continent": "AF",
     "aliases": ["kano"], "coordinates": {"lat": 12.0022, "lon": 8.5920},
     "source_hints": {"x_near": "Kano, Nigeria", "x_radius_km": 25,
                       "tiktok_place_url": None, "instagram_location_id": None,
                       "reddit_subreddits": ["Nigeria"], "news_edition": "NG:en"}},
    {"id": "ibadan_ng", "level": "city", "name": "Ibadan", "country": "NG", "continent": "AF",
     "aliases": ["ibadan"], "coordinates": {"lat": 7.3775, "lon": 3.9470},
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


def resolve_countries(scope: LocationScope) -> list[str]:
    """Which country codes this scope resolves to, for market
    ensure/subscribe. Continent explodes to every member country (real
    per-country precision, per the user's decision — never one shared,
    loosely-filtered continent query)."""
    if scope.level == "global":
        return ["GLOBAL"]
    if scope.level == "continent":
        c = CONTINENTS.get((scope.continent or "").upper())
        return list(c["countries"]) if c else []
    if scope.level == "country":
        code = (scope.country or "").upper()
        return [code] if code in COUNTRIES else []
    # region/city: resolved by the caller via get_place(scope.place_id)["country"]
    return []

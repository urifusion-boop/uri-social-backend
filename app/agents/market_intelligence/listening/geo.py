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


def location_scope_display_name(scope: dict) -> Optional[str]:
    """A plain, human-readable name for a saved location_scope dict (e.g.
    "Nigeria", "Lagos, Nigeria", "Africa") — or None for global/unset.
    Used to default a new topic's geographic_scope to what the business
    actually configured in Settings, instead of the Brand Playbook's own
    loose "customer_location" free text (often a whole continent, e.g.
    "Africa", even when Settings has since been set to a specific
    country) — see resolve_topic_location_hint's docstring for the live
    bug this mismatch caused."""
    scope = scope or {}
    level = scope.get("level")
    if level == "city":
        country_name = COUNTRY_NAMES.get((scope.get("country") or "").upper())
        city = scope.get("city")
        if city and country_name:
            return f"{city}, {country_name}"
        return city or country_name
    if level == "country":
        return COUNTRY_NAMES.get((scope.get("country") or "").upper())
    if level == "continent":
        c = CONTINENTS.get((scope.get("continent") or "").upper().replace(" ", "_"))
        return c["label"] if c else None
    return None


# A development's own extracted location is almost always a bare city or
# venue name ("New Delhi", "Mandalay Bay, Las Vegas") with no country word
# in it at all — confirmed live, this is actually the common case, not the
# exception. A country-name-only check (below) misses it completely. This
# is a curated, bounded list of major capitals and conference/exhibition
# hub cities (the kind an "Upcoming Items" announcement actually names),
# not an exhaustive world-cities gazetteer — the same deliberately-narrow,
# confirmed-not-guessed philosophy as _NEWS_EDITIONS above. A city missing
# from this list simply falls through to "ambiguous, stays visible," same
# as before — this only widens how much of the confident-mismatch case it
# can actually catch.
MAJOR_CITY_COUNTRY: dict[str, str] = {
    "new delhi": "IN", "delhi": "IN", "mumbai": "IN", "bangalore": "IN", "bengaluru": "IN",
    "chennai": "IN", "kolkata": "IN", "hyderabad": "IN", "pune": "IN",
    "las vegas": "US", "new york": "US", "los angeles": "US", "san francisco": "US",
    "chicago": "US", "boston": "US", "miami": "US", "dallas": "US", "houston": "US",
    "atlanta": "US", "seattle": "US", "washington": "US", "orlando": "US", "austin": "US",
    "london": "GB", "manchester": "GB", "birmingham": "GB", "edinburgh": "GB",
    "paris": "FR", "berlin": "DE", "munich": "DE", "frankfurt": "DE", "hamburg": "DE",
    "madrid": "ES", "barcelona": "ES", "rome": "IT", "milan": "IT", "amsterdam": "NL",
    "brussels": "BE", "vienna": "AT", "zurich": "CH", "geneva": "CH",
    "stockholm": "SE", "copenhagen": "DK", "oslo": "NO", "helsinki": "FI",
    "warsaw": "PL", "prague": "CZ", "budapest": "HU", "athens": "GR", "lisbon": "PT",
    "dublin": "IE", "moscow": "RU", "istanbul": "TR",
    "dubai": "AE", "abu dhabi": "AE", "doha": "QA", "riyadh": "SA", "jeddah": "SA",
    "tel aviv": "IL", "jerusalem": "IL", "beirut": "LB", "amman": "JO",
    "karachi": "PK", "lahore": "PK", "islamabad": "PK", "dhaka": "BD", "colombo": "LK",
    "kathmandu": "NP", "hanoi": "VN", "ho chi minh city": "VN", "taipei": "TW",
    "tokyo": "JP", "osaka": "JP", "kyoto": "JP", "seoul": "KR", "beijing": "CN",
    "shanghai": "CN", "shenzhen": "CN", "guangzhou": "CN", "hong kong": "HK",
    "singapore": "SG", "bangkok": "TH", "jakarta": "ID", "manila": "PH",
    "kuala lumpur": "MY", "sydney": "AU", "melbourne": "AU", "auckland": "NZ",
    "wellington": "NZ", "toronto": "CA", "vancouver": "CA", "montreal": "CA",
    "mexico city": "MX", "sao paulo": "BR", "são paulo": "BR", "rio de janeiro": "BR",
    "buenos aires": "AR", "bogota": "CO", "bogotá": "CO", "lima": "PE", "santiago": "CL",
    "cairo": "EG", "casablanca": "MA", "tunis": "TN", "algiers": "DZ",
    "nairobi": "KE", "addis ababa": "ET", "kampala": "UG", "dar es salaam": "TZ",
    "accra": "GH", "dakar": "SN", "luanda": "AO", "kinshasa": "CD", "harare": "ZW",
    "lusaka": "ZM", "gaborone": "BW", "windhoek": "NA", "maputo": "MZ", "kigali": "RW",
    "johannesburg": "ZA", "cape town": "ZA", "durban": "ZA",
    "lagos": "NG", "abuja": "NG", "port harcourt": "NG", "kano": "NG", "ibadan": "NG",
    "benin city": "NG", "kaduna": "NG", "enugu": "NG",
}

# The Brand Playbook's "Region / market" field (select-all-that-apply chips,
# saved as a comma-joined string — see brand-setup/page.tsx) is a free-text
# business fact, not this module's own location_scope — confirmed live as
# the thing relevance_breakdown's geo_fit component actually reads
# (scan_runner.py's _fetch_business_context). Mapped here, not guessed
# per-call, exactly like _NEWS_EDITIONS above: a curated set of real chip
# labels to the countries they mean, nothing invented. "Global /
# International" and "Other" deliberately map to None — "serves everywhere"
# and "unspecified" are both real answers, not a country list to resolve.
REGION_CHIP_COUNTRIES: dict[str, Optional[list[str]]] = {
    "Nigeria": ["NG"],
    "West Africa": ["NG", "GH", "SN", "CI", "ML", "BF", "BJ", "TG", "SL", "LR", "GM", "GN", "GW", "NE", "MR", "CV"],
    "Pan-African": None,  # resolved separately to the full CONTINENTS["AFRICA"] list below
    "United States": ["US"],
    "United Kingdom": ["GB"],
    "Global / International": None,  # serves everywhere — no country list to check against
    "Other": None,  # unspecified — never guessed
}


def resolve_text_country(text: Optional[str]) -> Optional[str]:
    """Best-effort: the first country MAJOR_CITY_COUNTRY or COUNTRY_NAMES
    can confidently place `text` in, or None if it can't be placed.
    Deliberately returns the SAME kind of honest "can't tell" as
    development_location_conflicts above, for the same reason — a missed
    match costs less than a wrong one."""
    if not text:
        return None
    lowered = text.lower()
    # Word-boundary, not a bare substring check — "Niger" is a literal
    # substring of "Nigeria" ("...NIGER-ia"), so a plain `in` match would
    # confidently resolve "somewhere in Nigeria" to the wrong country.
    for city, code in MAJOR_CITY_COUNTRY.items():
        if re.search(rf"\b{re.escape(city)}\b", lowered):
            return code
    for code, name in COUNTRY_NAMES.items():
        name_l = (name or "").lower()
        if code != "GLOBAL" and len(name_l) >= 4 and re.search(rf"\b{re.escape(name_l)}\b", lowered):
            return code
    return None


def region_chips_cover_country(region_text: Optional[str], country: Optional[str]) -> Optional[bool]:
    """True when "Global / International" was selected (serves everywhere
    — any real country is covered) or at least one other selected chip's
    own country list includes `country`; False once a selected chip
    resolves to a real country list that does NOT include it; None when
    nothing selected resolves to any real list at all (only "Other" was
    picked, or `region_text`/`country` is missing) — callers treat None
    as "can't tell," not as a mismatch."""
    if not region_text or not country:
        return None
    chips = [c.strip() for c in region_text.split(",") if c.strip()]
    if "Global / International" in chips:
        return True  # serves everywhere — any real country is covered
    saw_a_real_list = False
    for chip in chips:
        if chip == "Pan-African":
            codes = CONTINENTS.get("AFRICA", {}).get("countries")
        else:
            codes = REGION_CHIP_COUNTRIES.get(chip)
        if codes is None:
            continue
        saw_a_real_list = True
        if country in codes:
            return True
    return False if saw_a_real_list else None


def development_location_conflicts(location_text: Optional[str], expected_country: str) -> bool:
    """True when `location_text` (a Development's own free-text location,
    extracted by an LLM from whatever the source announcement said — see
    development_extractor.py) clearly names a DIFFERENT country than
    `expected_country` — either by naming the country outright, or by
    naming a major city from MAJOR_CITY_COUNTRY above (confirmed live:
    "New Delhi" and "Mandalay Bay, Las Vegas" are bare city/venue names
    with no country word in them at all — the common case, not an edge
    case, for this kind of announcement). Never true for text this can't
    place (no location given, or a venue/city not in either list) — a
    missed match is far less harmful than hiding something that might
    genuinely be relevant, so this only acts on a confident mismatch.

    This is the backstop for what collection-time geo targeting
    (resolve_topic_location_hint, wired into scan_runner.py) doesn't catch
    — either because it was collected before that fix existed, or because
    a source has no real upstream geo lever at all (honestly documented per
    adapter in capabilities().notes). Confirmed live: a Nigeria-scoped
    account's Upcoming Items showed a real New Delhi conference and a Las
    Vegas expo with nothing anywhere checking whether they had anything to
    do with the account's actual market."""
    if not location_text or not expected_country:
        return False
    text = location_text.lower()
    expected = expected_country.upper()
    expected_name = (COUNTRY_NAMES.get(expected) or "").lower()
    if expected_name and expected_name in text:
        return False
    for city, code in MAJOR_CITY_COUNTRY.items():
        if code != expected and city in text:
            return True
    for code, name in COUNTRY_NAMES.items():
        if code == expected or code == "GLOBAL":
            continue
        name_l = (name or "").lower()
        # Minimum length guards against short names (e.g. a 2-3 letter code
        # or a very short country name) matching inside an unrelated word.
        if len(name_l) >= 4 and name_l in text:
            return True
    return False


async def resolve_topic_location_hint(db, brand_id: str) -> Optional[dict]:
    """The real, canonical location hint for a business's OWN topic-based
    scans (scan_runner.py) — reads the same mi_listening_profiles'
    location_scope Settings' Listening location card actually writes to,
    instead of Topic.geographic_scope (a free-text guess made once, at
    topic-creation time, from the Brand Playbook — see brand_topic.py).

    Confirmed live: an account with location_scope correctly set to Nigeria
    still had Upcoming Items showing a New Delhi conference and a Las Vegas
    expo, because nothing in the topic-scan collection path ever consulted
    location_scope at all — Topic.geographic_scope stayed stuck at whatever
    the Playbook's loose "customer_location" text said (often a whole
    continent, e.g. "Africa") and was never enforced as a real filter
    downstream, only used as a soft keyword-prompt hint. This is the same
    `location` shape and mechanism listening/collector.py already uses
    successfully for the shared-market subsystem (see markets.py's
    `_location_hint`) — reused here, not reinvented, for a business's own
    topic scans too.

    Returns None for "global"/"continent" scope or no saved profile at all
    — unchanged, unfiltered collection, same as before this existed.
    Continent-level has no single country to hand an adapter (that's the
    shared-market subsystem's own per-country-explosion job via
    resolve_countries, not a single business's one topic scan)."""
    profile = await db["mi_listening_profiles"].find_one({"brand_id": brand_id})
    if not profile:
        return None
    scope = profile.get("location_scope") or {}
    level = scope.get("level")
    country = (scope.get("country") or "").upper()
    if level not in ("country", "city") or not country or country == "GLOBAL":
        return None
    lang = (COUNTRY_LANGS.get(country) or ["en"])[0]
    hint: dict = {"country": country, "news_edition": _NEWS_EDITIONS.get(country, f"{country}:{lang}")}
    if level == "city" and scope.get("place_id"):
        place = await get_place(db, scope["place_id"])
        if place:
            hint.update({k: v for k, v in (place.get("source_hints") or {}).items() if v is not None})
            hint["place_id"] = place["id"]
    return hint

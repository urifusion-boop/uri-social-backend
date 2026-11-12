"""
Markets and their searches.

A market is a category in a country (key "hair_wigs:NG"). Its searches are
compiled from data, never written by hand:
  • core   — the category's own terms (plus local-language terms)
  • intent — intent phrases × core terms, e.g. ("who sells" OR "where can i buy") (wig OR "frontal wig")
  • learned— terms discovered in useful posts and promoted to monitoring
One market's searches serve every business subscribed to it.

Two query *styles*, picked per source (SOURCE_QUERY_STYLE below):
  • "or_group" — X and Reddit both take real Boolean search syntax, so many
    terms are packed into one short '(a OR "b c" OR d)' string per query —
    few queries, each covering many terms, with intent/problem phrases
    AND-ed in front of the products they go with. This was the module's
    only style while X was the only source.
  • "flat"     — TikTok, Instagram and YouTube's search actors take a plain
    list of distinct terms/hashtags with no AND/OR operators, and
    News/Google-News RSS search is the same — so each cleaned term becomes
    its own query instead of being packed into a group, and intent-phrase ×
    product combination is skipped entirely (there's no way to express
    "(how to use) AND (wig)" as a single hashtag or plain search term; the
    product term alone is what a flat-search actor can actually search for).
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Optional

from . import geo, ontology

MAX_QUERY_CHARS = 380

# Keyed by the adapter's exact ADAPTER_REGISTRY key (scan_runner.py) — both
# the "cheap test" and "pro" variant of a platform share one style, since the
# style follows the platform's search syntax, not which actor runs it.
SOURCE_QUERY_STYLE: dict[str, str] = {
    "x": "or_group", "x_pro": "or_group",
    "reddit": "or_group", "reddit_pro": "or_group",
    "tiktok": "flat", "tiktok_pro": "flat",
    "instagram": "flat", "instagram_pro": "flat",
    "youtube": "flat", "youtube_pro": "flat",
    "news": "flat", "news_pro": "flat",
    "facebook": "flat", "facebook_pro": "flat",
}

# Sourced from geo.py (Phase F) — one registry, not duplicated here. Kept as
# module-level names since callers already import COUNTRY_LANGS/COUNTRY_NAMES
# from this module.
COUNTRY_LANGS = geo.COUNTRY_LANGS
COUNTRY_NAMES = geo.COUNTRY_NAMES


def market_key(category_id: str, country: str) -> str:
    return f"{category_id}:{(country or 'GLOBAL').upper()}"


def _clean(terms: list[str]) -> list[str]:
    out: list[str] = []
    for t in terms or []:
        t = " ".join(str(t).replace('"', " ").replace("(", " ").replace(")", " ").split()).lower()
        if t and t not in out:
            out.append(t)
    return out


def _q(term: str) -> str:
    return f'"{term}"' if " " in term else term


def or_groups(terms: list[str], prefix: str = "", limit: int = MAX_QUERY_CHARS) -> list[str]:
    """Pack terms into '(a OR "b c" OR d)' groups; an optional prefix group is
    AND-ed in front of each (used for intent × product)."""
    out, cur = [], []
    head = f"{prefix} " if prefix else ""
    for t in _clean(terms):
        trial = cur + [_q(t)]
        if cur and len(head + "(" + " OR ".join(trial) + ")") > limit:
            out.append(head + "(" + " OR ".join(cur) + ")")
            cur = [_q(t)]
        else:
            cur = trial
    if cur:
        out.append(head + "(" + " OR ".join(cur) + ")")
    return out


def query_id(q: str) -> str:
    return hashlib.sha1(q.encode("utf-8")).hexdigest()[:12]


def flat_terms(terms: list[str]) -> list[str]:
    """One query string per cleaned term — the 'flat' style's equivalent of
    or_groups(), for sources whose search takes plain terms/hashtags with no
    AND/OR operators to pack into a group."""
    return _clean(terms)


CITY_QUERIES_PER_PLACE = 3  # small, fixed — a deliberate add-on per requested place, not scaled by max_queries_per_market


def _location_hint(country: str, place: Optional[dict] = None) -> Optional[dict]:
    """What collector.py hands the adapter as `location` (Phase F). A plain
    country code is always safe to pass, even for a market's base queries —
    it only makes a market's OWN already-intended country scoping real
    upstream (e.g. TikTok's `location` field) instead of a no-op label; it
    never narrows anything a subscriber wasn't already expecting, since the
    market itself is already `category:country`. GLOBAL means no filter."""
    if (country or "GLOBAL").upper() == "GLOBAL":
        return None
    code = country.upper()
    lang = (COUNTRY_LANGS.get(code) or ["en"])[0]
    # News editions are genuinely country-native (no "known place" needed) —
    # default from the market's own country so every country-scoped market
    # gets this lever, not only ones with a requested city.
    hint: dict = {"country": code, "news_edition": f"{code}:{lang}"}
    if place:
        hint.update({k: v for k, v in (place.get("source_hints") or {}).items() if v is not None})
        hint["place_id"] = place.get("id")
    return hint


def compile_queries(category: dict, country: str, patterns: dict, learned: list[str],
                    max_queries: int, source: str = "x", places: Optional[list[dict]] = None) -> dict:
    """The searches for one market run, in priority order, plus exclusions.

    role "measure": intent/problem phrases × products — narrow enough to read
    completely, so they alone decide coverage and feed trend counts. Only
    produced for "or_group"-style sources (see module docstring); a flat
    search has no way to express an intent phrase AND-ed with a product.
    role "sample": broad category and learned terms — far too busy to read
    completely; used to discover topics and wording, never to count trends.

    `places` (Phase F): known-place docs at least one subscriber has asked
    for inside this market's country (markets.py's `city_requests`). Each
    gets its own small, ADDITIONAL query set on top of the base ones above —
    never replacing or narrowing them, so subscribers who only want the
    whole country see no change. Capped at CITY_QUERIES_PER_PLACE per place,
    outside `max_queries` (a deliberate per-request add-on, not scaled by
    the market's own query budget)."""
    style = SOURCE_QUERY_STYLE.get(source, "or_group")
    langs = COUNTRY_LANGS.get((country or "").upper(), ["en"])
    core = _clean(category.get("core_terms") or [])
    local = []
    for lang in langs:
        local += (category.get("local_terms") or {}).get(lang, [])
    queries: list[dict] = []

    if style == "or_group":
        # Intent × the most central product terms: buying, recommendation and
        # problem talk is the most commercially useful, so it goes first.
        head_terms = core[:6]
        for lang in langs:
            lp = patterns.get(lang) or {}
            intents = _clean((lp.get("purchase") or [])[:5] + (lp.get("recommendation") or [])[:3])
            problems = _clean((lp.get("problem") or [])[:4] + (lp.get("how_to") or [])[:2])
            for label, phrases in (("intent", intents), ("problem", problems)):
                if not phrases or not head_terms:
                    continue
                prefix = "(" + " OR ".join(_q(p) for p in phrases) + ")"
                for g in or_groups(head_terms, prefix=prefix):
                    queries.append({"kind": f"{label}:{lang}", "q": g, "role": "measure"})

        for g in or_groups(core + _clean(local)):
            queries.append({"kind": "core", "q": g, "role": "sample"})
        for g in or_groups(learned):
            queries.append({"kind": "learned", "q": g, "role": "sample"})
    else:
        # Flat: every cleaned core/local/learned term is its own query —
        # no intent-phrase step (nothing to AND it onto), no OR-packing
        # (the actor's own keyword/hashtag list already covers many terms
        # per run without needing them combined into one query string).
        for t in flat_terms(core + local):
            queries.append({"kind": "core", "q": t, "role": "sample"})
        for t in flat_terms(learned):
            queries.append({"kind": "learned", "q": t, "role": "sample"})

    base_hint = _location_hint(country)
    for q in queries:
        q["id"] = query_id(q["q"])
        q["location_hint"] = base_hint
    kept = queries[:max_queries]

    city_queries: list[dict] = []
    for place in (places or []):
        place_hint = _location_hint(country, place)
        terms = core + _clean(local)
        geo_qs = or_groups(terms[:6]) if style == "or_group" else flat_terms(terms)
        for g in geo_qs[:CITY_QUERIES_PER_PLACE]:
            city_queries.append({"kind": f"geo:{place['id']}", "q": g, "role": "sample",
                                 "id": query_id(f"{place['id']}:{g}"), "location_hint": place_hint})

    return {"queries": kept + city_queries, "dropped": max(0, len(queries) - max_queries),
            "excluded": _clean(category.get("excluded") or [])[:10], "languages": langs}


async def ensure_market(db, category_id: str, country: str, label: Optional[str] = None,
                        pilot: bool = False) -> dict:
    key = market_key(category_id, country)
    now = datetime.utcnow()
    existing = await db["mi_markets"].find_one({"key": key})
    if existing:
        if pilot and not existing.get("pilot"):
            await db["mi_markets"].update_one({"key": key}, {"$set": {"pilot": True}})
        existing.pop("_id", None)
        return existing
    cat = await ontology.category(db, category_id) or {}
    doc = {
        "key": key, "category_id": category_id, "country": (country or "GLOBAL").upper(),
        "label": f"{label or cat.get('label') or category_id} · {COUNTRY_NAMES.get((country or 'GLOBAL').upper(), country)}",
        "status": "active", "pilot": pilot, "subscribers": [], "learned_terms": [],
        "created_at": now, "last_collected_at": None,
    }
    await db["mi_markets"].insert_one(dict(doc))
    return doc


async def subscribe(db, key: str, brand_id: str) -> None:
    await db["mi_markets"].update_one({"key": key}, {"$addToSet": {"subscribers": brand_id}})


async def unsubscribe_others(db, brand_id: str, keep: list[str]) -> None:
    await db["mi_markets"].update_many({"subscribers": brand_id, "key": {"$nin": keep}},
                                       {"$pull": {"subscribers": brand_id}})


async def request_place(db, key: str, place_id: str, brand_id: str) -> None:
    """One subscriber asking this market's collection to also cover a
    specific city/region inside it (Phase F) — additive, never replaces the
    market's own country-wide collection for other subscribers. Stored as
    `city_requests.{place_id}: [brand_id, ...]` so `collect_market` knows
    which known places to build extra geo-scoped queries for."""
    await db["mi_markets"].update_one({"key": key}, {"$addToSet": {f"city_requests.{place_id}": brand_id}})


async def unrequest_place(db, key: str, place_id: str, brand_id: str) -> None:
    await db["mi_markets"].update_one({"key": key}, {"$pull": {f"city_requests.{place_id}": brand_id}})


async def unrequest_places_elsewhere(db, brand_id: str, keep_place_ids: list[str]) -> None:
    """Mirrors unsubscribe_others, for place requests: pulls this brand out
    of every market's city_requests for a place not in `keep_place_ids`."""
    async for m in db["mi_markets"].find({f"city_requests": {"$exists": True}}):
        for place_id in list((m.get("city_requests") or {}).keys()):
            if place_id not in keep_place_ids and brand_id in (m["city_requests"].get(place_id) or []):
                await unrequest_place(db, m["key"], place_id, brand_id)


def requested_places(market: dict) -> list[str]:
    return [pid for pid, brands in (market.get("city_requests") or {}).items() if brands]


async def due_markets(db, cfg: dict, now: Optional[datetime] = None, only: Optional[list[str]] = None) -> list[dict]:
    """Markets with a subscriber (or marked pilot) whose last collection is
    older than their cadence — a market's own cadence_hours (set by
    update_cadence, PRD §7.3) if it has one, else the configured default."""
    from datetime import timedelta
    now = now or datetime.utcnow()
    q: dict[str, Any] = {"status": "active", "$or": [{"subscribers.0": {"$exists": True}}, {"pilot": True}]}
    if only:
        q = {"key": {"$in": only}}
    out = []
    async for m in db["mi_markets"].find(q):
        m.pop("_id", None)
        last = m.get("last_collected_at")
        hours = m.get("cadence_hours") or cfg["cadence_hours"]
        if only or not last or now - last >= timedelta(hours=hours):
            out.append(m)
    return out


async def update_cadence(db, market: dict, cfg: dict, now: Optional[datetime] = None,
                         tier_floor_hours: Optional[int] = None) -> int:
    """Promote a market to faster collection while it has an active hot
    signal (rising/spike), cool it back down to the default once it
    doesn't — PRD §7.3: "Hot topics are automatically promoted to faster
    cadence and cooled down when momentum fades." Returns the resolved
    cadence in hours.

    tier_floor_hours (PRD §6.5, §21 open decision #8): the fastest cadence
    any of this market's current subscribers' plan tiers calls for, resolved
    by the caller (cycle.py — it already has every subscriber's profile
    loaded for the cycle, so this module doesn't need its own plan-tier
    lookups). "a query serving 2,000 wig sellers is collected once, on the
    schedule its most demanding subscriber's plan allows" — one shared
    cadence per market either way (collect once, serve many stays true),
    just never slower than its fastest-tier subscriber needs."""
    c = cfg["cadence"]
    now = now or datetime.utcnow()
    hot = await db["mi_listen_signals"].count_documents({
        "market": market["key"], "status": "active", "expires_at": {"$gt": now},
        "type": {"$in": c["hot_signal_types"]},
    })
    hours = c["hot_hours"] if hot else cfg["cadence_hours"]
    if tier_floor_hours is not None:
        hours = min(hours, tier_floor_hours)
    await db["mi_markets"].update_one({"key": market["key"]}, {"$set": {"cadence_hours": hours}})
    return hours

"""
Jane + Ads — turning a pin into a location Meta will show by NAME.

geo.py resolves every targeted pocket to coordinates. Sent to Meta as they are, they
become `custom_locations` (lat/lng + radius), and Meta has no name for an arbitrary
coordinate, so Ads Manager renders the ad set's location as "(6.6018, 3.3515) + 3 km".
Neither we nor a client opening Ads Manager can tell whether that is Ikeja or a car
park, which makes the one part of the ad a client most wants to check — where their
money is being spent — unreadable. So we never send one.

Meta's `adgeolocation` search returns typed KEYS (city / neighborhood / subcity /
region) which, sent as `geo_locations.cities` etc., render with their real names.
This module resolves a pin's name to such a key, and a campaign's city or state to a
broader key for the fallback.

Two rules, both learned from what the search actually returns:

**A match must be in the right region.** Searching "Yaba" (a Lagos district) returns
"Yaba, Katsina State" as its top hit — a different place ~700km away. Accepting that
blindly would move a Lagos campaign's budget to northern Nigeria and still look
correct in the UI. So a candidate is only accepted when its region matches the
campaign's, and anything unmatched is discarded rather than guessed at.

**A location we cannot name is not targeted at all.** Our gazetteer carries pockets
Meta has no key for — "Computer Village", "Admiralty Way" — and those used to be sent
as raw coordinates. They are now dropped instead: a client opening Ads Manager must
never be shown "(6.5960, 3.3420) + 1.5 km", because a coordinate is unverifiable to
them and to us, and unverifiable is worse than broader. meta_targeting_from_geo_named
falls back to the campaign's own city or state — both named — when no pocket resolves.

Everything here fails open: any error, timeout, or unconvincing match returns None.
The caller then widens to a named area rather than narrowing to a coordinate.
"""
from __future__ import annotations

import json
from typing import Optional

import httpx

from app.core.config import settings

# Meta location types that carry a name and accept a radius, narrowest first — a
# neighbourhood is a better match for "Lekki" than the city that contains it.
_SEARCH_TYPES = ["neighborhood", "subcity", "city"]

# Which geo_locations field each type goes in. Meta rejects a key filed under the
# wrong one, so this mapping is part of the contract, not a convenience.
_FIELD_FOR_TYPE = {
    "neighborhood": "neighborhoods",
    "subcity": "subcities",
    "city": "cities",
    "region": "regions",
}

# Resolved names are stable, so one process-lifetime cache keeps a multi-pin plan to
# a handful of calls instead of one per pin per launch.
_cache: dict[tuple[str, str], Optional[dict]] = {}


def _norm(value: str) -> str:
    return "".join(ch for ch in (value or "").lower() if ch.isalnum() or ch == " ").strip()


def _region_matches(hit_region: str, expected_region: str) -> bool:
    """Whether a search hit is in the region we're actually advertising in.

    Lenient on wording ("Lagos" vs "Lagos State") but strict on identity — this is the
    check that stops a Lagos ad silently targeting Yaba, Katsina State.
    """
    a, b = _norm(hit_region), _norm(expected_region)
    if not a or not b:
        return False
    a = a.removesuffix(" state")
    b = b.removesuffix(" state")
    return a == b or a.startswith(b) or b.startswith(a)


async def resolve_named_location(
    name: str, expected_region: str, access_token: str = "", timeout: float = 8.0
) -> Optional[dict]:
    """Meta's named key for `name`, or None if it cannot be named confidently.

    Returns {"type": ..., "key": ..., "name": ..., "region": ...} on a confident,
    region-verified match.
    """
    name = (name or "").strip()
    if not name or not expected_region:
        return None
    token = access_token or settings.META_ADS_ACCESS_TOKEN
    if not token:
        return None

    cache_key = (_norm(name), _norm(expected_region))
    if cache_key in _cache:
        return _cache[cache_key]

    graph = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{graph}/search",
                params={
                    "type": "adgeolocation",
                    "q": name,
                    "country_code": "NG",
                    "location_types": json.dumps(_SEARCH_TYPES),
                    "limit": 10,
                    "access_token": token,
                },
            )
        hits = (resp.json() or {}).get("data") or []
    except Exception as e:
        print(f"[GeoNames] lookup failed for {name!r}: {e}", flush=True)
        _cache[cache_key] = None
        return None

    wanted = _norm(name)
    best = None
    for hit in hits:
        if hit.get("type") not in _FIELD_FOR_TYPE:
            continue
        if not _region_matches(hit.get("region", ""), expected_region):
            continue
        hit_name = _norm(hit.get("name", ""))
        # Exact name wins outright. Otherwise Meta's name must CONTAIN what we asked
        # for ("Lekki" -> "Lekki Peninsula"), never the reverse: "Lagos" contains
        # "Lekki" in no useful sense, and accepting a shorter, broader name is how a
        # neighbourhood target silently becomes a whole city.
        if hit_name == wanted:
            best = hit
            break
        if wanted and wanted in hit_name and best is None:
            best = hit

    result = None
    if best:
        result = {"type": best["type"], "key": best["key"],
                  "name": best.get("name", ""), "region": best.get("region", "")}
    _cache[cache_key] = result
    return result


def field_for_type(location_type: str) -> str:
    return _FIELD_FOR_TYPE.get(location_type, "cities")


async def resolve_region(
    region_name: str, access_token: str = "", timeout: float = 8.0
) -> Optional[dict]:
    """Meta's `region` key for a state — the last named fallback before the country.

    Only reached when no pocket in the plan could be named. Broader than anything the
    planner chose, and deliberately so: a named state is auditable in Ads Manager
    where a coordinate is not, and being able to read where the money went matters
    more than a tighter box nobody can verify.
    """
    region_name = (region_name or "").strip()
    if not region_name:
        return None
    token = access_token or settings.META_ADS_ACCESS_TOKEN
    if not token:
        return None

    cache_key = ("__region__", _norm(region_name))
    if cache_key in _cache:
        return _cache[cache_key]

    graph = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{graph}/search",
                params={
                    "type": "adgeolocation",
                    "q": region_name,
                    "country_code": "NG",
                    "location_types": json.dumps(["region"]),
                    "limit": 10,
                    "access_token": token,
                },
            )
        hits = (resp.json() or {}).get("data") or []
    except Exception as e:
        print(f"[GeoNames] region lookup failed for {region_name!r}: {e}", flush=True)
        _cache[cache_key] = None
        return None

    wanted = _norm(region_name).removesuffix(" state")
    result = None
    for hit in hits:
        if hit.get("type") != "region":
            continue
        if _norm(hit.get("name", "")).removesuffix(" state") == wanted:
            result = {"type": "region", "key": hit["key"],
                      "name": hit.get("name", ""), "region": hit.get("name", "")}
            break
    _cache[cache_key] = result
    return result


async def region_for(place: str, access_token: str = "", timeout: float = 8.0) -> str:
    """The STATE a place sits in — what the region guard actually needs.

    Callers naturally pass the campaign's city ("Ikeja"), but Meta reports every hit's
    region as the state ("Lagos State"), so comparing pockets against a city rejected
    everything. Live-caught: Opebi and Alausa are both real, targetable Meta locations
    that were thrown away because the guard was asked to match them against "Ikeja",
    and the campaign silently fell back to all of Nigeria.

    Returns "" when nothing resolves, and the caller then matches on the place itself —
    which is the old behaviour, and correct when the place IS a state ("Lagos").
    """
    place = (place or "").strip()
    if not place:
        return ""
    cache_key = ("__regionfor__", _norm(place))
    if cache_key in _cache:
        return (_cache[cache_key] or {}).get("name", "")

    # A place that IS a state needs no lookup.
    as_region = await resolve_region(place, access_token, timeout)
    if as_region:
        _cache[cache_key] = as_region
        return as_region["name"]

    token = access_token or settings.META_ADS_ACCESS_TOKEN
    if not token:
        return ""
    graph = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(
                f"{graph}/search",
                params={"type": "adgeolocation", "q": place, "country_code": "NG",
                        "location_types": json.dumps(_SEARCH_TYPES), "limit": 10,
                        "access_token": token},
            )
        hits = (resp.json() or {}).get("data") or []
    except Exception as e:
        print(f"[GeoNames] region_for failed for {place!r}: {e}", flush=True)
        _cache[cache_key] = None
        return ""

    wanted = _norm(place)
    for hit in hits:
        # Exact name only. A fuzzy match here would pick the wrong state, which is the
        # very failure (Yaba -> Katsina) the guard exists to prevent.
        if _norm(hit.get("name", "")) == wanted and hit.get("region"):
            result = {"name": hit["region"]}
            _cache[cache_key] = result
            return hit["region"]
    _cache[cache_key] = None
    return ""

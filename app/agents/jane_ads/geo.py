"""
Jane + Ads — watering-hole / pin-and-pocket geo targeting.

Extends the geography layer of the decision logic. The suburb is the container; the
NAMED micro-locations inside it are the real targets. A tight pin concentrates a small
budget on the few thousand people who matter.

Source roles (the whole game — "confidently wrong" is worse than "admits uncertainty"):
  • AI reasoning  → PROPOSE what kind of place fits (judgment). Never trusted for coords.
  • Geocoding     → LOCATE & VALIDATE the pin (real map service). Stops hallucination.
  • Web search    → CHECK freshness when uncertain (not wired here yet).
  • Performance   → the MOAT: which pins actually converted (learned over time).

Golden rule: NEVER pin a location we can't validate. If geocoding can't confirm it,
fall back to a broader known-good area and say so.

Pure orchestration + injectable providers → the static providers make it fully
unit-testable now; the LLM proposer and Google geocoder are the production impls.
"""
from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import Optional

import aiohttp
import openai

from app.core.config import settings

from .models import GeoMode, GeoPin, GeoPlan, Goal, PinSource, PurchaseBehaviour

# ── Mode decision (PULL vs GO-FIND) ───────────────────────────────────────────
# Businesses that pull customers to a location default to own-radius; the ones that
# must go find customers where they gather are watering-hole.
_WATERING_HOLE_KEYWORDS = {
    "real", "estate", "realtor", "property", "luxury", "premium", "travel", "tour",
    "b2b", "supplier", "wholesale", "distributor", "gallery", "developer", "broker",
}


def decide_geo_mode(category: str, description: str = "") -> GeoMode:
    text = f"{category} {description}".lower()
    words = set(text.replace(",", " ").replace("/", " ").split())
    if words & _WATERING_HOLE_KEYWORDS:
        return GeoMode.WATERING_HOLE
    return GeoMode.OWN_RADIUS


# ── Geocoding (LOCATE & VALIDATE) ─────────────────────────────────────────────

class Geocoder(ABC):
    @abstractmethod
    async def geocode(self, place: str, city: str) -> Optional[tuple[float, float, float]]:
        """Return (lat, lng, radius_km) for a real place, or None if it can't be
        confirmed. None is the signal that stops an imaginary pin."""
        ...


# A seed of real Lagos micro-locations (the offline moat + test double). radius_km is
# tuned per place: small self-contained estates get tight radii; commercial axes a bit
# wider to respect the ~1km platform minimum.
_LAGOS_PLACES: dict[str, tuple[float, float, float]] = {
    "banana island":        (6.4269, 3.4460, 1.5),
    "dolphin estate":       (6.4780, 3.4020, 1.5),
    "bode thomas":          (6.4990, 3.3620, 1.5),
    "adeniran ogunsanya":   (6.4930, 3.3560, 1.5),
    "victoria island":      (6.4281, 3.4219, 3.0),
    "vi":                   (6.4281, 3.4219, 3.0),
    "ikoyi":                (6.4520, 3.4340, 2.5),
    "lekki":                (6.4470, 3.4700, 3.0),
    "lekki phase 1":        (6.4440, 3.4780, 2.0),
    "admiralty way":        (6.4430, 3.4770, 1.5),
    "surulere":             (6.4990, 3.3540, 3.0),
    "ikeja":                (6.6018, 3.3515, 3.0),
    "computer village":     (6.5960, 3.3420, 1.5),
    "yaba":                 (6.5150, 3.3710, 2.5),
    "oshodi":               (6.5550, 3.3480, 2.5),
    "gbagada":              (6.5450, 3.3900, 2.5),
    "maryland":             (6.5710, 3.3670, 2.0),
}


class StaticGeocoder(Geocoder):
    """Offline geocoder over a curated Lagos gazetteer. Serves tests AND acts as the
    real fallback when no Google key is configured — it still knows the major pockets."""

    def __init__(self, places: Optional[dict[str, tuple[float, float, float]]] = None) -> None:
        self._places = {k.lower(): v for k, v in (places or _LAGOS_PLACES).items()}

    async def geocode(self, place: str, city: str) -> Optional[tuple[float, float, float]]:
        key = place.strip().lower()
        if key in self._places:
            return self._places[key]
        # Loose contains-match so "the Adeniran Ogunsanya axis" still resolves.
        for name, coord in self._places.items():
            if name in key or key in name:
                return coord
        return None


# ── Pin proposal (AI PROPOSES the place) ──────────────────────────────────────

class PinProposer(ABC):
    @abstractmethod
    async def propose(self, business_name: str, category: str, city: str,
                      mode: GeoMode, goal: Goal) -> list[tuple[str, str]]:
        """Return [(place_name, reason), …] — the KIND of pockets that fit. Names only;
        coordinates are the geocoder's job."""
        ...


class StaticPinProposer(PinProposer):
    """Deterministic proposer for tests — returns a fixed list."""

    def __init__(self, proposals: list[tuple[str, str]]) -> None:
        self._proposals = proposals

    async def propose(self, business_name, category, city, mode, goal):
        return list(self._proposals)


class LLMPinProposer(PinProposer):
    """Production proposer — the model PROPOSES named pockets (judgment). It is told to
    name real, well-known places only; the geocoder still validates every one, so an
    invented street is dropped rather than pinned."""

    async def propose(self, business_name, category, city, mode, goal):
        if not settings.OPENAI_API_KEY or not city:
            return []
        mode_hint = ("Find the PLACES this kind of customer gathers (offices, estates, "
                     "malls, hubs)." if mode == GeoMode.WATERING_HOLE
                     else "Find the specific commercial streets/pockets in the area where "
                          "buyers actually concentrate (not residential interiors).")
        prompt = (
            f"You are a Lagos media buyer choosing ad targeting for '{business_name}' "
            f"(a {category or 'local business'}) in {city}.\n{mode_hint}\n"
            "Name 2–4 SPECIFIC, REAL, well-known micro-locations (named streets, estates, "
            "or pockets) inside the area — never invent a place; only name ones you are "
            "confident exist. For each give a short reason.\n"
            'Return JSON: {"pins":[{"name":"...","reason":"..."}]}'
        )
        try:
            client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
            resp = await client.chat.completions.create(
                model="gpt-4o-mini",
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}],
                timeout=15,
            )
            data = json.loads(resp.choices[0].message.content or "{}")
            return [(p.get("name", ""), p.get("reason", "")) for p in data.get("pins", []) if p.get("name")]
        except Exception as e:
            print(f"[Geo] LLM proposer error: {e}", flush=True)
            return []


class GoogleGeocoder(Geocoder):
    """Production geocoder — validates a named place to real coordinates via the Google
    Geocoding API. Returns None if Google can't confirm it (→ the pin is dropped)."""

    def __init__(self, radius_km: float = 2.0) -> None:
        self._key = os.getenv("GOOGLE_API_KEY", "")
        self._radius_km = radius_km

    async def geocode(self, place: str, city: str) -> Optional[tuple[float, float, float]]:
        if not self._key:
            return None
        address = f"{place}, {city}" if city and city.lower() not in place.lower() else place
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://maps.googleapis.com/maps/api/geocode/json",
                    params={"address": address, "key": self._key},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    data = await r.json()
            if data.get("status") == "OK" and data.get("results"):
                loc = data["results"][0]["geometry"]["location"]
                return (loc["lat"], loc["lng"], self._radius_km)
        except Exception as e:
            print(f"[Geo] Google geocode error: {e}", flush=True)
        return None


class CompositeGeocoder(Geocoder):
    """Try Google first (freshest), then the Lagos gazetteer. Guarantees major pockets
    resolve even without a Google key, and upgrades precision when the key works."""

    def __init__(self) -> None:
        self._google = GoogleGeocoder()
        self._static = StaticGeocoder()

    async def geocode(self, place: str, city: str) -> Optional[tuple[float, float, float]]:
        return await self._google.geocode(place, city) or await self._static.geocode(place, city)


# Offline fallback knowledge: commercial pockets per area, and wealth pockets for
# watering-hole/luxury. Lets Jane propose real pins even when the LLM is unavailable.
_COMMERCIAL_POCKETS: dict[str, list[tuple[str, str]]] = {
    "surulere": [("Bode Thomas", "commercial street where offices concentrate"),
                 ("Adeniran Ogunsanya", "high foot traffic and offices")],
    "ikeja":    [("Computer Village", "the commercial/tech hub of Ikeja")],
    "victoria island": [("Admiralty Way", "premium retail and corporate strip")],
    "vi":       [("Admiralty Way", "premium retail and corporate strip")],
    "lekki":    [("Admiralty Way", "commercial strip in Lekki Phase 1")],
    "yaba":     [("Yaba", "commercial and student hub")],
}
_WEALTH_POCKETS: list[tuple[str, str]] = [
    ("Banana Island", "where the wealth lives"),
    ("Dolphin Estate", "affluent residential pocket"),
    ("Victoria Island", "where high-value buyers work"),
]


class HeuristicPinProposer(PinProposer):
    """Rule-based proposer over the gazetteer — used when the LLM is unavailable so the
    pin-and-pocket concept still works offline. Every name still passes the geocoder."""

    async def propose(self, business_name, category, city, mode, goal):
        if mode == GeoMode.WATERING_HOLE:
            return list(_WEALTH_POCKETS)
        pockets = _COMMERCIAL_POCKETS.get((city or "").strip().lower())
        if pockets:
            return list(pockets)
        return [(city, "targeting the area broadly")] if city else []


class ChainedPinProposer(PinProposer):
    """Try each proposer until one returns names — LLM first, heuristic as fallback."""

    def __init__(self, *proposers: PinProposer) -> None:
        self._proposers = proposers

    async def propose(self, business_name, category, city, mode, goal):
        for p in self._proposers:
            out = await p.propose(business_name, category, city, mode, goal)
            if out:
                return out
        return []


def default_providers() -> tuple[PinProposer, Geocoder]:
    """Production wiring: LLM proposes (heuristic fallback), Google-then-gazetteer validates."""
    return ChainedPinProposer(LLMPinProposer(), HeuristicPinProposer()), CompositeGeocoder()


async def geo_for_request(business_name: str, category: str, city: str,
                          goal: Goal = Goal.MESSAGES, description: str = "") -> GeoPlan:
    """Convenience: LLM proposes; if its names don't validate, fall back to the
    known-good gazetteer heuristic; Google-then-gazetteer geocodes."""
    return await build_geo_plan(
        business_name, category, city,
        proposer=LLMPinProposer(), geocoder=CompositeGeocoder(),
        goal=goal, description=description,
        fallback_proposer=HeuristicPinProposer(),
    )


# ── Build the geo plan ────────────────────────────────────────────────────────

async def build_geo_plan(
    business_name: str,
    category: str,
    city: str,
    proposer: PinProposer,
    geocoder: Geocoder,
    goal: Goal = Goal.MESSAGES,
    description: str = "",
    max_pins: int = 4,
    fallback_proposer: Optional[PinProposer] = None,
) -> GeoPlan:
    """AI proposes named pockets → geocoder validates each → keep only validated pins.
    If the primary proposer's names don't validate, try the fallback proposer's
    known-good pins before giving up. If NOTHING validates, fall back to a broad area
    and say so — never pin an unvalidated place."""
    mode = decide_geo_mode(category, description)

    async def _validate(prop: PinProposer) -> list[GeoPin]:
        proposals = await prop.propose(business_name, category, city, mode, goal)
        out: list[GeoPin] = []
        for name, reason in proposals[:max_pins]:
            coord = await geocoder.geocode(name, city)
            if coord is None:
                continue   # NEVER pin an unvalidated place
            lat, lng, radius_km = coord
            out.append(GeoPin(name=name, lat=lat, lng=lng, radius_km=radius_km,
                              source=PinSource.GEOCODED, reason=reason))
        return out

    pins = await _validate(proposer)
    if not pins and fallback_proposer is not None:
        pins = await _validate(fallback_proposer)   # known-good gazetteer pins

    if not pins:
        # Nothing validated → broad, honest fallback to the city itself.
        fallback = city or "the wider area"
        return GeoPlan(
            mode=mode, city=city, pins=[], fallback_area=fallback,
            explanation=(f"I couldn't confirm specific streets, so I'm targeting {fallback} "
                         f"broadly rather than guess a location that might not exist."),
        )

    return GeoPlan(mode=mode, city=city, pins=pins,
                   explanation=_explain(mode, city, pins))


def _explain(mode: GeoMode, city: str, pins: list[GeoPin]) -> str:
    names = ", ".join(p.name for p in pins)
    where = "where your customers gather" if mode == GeoMode.WATERING_HOLE \
        else "around your area"
    lead = pins[0].reason or f"that's {where}"
    return (f"I'm pinning {names} in {city} — {lead}. "
            f"Tight pins like these concentrate your budget on the people who matter "
            f"instead of the whole {city}.")

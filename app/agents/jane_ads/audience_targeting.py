"""
Jane + Ads — turning Jane's audience call into what Meta's ad set actually accepts.

Live-reported: the real ad set always shipped broad (all ages, all genders, no
interests) no matter what Jane's audience reasoning said — `PlanVariant.audience_segment`
("small businesses launching their first online campaign") and the brand's own
`target_audience` are free text, shown in the plan card and used to steer the
CREATIVE, but nothing ever translated them into Meta's age_min/age_max/genders/
flexible_spec fields. This module is that translation.

Two steps, because Meta's targeting fields are structured and audience_segment isn't:
1. An LLM call extracts an age range, gender (if the text actually implies one —
   most audience descriptions don't), and a short list of INTEREST KEYWORDS a media
   buyer would search Meta's own targeting tool for (not the raw sentence — "small
   businesses launching their first online campaign" isn't a valid interest, but
   "Small business" and "Digital marketing" are real, searchable ones).
2. Each keyword is resolved against Meta's own targeting-search endpoint
   (`GET /search?type=adinterest`), which is the ONLY reliable way to get a real
   interest id — Meta rejects an invented id outright, and interest names/ids
   change over time, so nothing here is hardcoded.

Best-effort throughout, like plan_variants.py's variant generation: unresolvable
text, no interests found, or the AI/Graph API being unreachable all just leave the
ad set on its existing geo-only targeting — broad-on-this-axis is always a valid,
launchable ad, so a failure here must never block the build.
"""
from __future__ import annotations

import json
import re
from typing import Optional

import httpx
import openai

from app.core.config import settings

_MAX_INTERESTS = 5
# Meta's own floor for any ads audience; also keeps a stray "13" from the model
# (a plausible-sounding minimum a human might type, but usually meaning "everyone
# old enough to buy this") from narrowing an ad only the youngest end wants.
_MIN_AGE = 18
_MAX_AGE = 65
_GENDER_CODES = {"male": [1], "female": [2]}  # "all"/anything else → omit the key


def _extraction_prompt(audience_text: str) -> str:
    return (
        "A media buyer described their target audience in their own words:\n"
        f'"{audience_text}"\n\n'
        "Turn this into Meta Ads targeting parameters. Return ONLY JSON:\n"
        "{\n"
        '  "age_min": <18-65, or null if the text implies no age skew>,\n'
        '  "age_max": <18-65, or null if the text implies no age skew>,\n'
        '  "gender": "male" | "female" | "all",\n'
        '  "interest_keywords": [<0-5 short phrases you would type into Meta\'s own\n'
        "     interest-targeting search box to reach this audience — real, searchable\n"
        "     interest/industry/behaviour terms, e.g. \"Small business\", \"Online\n"
        "     shopping\", \"Skincare\" — never a restatement of the sentence itself>]\n"
        "}\n\n"
        "Most audience descriptions ('small businesses launching their first online "
        "campaign', 'homeowners in new estates') imply NO age or gender skew — leave "
        "those null/\"all\" unless the text is explicit ('young professionals', "
        "'mothers', 'men's grooming'). Prefer fewer, more precise keywords over five "
        "vague ones; an empty list is correct if nothing in the text names a real "
        "interest category.\n\n"
        "Every keyword must come from WHAT THIS AUDIENCE DOES, SELLS, OR BUYS — their "
        "trade, industry, or the category of thing they'd purchase. Never derive one "
        "from an age, a generation, or a guess at what people that age enjoy: the age "
        "range is already carried by age_min/age_max above, and restating it as an "
        "interest just adds people nothing like the audience. For 'gym owners' the "
        "keywords are about gyms and fitness businesses."
        # NOTE: this deliberately no longer names a specific bad interest. An earlier
        # version cited "Hip-hop music" as the thing to avoid, which did not help —
        # the wrong interest was coming from META's search ranking, not from this
        # model (see _resolve_interest), so the prompt was the wrong layer to fix it.
    )


async def _extract_hints(audience_text: str) -> dict:
    client = openai.AsyncOpenAI(api_key=settings.jane_ads_openai_key)
    resp = await client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": _extraction_prompt(audience_text)}],
        timeout=20,
    )
    return json.loads(resp.choices[0].message.content or "{}")


_PAREN_SUFFIX = re.compile(r"\s*\([^)]*\)\s*$")
_WORD = re.compile(r"[a-z0-9]+")


def _match_score(keyword: str, hit_name: str) -> int:
    """How well a Meta interest actually matches the keyword we searched for.

    3 = the same thing, 2 = contains every word we asked for, 1 = partial overlap,
    0 = unrelated, so reject it. Meta puts its own category in a trailing
    parenthetical ("Health club (fitness)"), which is not part of the name for
    matching purposes.
    """
    base = _PAREN_SUFFIX.sub("", hit_name or "").strip().lower()
    q = (keyword or "").strip().lower()
    if not base or not q:
        return 0
    if base == q:
        return 3
    q_words, base_words = set(_WORD.findall(q)), set(_WORD.findall(base))
    if not q_words or not base_words:
        return 0
    if q_words <= base_words:
        return 2
    return 1 if q_words & base_words else 0


async def _resolve_interest(client: httpx.AsyncClient, graph_base: str,
                             access_token: str, keyword: str) -> Optional[dict]:
    """Meta's own targeting-search result for one keyword — an invented interest id
    is rejected outright at ad-set creation, so this is the only reliable source of
    real ids.

    Meta's own ranking cannot be trusted blindly, though. Live-confirmed: searching
    "Gym" with limit=1 returned "Hip-hop music (music)" as the single top hit, and it
    shipped on a real ad set aimed at gym owners; searching the SAME term with a
    larger limit doesn't return it at all. So ask for several and pick by actual
    lexical match to what was searched, rejecting anything unrelated — those same
    searches also surface "Government", "India" and "Reality television
    personalities". Rejecting is safe: a dropped keyword just leaves the ad broader
    on that axis, whereas a wrong interest spends the budget on the wrong people.
    """
    resp = await client.get(
        f"{graph_base}/search",
        params={"type": "adinterest", "q": keyword, "limit": 10,
                "fields": "id,name,topic", "access_token": access_token},
    )
    hits = [h for h in (resp.json().get("data") or []) if h.get("id")]
    usable = [(_match_score(keyword, h.get("name", "")), h) for h in hits]
    usable = [(s, h) for s, h in usable if s > 0]
    if not usable:
        if hits:
            print(f"[AudienceTargeting] no relevant interest for {keyword!r} — "
                  f"discarded {[h.get('name') for h in hits[:3]]}", flush=True)
        return None
    # Best match wins. Among equals prefer Meta's own generic categories, which it
    # names with a trailing "(category)", over brand and person pages that merely
    # contain the same words — then the plainest name ("Health club (fitness)" over
    # "Goodlife Health Clubs, Australia"). Targeting one gym CHAIN's followers is a
    # far narrower audience than people interested in gyms.
    def _rank(sh):
        score, h = sh
        name = h.get("name", "")
        return (score, 1 if _PAREN_SUFFIX.search(name) else 0, -len(name))

    _, hit = max(usable, key=_rank)
    return {"id": hit["id"], "name": hit["name"]}


async def _drop_invalid_interests(client: httpx.AsyncClient, graph_base: str,
                                  access_token: str, interests: list[dict]) -> list[dict]:
    """Interests Meta's own search happily returns, but its ad-set create then rejects.

    Live-caught on a real launch: search for a wedding audience returned "QC School of
    Event and Wedding Planning", which failed the launch outright with

        Some detailed targeting options have been combined — please update the
        targeting spec to remove them (code=100, subcode=1870247)

    Meta marks these `valid: false` on its adinterestvalid endpoint, so one batch call
    catches them before they can break a launch. A dropped interest just leaves the ad
    broader; a deprecated one stops the campaign going live at all.
    """
    if not interests:
        return []
    resp = await client.get(
        f"{graph_base}/search",
        params={"type": "adinterestvalid",
                "interest_fbid_list": json.dumps([i["id"] for i in interests]),
                "access_token": access_token},
    )
    # Only an explicit `false` drops an interest. A missing id, a missing `valid`
    # field, or an unexpected response shape all mean "unknown", and losing a good
    # interest to a patchy response is worse than the rare deprecated one slipping by.
    verdicts = {str(d.get("id")): d.get("valid") for d in (resp.json().get("data") or [])}
    def _rejected(i: dict) -> bool:
        return verdicts.get(str(i["id"]), True) is False
    kept = [i for i in interests if not _rejected(i)]
    dropped = [i["name"] for i in interests if _rejected(i)]
    if dropped:
        print(f"[AudienceTargeting] dropped interests Meta reports invalid: {dropped}", flush=True)
    return kept


async def resolve_audience_targeting(audience_text: str, access_token: str) -> dict:
    """Meta's targeting fields for this audience description, merge-ready alongside
    geo.meta_targeting_from_geo()'s geo_locations — {} (broad on this axis) for
    empty input, an unconfigured AI key, or any extraction/resolution failure."""
    text = (audience_text or "").strip()
    if not text or not settings.jane_ads_openai_key:
        return {}
    try:
        hints = await _extract_hints(text)
    except Exception as e:
        print(f"[AudienceTargeting] extraction skipped: {e}", flush=True)
        return {}

    targeting: dict = {}
    age_min, age_max = hints.get("age_min"), hints.get("age_max")
    if isinstance(age_min, int) and isinstance(age_max, int) and _MIN_AGE <= age_min < age_max <= _MAX_AGE:
        targeting["age_min"], targeting["age_max"] = age_min, age_max
    gender_codes = _GENDER_CODES.get(str(hints.get("gender", "")).strip().lower())
    if gender_codes:
        targeting["genders"] = gender_codes

    keywords = [str(k).strip() for k in (hints.get("interest_keywords") or []) if str(k).strip()]
    graph_base = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"
    interests = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for keyword in keywords[:_MAX_INTERESTS]:
                try:
                    hit = await _resolve_interest(client, graph_base, access_token, keyword)
                except Exception as e:
                    print(f"[AudienceTargeting] interest lookup skipped for {keyword!r}: {e}", flush=True)
                    continue
                if hit:
                    interests.append(hit)
            # One batch check before any of these reach an ad set — a deprecated
            # interest doesn't merely degrade the targeting, it fails the whole launch.
            interests = await _drop_invalid_interests(client, graph_base, access_token, interests)
    except Exception as e:
        print(f"[AudienceTargeting] interest resolution skipped: {e}", flush=True)
    if interests:
        targeting["flexible_spec"] = [{"interests": interests}]

    return targeting

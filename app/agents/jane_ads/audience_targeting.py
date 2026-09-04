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
        "interest category."
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


async def _resolve_interest(client: httpx.AsyncClient, graph_base: str,
                             access_token: str, keyword: str) -> Optional[dict]:
    """Meta's own targeting-search result for one keyword — an invented interest id
    is rejected outright at ad-set creation, so this is the only reliable source."""
    resp = await client.get(
        f"{graph_base}/search",
        params={"type": "adinterest", "q": keyword, "limit": 1, "access_token": access_token},
    )
    data = resp.json()
    hits = data.get("data") or []
    return {"id": hits[0]["id"], "name": hits[0]["name"]} if hits else None


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
    except Exception as e:
        print(f"[AudienceTargeting] interest resolution skipped: {e}", flush=True)
    if interests:
        targeting["flexible_spec"] = [{"interests": interests}]

    return targeting

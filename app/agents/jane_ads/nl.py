"""
Jane + Ads — natural-language layer ("Layer 0").

Turns a customer's plain-English message ("I want more customers this week, I've got
₦10k, my shop's in Surulere") into the structured CampaignRequest the decision engine
consumes. The LLM UNDERSTANDS the human; the deterministic engine makes the money
decision. If something essential is missing (budget), Jane asks a follow-up instead
of guessing.

The mapping from parsed fields → CampaignRequest is pure and unit-tested; the LLM parse
itself is live (non-deterministic) and degrades gracefully to "ask for more".
"""
from __future__ import annotations

import json
from typing import Optional

import openai
from pydantic import BaseModel

from app.core.config import settings

from .models import (
    CampaignRequest,
    CreativeContext,
    CreativeKind,
    Goal,
    PurchaseBehaviour,
)

_GOALS = {g.value for g in Goal}
_BEHAVIOURS = {b.value for b in PurchaseBehaviour}


class ParsedCampaign(BaseModel):
    """What Jane understood from the message — every field optional; `missing` lists the
    essentials she still needs, and `clarify` is the one question to ask for them."""
    business_name: str = ""
    category: str = ""
    goal: Optional[str] = None
    budget_ngn: Optional[float] = None
    city: str = ""
    stated_behaviour: Optional[str] = None
    is_new_thing: bool = False
    has_existing_demand: bool = False
    has_video: bool = False
    missing: list[str] = []
    clarify: str = ""


async def parse_message(message: str, business_name: str = "", category: str = "") -> ParsedCampaign:
    """Extract structured campaign fields from a plain-language message."""
    if not settings.OPENAI_API_KEY or not (message or "").strip():
        return ParsedCampaign(missing=["budget_ngn"], clarify="How much would you like to spend?")

    prompt = (
        "You are Jane, an ad assistant for Nigerian SMEs. Read the message and extract "
        "what you can. Do NOT invent values — leave a field null/empty if not stated.\n\n"
        f"Known so far — business: '{business_name or '?'}', category: '{category or '?'}'.\n"
        f'Message: "{message}"\n\n'
        "Extract JSON with these keys:\n"
        "- business_name (string)\n- category (e.g. restaurant, fashion, plumber, clinic)\n"
        f"- goal (one of {sorted(_GOALS)}; infer from intent, else null)\n"
        "- budget_ngn (number in Naira; parse '₦10k','10,000','ten thousand' → 10000; else null)\n"
        "- city (the place/area mentioned, e.g. Surulere, Lekki; else empty)\n"
        f"- stated_behaviour (one of {sorted(_BEHAVIOURS)} ONLY if they say how customers find them; else null)\n"
        "- is_new_thing (true if launching something new nobody searches for yet)\n"
        "- has_existing_demand (true if people already search/look for this)\n"
        "- has_video (true if they mention having a video)\n"
        "Return ONLY the JSON object."
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
    except Exception as e:
        print(f"[NL] parse error: {e}", flush=True)
        return ParsedCampaign(missing=["budget_ngn"], clarify="How much would you like to spend?")

    parsed = _coerce(data, business_name, category)
    if not parsed.budget_ngn or parsed.budget_ngn <= 0:
        parsed.missing = ["budget_ngn"]
        parsed.clarify = "About how much would you like to spend on this?"
    return parsed


def _coerce(data: dict, business_name: str, category: str) -> ParsedCampaign:
    """Normalize raw LLM JSON into a validated ParsedCampaign (defensive about types)."""
    def _num(v):
        try:
            return float(v) if v not in (None, "", "null") else None
        except (TypeError, ValueError):
            return None

    goal = str(data.get("goal") or "").lower().replace("-", "_") or None
    beh = str(data.get("stated_behaviour") or "").lower() or None
    return ParsedCampaign(
        business_name=str(data.get("business_name") or business_name or "").strip(),
        category=str(data.get("category") or category or "").strip(),
        goal=goal if goal in _GOALS else None,
        budget_ngn=_num(data.get("budget_ngn")),
        city=str(data.get("city") or "").strip(),
        stated_behaviour=beh if beh in _BEHAVIOURS else None,
        is_new_thing=bool(data.get("is_new_thing")),
        has_existing_demand=bool(data.get("has_existing_demand")),
        has_video=bool(data.get("has_video")),
    )


def to_campaign_request(parsed: ParsedCampaign, business_id: str = "demo") -> Optional[CampaignRequest]:
    """Deterministic map from parsed fields → CampaignRequest. Returns None if the
    budget is missing (the one field we can't proceed without). Pure & unit-tested."""
    if not parsed.budget_ngn or parsed.budget_ngn <= 0:
        return None
    return CampaignRequest(
        business_id=business_id,
        business_name=parsed.business_name,
        category=parsed.category,
        goal=Goal(parsed.goal) if parsed.goal in _GOALS else Goal.MESSAGES,
        budget_ngn=parsed.budget_ngn,
        creative=CreativeContext(
            kind=CreativeKind.VIDEO if parsed.has_video else CreativeKind.IMAGE,
            has_video=parsed.has_video,
        ),
        stated_behaviour=(PurchaseBehaviour(parsed.stated_behaviour)
                          if parsed.stated_behaviour in _BEHAVIOURS else None),
        is_new_thing=parsed.is_new_thing,
        has_existing_demand=parsed.has_existing_demand,
        geo=parsed.city,
    )

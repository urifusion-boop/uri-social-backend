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
    desired_conversions: Optional[int] = None   # "20 customers" — a result, not a Naira amount;
                                                 # the router converts this to a budget when
                                                 # budget_ngn wasn't stated (PRD §3.1)
    city: str = ""
    stated_behaviour: Optional[str] = None
    is_new_thing: bool = False
    has_existing_demand: bool = False
    has_video: bool = False
    missing: list[str] = []
    clarify: str = ""


_NO_BUDGET_CLARIFY = (
    "About how many new customers would make this feel like a win — e.g. \"20 customers\" or "
    "\"around 15 people\"? Or tell me your budget directly if you'd rather."
)

_NO_BUSINESS_CLARIFY = (
    "What would you like to promote? Tell me a bit about your business or what you're selling."
)


class NlUnavailableError(Exception):
    """The language model couldn't be reached — quota exhausted, timeout, outage.
    Distinct from a successful parse that's merely missing a field: the caller must
    surface this as a temporary 'try again later', NOT as another follow-up question.
    Treating an outage as "no budget given" is what made Jane loop forever on the
    budget question when OpenAI was over quota."""


async def parse_message(message: str, business_name: str = "", category: str = "") -> ParsedCampaign:
    """Extract structured campaign fields from a plain-language message. Raises
    NlUnavailableError if the model call fails (so an outage never masquerades as a
    'need more info' follow-up)."""
    if not settings.OPENAI_API_KEY:
        raise NlUnavailableError("OPENAI_API_KEY is not configured")
    if not (message or "").strip():
        # Nothing to parse — ask what they want to promote (or for budget if the
        # business is already known). This is a genuine need-more, not an outage.
        if not business_name and not category:
            return ParsedCampaign(missing=["business_name"], clarify=_NO_BUSINESS_CLARIFY)
        return ParsedCampaign(business_name=business_name, category=category,
                               missing=["budget_ngn"], clarify=_NO_BUDGET_CLARIFY)

    prompt = (
        "You are Jane, an ad assistant for Nigerian SMEs. Read the message and extract "
        "what you can. Do NOT invent values — leave a field null/empty if not stated.\n\n"
        f"Known so far — business: '{business_name or '?'}', category: '{category or '?'}'.\n"
        f'Message: "{message}"\n\n'
        "Extract JSON with these keys:\n"
        "- business_name (string)\n- category (e.g. restaurant, fashion, plumber, clinic)\n"
        f"- goal (one of {sorted(_GOALS)}; infer from intent, else null)\n"
        "- budget_ngn (a NAIRA amount they'll spend; parse '₦10k','10,000','ten thousand' → 10000; "
        "else null — do NOT put a plain customer/people count here)\n"
        "- desired_conversions (a NUMBER OF PEOPLE/CUSTOMERS/ORDERS they want as a RESULT — e.g. "
        "'20 customers' → 20, 'around 15 people' → 15, 'get me 30 orders' → 30; else null — do NOT "
        "put a Naira amount here, and do NOT guess this from a bare number with no people/customer "
        "unit attached)\n"
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
        raise NlUnavailableError(str(e)) from e

    parsed = _coerce(data, business_name, category)
    # Business identity comes first — asking about budget before Jane knows what's
    # being promoted produces a generic, placeholder campaign (a goal alone, e.g. from
    # a quick-reply chip, isn't enough). Only fall through to the budget check once
    # Jane actually knows what this is for, from either the message or history.
    if not parsed.business_name and not parsed.category:
        parsed.missing = ["business_name"]
        parsed.clarify = _NO_BUSINESS_CLARIFY
        return parsed
    # A stated Naira budget always wins. Without one, a stated customer-count is enough to
    # proceed — the router converts it to a budget using real cost-per-conversation data
    # before this reaches to_campaign_request. Only ask again when NEITHER is given.
    if (not parsed.budget_ngn or parsed.budget_ngn <= 0) and not parsed.desired_conversions:
        parsed.missing = ["budget_ngn"]
        parsed.clarify = _NO_BUDGET_CLARIFY
    return parsed


def _coerce(data: dict, business_name: str, category: str) -> ParsedCampaign:
    """Normalize raw LLM JSON into a validated ParsedCampaign (defensive about types)."""
    def _num(v):
        try:
            return float(v) if v not in (None, "", "null") else None
        except (TypeError, ValueError):
            return None

    def _int(v):
        n = _num(v)
        return int(n) if n and n > 0 else None

    goal = str(data.get("goal") or "").lower().replace("-", "_") or None
    beh = str(data.get("stated_behaviour") or "").lower() or None
    return ParsedCampaign(
        business_name=str(data.get("business_name") or business_name or "").strip(),
        category=str(data.get("category") or category or "").strip(),
        goal=goal if goal in _GOALS else None,
        budget_ngn=_num(data.get("budget_ngn")),
        desired_conversions=_int(data.get("desired_conversions")),
        city=str(data.get("city") or "").strip(),
        stated_behaviour=beh if beh in _BEHAVIOURS else None,
        is_new_thing=bool(data.get("is_new_thing")),
        has_existing_demand=bool(data.get("has_existing_demand")),
        has_video=bool(data.get("has_video")),
    )


def to_campaign_request(parsed: ParsedCampaign, business_id: str = "demo") -> Optional[CampaignRequest]:
    """Deterministic map from parsed fields → CampaignRequest. Returns None if Jane
    doesn't know what's being promoted yet (business_name AND category both empty) or
    if the budget is missing — the two things we can't proceed without. Gated here
    directly (not just via parse_message's `missing`/`clarify`) so this stays correct
    even if a caller skips that ordering. Pure & unit-tested."""
    if not parsed.business_name and not parsed.category:
        return None
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

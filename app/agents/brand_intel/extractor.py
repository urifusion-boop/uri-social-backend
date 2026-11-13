"""
Turns the text of a business's website into Brand Playbook fields, with one model call.

Every value comes back with its basis: "stated" (the site says it) or "inferred" (a reasonable
reading the owner should check), and the page it came from. The page shows these as
"From your website" and "Jane's guess". The model is told to leave a field empty rather than
invent it; prices, testimonials and numbers must be copied from the site, never written.
"""
from __future__ import annotations

import json
from typing import Any

import openai

from app.core.config import settings

# The richest model first; the others are what the rest of the codebase already runs on, used
# only if an account doesn't have the first.
MODELS = ("gpt-5.5", "gpt-5.4", "gpt-4o")
MAX_INPUT_CHARS = 36_000


class ExtractionUnavailable(Exception):
    pass


# key -> (kind, what to put there). Keys are the Brand Playbook's own field names.
FIELDS: dict[str, tuple[str, str]] = {
    "brand_name": ("text", "the business's name as customers know it"),
    "industry": ("text", "the industry in 1-4 words, e.g. 'Event transport', 'Skincare', 'Accounting software'"),
    "product_description": ("text", "what the business does and for whom, 1-2 plain sentences"),
    "tagline": ("text", "the site's own slogan or tagline, copied exactly; empty if it has none"),
    "key_products_services": ("list", "each product or service by its name on the site; add the price in brackets only if the site shows it"),
    "price_range": ("text", "the price level or range stated on the site, e.g. '₦25,000 – ₦80,000' or 'premium'; empty if not stated"),
    "unique_selling_proposition": ("text", "what makes them different from others, one sentence"),
    "ideal_customer_profile": ("text", "who the ideal customer is, one sentence describing a real buyer"),
    "customer_pain_points": ("list", "problems the customer has that this business solves, up to 5"),
    "customer_needs": ("list", "what customers want from a business like this, up to 5"),
    "why_customers_choose_us": ("text", "the strongest reason a customer would pick them, one sentence"),
    "proof_points": ("list", "testimonials, reviews, awards or results copied word for word from the site, with the person's name if shown; never written by you"),
    "common_questions": ("list", "questions customers ask, from FAQs or obvious from the site, up to 5"),
    "derived_voice": ("text", "how the site sounds, 3-6 words, e.g. 'warm, plain-spoken, a little playful'"),
    "region": ("text", "the country or region they operate in"),
    "customer_location": ("text", "the cities or areas they serve, e.g. 'Lekki, Victoria Island and Ikoyi'"),
    "languages": ("list", "languages the site is written in"),
}


def _prompt(site: dict) -> str:
    field_lines = "\n".join(f'- "{k}" ({kind}): {desc}' for k, (kind, desc) in FIELDS.items())
    return (
        "You are setting up a marketing playbook for a small business from its own website.\n"
        "Read the pages below and fill the fields. Rules:\n"
        "- Use only what the pages say or clearly imply. If a field isn't supported, leave value empty.\n"
        "- basis is \"stated\" when the pages say it outright, \"inferred\" when it's your reading.\n"
        "- Prices, testimonials, awards and numbers: copy them exactly, mark them stated, never invent.\n"
        "- page: the URL of the page the value came from.\n"
        "- Plain English, no marketing fluff, no emoji.\n\n"
        f"Fields:\n{field_lines}\n\n"
        'Return ONLY JSON: {"fields": {"<key>": {"value": <string, or list of strings for list fields>, '
        '"basis": "stated"|"inferred", "page": "<url>"}}}\n\n'
        f"WEBSITE: {site['url']}\n\n{site['text'][:MAX_INPUT_CHARS]}"
    )


async def extract(site: dict) -> dict[str, dict[str, Any]]:
    """{field_key: {"value", "basis", "page"}} for every field the site supports."""
    if not settings.OPENAI_API_KEY:
        raise ExtractionUnavailable("OPENAI_API_KEY is not configured")
    client = openai.AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    last: Exception | None = None
    for model in MODELS:
        try:
            resp = await client.chat.completions.create(
                model=model,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": _prompt(site)}],
                timeout=90,
            )
            data = json.loads(resp.choices[0].message.content or "{}")
            return clean(data.get("fields") or {}, site)
        except (openai.NotFoundError, openai.PermissionDeniedError) as e:
            last = e  # this account doesn't have the model: try the next one
            continue
        except openai.BadRequestError as e:
            if "model" in str(e).lower():
                last = e
                continue
            raise ExtractionUnavailable(str(e))
        except Exception as e:
            if "insufficient_quota" in str(e):
                from app.services.AIService import _alert_openai_out_of_credit
                await _alert_openai_out_of_credit(str(e))
            raise ExtractionUnavailable(str(e))
    raise ExtractionUnavailable(str(last))


def clean(raw: dict, site: dict) -> dict[str, dict[str, Any]]:
    """Keep only known fields with a real value, in the right shape; drop a 'stated' proof point,
    price or tagline that doesn't actually appear in the site text."""
    text = (site.get("text") or "").lower()
    pages = set(site.get("pages") or [])
    out: dict[str, dict[str, Any]] = {}
    for key, (kind, _) in FIELDS.items():
        item = raw.get(key)
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        if kind == "list":
            if isinstance(value, str):
                value = [value]
            value = [" ".join(str(v).split())[:200] for v in (value or []) if str(v).strip()][:12]
            if not value:
                continue
        else:
            value = " ".join(str(value or "").split())[:400]
            if not value:
                continue
        basis = "stated" if item.get("basis") == "stated" else "inferred"
        page = item.get("page") if item.get("page") in pages else site.get("url", "")
        if key in ("proof_points", "tagline", "price_range"):
            # Words that must be the site's own: keep only what can be found in it.
            if kind == "list":
                value = [v for v in value if _appears(v, text)]
                if not value:
                    continue
            elif not _appears(value, text):
                if key == "price_range" and not any(ch.isdigit() for ch in value):
                    basis = "inferred"   # "premium" is a reading, not a quote
                else:
                    continue
        out[key] = {"value": value, "basis": basis, "page": page}
    return out


def _numbers(s: str) -> set[str]:
    import re
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", s)}


def _appears(value: str, text: str) -> bool:
    """The value is the site's own: every number in it is on the site (a made-up ₦10,000 can't
    pass on the strength of the "000" in ₦25,000), and most of its words are too."""
    if not _numbers(value) <= _numbers(text):
        return False
    words = [w for w in "".join(c if c.isalnum() else " " for c in value.lower()).split()
             if len(w) > 2 and not w.isdigit()]
    if not words:
        return bool(_numbers(value))
    hits = sum(1 for w in words if w in text)
    return hits >= max(1, int(len(words) * 0.7))

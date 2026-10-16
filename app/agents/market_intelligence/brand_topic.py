"""
Uri Market Intelligence — a starter topic built from the brand's own Playbook.

So an owner never starts from a blank form: the question, keywords, location,
competitors and languages come from what the brand already recorded (what it
sells, who buys, where, their pain points and questions, competitors). It is
only a suggestion — the owner still reviews it before anything is collected
(PRD §9), and nothing here is invented: an empty Playbook field stays empty.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.services.AIService import AIService

PROMPT_VERSION = "mi-brand-topic-v1"
MODEL_NAME = "gpt-4o-mini"

_LANG_CODES = {
    "english": "en", "pidgin": "pcm", "nigerian pidgin": "pcm", "yoruba": "yo", "igbo": "ig",
    "hausa": "ha", "french": "fr",
}


class _BrandTopicLLM(BaseModel):
    question: str = Field(description="One plain question about what this brand's market is saying, in the owner's words.")
    keywords: list[str] = Field(description="4-8 search phrases real people would post: the brand name, what it sells in "
                                            "everyday words, buying questions and complaints in this category.")
    excluded_keywords: list[str] = Field(default_factory=list,
                                         description="Terms that would pull obviously unrelated posts (e.g. another meaning of the brand name). Empty if none.")


SYSTEM_PROMPT = """You set up social-listening for a small business from facts it already recorded about itself.

Rules:
- Use ONLY the facts given. Never invent a product, place, competitor or claim.
- question: one plain question the owner would ask, e.g. "What are people in Lagos saying when they look for
  affordable skincare, and what stops them buying?"
- keywords: 4-8 short phrases a real customer would actually post on X/Instagram — include the brand name,
  the product/category in everyday words, and buying-intent or complaint phrasing for this category
  (e.g. "where can I buy", "recommend a"). Not marketing slogans. No hashtags.
- excluded_keywords: only for obvious clashes (a brand name that is also a common word). Otherwise empty."""


def _clean(xs: Any) -> list[str]:
    return [str(x).strip() for x in (xs or []) if str(x or "").strip()]


def _facts(profile: dict) -> dict:
    return {
        "brand_name": (profile.get("brand_name") or "").strip(),
        "industry": (profile.get("industry") or "").strip(),
        "what_we_sell": (profile.get("product_description") or "").strip(),
        "products": _clean(profile.get("key_products_services"))[:8],
        "customer_location": (profile.get("customer_location") or profile.get("region") or "").strip(),
        "target_audience": (profile.get("target_audience") or "").strip()[:300],
        "pain_points": _clean(profile.get("customer_pain_points"))[:6],
        "common_questions": _clean(profile.get("common_questions"))[:6],
        "competitors": [re.sub(r"^@", "", c).strip() for c in _clean(profile.get("competitor_handles"))][:6],
    }


def _languages(profile: dict) -> list[str]:
    out: list[str] = []
    for lang in _clean(profile.get("languages")):
        code = _LANG_CODES.get(lang.lower(), lang.lower() if len(lang) <= 3 else None)
        if code and code not in out:
            out.append(code)
    return out or ["en"]


def _fallback(f: dict) -> dict:
    """No AI available: still useful, built only from the recorded facts."""
    subject = f["what_we_sell"] or ", ".join(f["products"][:2]) or f["industry"] or f["brand_name"]
    where = f" in {f['customer_location']}" if f["customer_location"] else ""
    keywords = [k for k in [f["brand_name"], *f["products"][:4], f["industry"]] if k]
    return {
        "question": f"What are people{where} saying about {subject}, and what stops them buying?",
        "keywords": list(dict.fromkeys(keywords))[:8],
        "excluded_keywords": [],
    }


async def suggest_brand_topic(profile: dict) -> Optional[dict]:
    """A ready-to-review topic for this brand, or None when the Playbook has
    nothing to build one from."""
    f = _facts(profile)
    if not (f["brand_name"] or f["what_we_sell"] or f["products"] or f["industry"]):
        return None

    facts_text = "\n".join(f"{k}: {v}" for k, v in f.items() if v)
    parsed = None
    try:
        req = AIService.build_ai_model(
            model=MODEL_NAME,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": f"Business facts:\n{facts_text}\n\nSet up the topic."}],
            temperature=0.2,
        )
        completion = await AIService.structured_chat_completion(req, response_model=_BrandTopicLLM)
        if not (isinstance(completion, dict) and "error" in completion):
            choice = completion.choices[0]
            if not getattr(choice.message, "refusal", None):
                parsed = choice.message.parsed
    except Exception as e:
        print(f"[MI][brand-topic] AI call failed: {e}")

    base = (
        {"question": parsed.question.strip(), "keywords": _clean(parsed.keywords)[:8],
         "excluded_keywords": _clean(parsed.excluded_keywords)[:6]}
        if parsed and parsed.keywords else _fallback(f)
    )
    if f["brand_name"] and not any(f["brand_name"].lower() in k.lower() for k in base["keywords"]):
        base["keywords"] = [f["brand_name"], *base["keywords"]][:8]
    return {
        **base,
        "geographic_scope": f["customer_location"] or None,
        "competitors": f["competitors"],
        "languages": _languages(profile),
        "requested_days": 7,
        "keep_updating": True,
        "built_from": [k for k, v in f.items() if v],
        "prompt_version": PROMPT_VERSION,
    }

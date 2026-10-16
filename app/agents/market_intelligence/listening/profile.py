"""
Business Listening Profile — built from the Brand Playbook (spec §3).

Structured, versioned, and explainable: every field records where it came
from. AI maps the business to one or more ontology categories in its
country; if none fits, it defines a new category (stored as "auto"), so any
kind of business works without code changes. The brand's own name and
competitors are private terms — never shared with other businesses.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from app.services.AIService import AIService

from . import markets, ontology

PROMPT_VERSION = "mi-listen-profile-v1"

COUNTRY_HINTS = {
    "NG": ["nigeria", "lagos", "abuja", "port harcourt", "ibadan", "kano", "enugu", "benin city", "naija"],
    "GH": ["ghana", "accra", "kumasi"], "KE": ["kenya", "nairobi", "mombasa"], "ZA": ["south africa", "johannesburg", "cape town"],
    "GB": ["united kingdom", "uk", "london", "manchester"], "US": ["united states", "usa", "new york", "california"],
}


class _NewCategory(BaseModel):
    label: str = Field(description="Short category name, e.g. 'Dental clinics'.")
    core_terms: list[str] = Field(description="8-14 short terms people use for this kind of product/service.")
    excluded: list[str] = Field(default_factory=list, description="Other meanings to exclude. Empty if none.")


class _MapLLM(BaseModel):
    category_ids: list[str] = Field(description="1-2 ids from the given list that this business genuinely sells in. Empty if none fits.")
    new_category: Optional[_NewCategory] = Field(default=None, description="Only when no listed category fits.")
    country: str = Field(description="ISO-2 country of the business's customers (e.g. NG), or GLOBAL if unclear.")


MAP_PROMPT = """You map a business to market categories for social listening.
Pick 1-2 category ids from the list ONLY if the business actually sells in that category. If none fits, leave
category_ids empty and define new_category from the business's own facts (never invent products).
country: the ISO-2 country where its customers are (from the location facts); GLOBAL if not stated."""


def _country_from_text(text: str) -> Optional[str]:
    low = (text or "").lower()
    for code, words in COUNTRY_HINTS.items():
        if any(re.search(rf"\b{re.escape(w)}\b", low) for w in words):
            return code
    return None


def _facts(profile: dict) -> dict:
    clean = lambda xs: [str(x).strip() for x in (xs or []) if str(x or "").strip()]
    return {
        "brand_name": (profile.get("brand_name") or "").strip(),
        "industry": (profile.get("industry") or "").strip(),
        "what_we_sell": (profile.get("product_description") or "").strip()[:500],
        "products": clean(profile.get("key_products_services"))[:12],
        "customer_location": (profile.get("customer_location") or profile.get("region") or "").strip(),
        "target_audience": (profile.get("target_audience") or "").strip()[:300],
        "pain_points": clean(profile.get("customer_pain_points"))[:8],
        "common_questions": clean(profile.get("common_questions"))[:8],
        "competitors": [re.sub(r"^@", "", c) for c in clean(profile.get("competitor_handles"))][:8],
        "primary_goal": (profile.get("primary_goal") or "").strip(),
        "languages": clean(profile.get("languages"))[:5],
        "preferred_formats": clean(profile.get("preferred_formats"))[:6],
        "target_platforms": clean(profile.get("target_platforms"))[:6],
        "brand_voice": (profile.get("derived_voice") or "").strip()[:300],
    }


def fingerprint(facts: dict) -> str:
    keys = ("brand_name", "industry", "what_we_sell", "products", "customer_location", "competitors")
    return hashlib.sha256(json.dumps({k: facts.get(k) for k in keys}, sort_keys=True).encode()).hexdigest()[:16]


async def _map(db, facts: dict) -> tuple[list[str], Optional[str]]:
    cats = await ontology.categories(db)
    listing = "\n".join(f"- {c['id']}: {c['label']} ({', '.join((c.get('core_terms') or [])[:6])})" for c in cats)
    facts_text = "\n".join(f"{k}: {v}" for k, v in facts.items() if v and k not in ("brand_voice",))
    country = _country_from_text(facts.get("customer_location", ""))
    try:
        req = AIService.build_ai_model(model="gpt-4o-mini", temperature=0, messages=[
            {"role": "system", "content": MAP_PROMPT},
            {"role": "user", "content": f"Categories:\n{listing}\n\nBusiness facts:\n{facts_text}"}])
        completion = await AIService.structured_chat_completion(req, response_model=_MapLLM)
        if isinstance(completion, dict) and "error" in completion:
            raise RuntimeError(completion["error"])
        parsed: _MapLLM = completion.choices[0].message.parsed
    except Exception as e:
        print(f"[MI][listen] category mapping failed: {e}")
        return [], country
    valid = {c["id"] for c in cats}
    ids = [c for c in parsed.category_ids if c in valid][:2]
    if not ids and parsed.new_category and parsed.new_category.core_terms:
        new = await ontology.add_auto_category(db, parsed.new_category.label, parsed.new_category.core_terms,
                                               parsed.new_category.excluded)
        ids = [new["id"]]
    code = (parsed.country or "").upper()
    return ids, country or (code if len(code) == 2 or code == "GLOBAL" else None)


async def build(db, user_id: str, brand_id: str, playbook: dict, force: bool = False) -> Optional[dict]:
    """Create or refresh this business's listening profile and market
    subscriptions. Returns None when the Playbook doesn't say what it sells."""
    facts = _facts(playbook)
    if not (facts["brand_name"] or facts["what_we_sell"] or facts["products"] or facts["industry"]):
        return None
    fp = fingerprint(facts)
    existing = await db["mi_listening_profiles"].find_one({"brand_id": brand_id})
    if existing and existing.get("fingerprint") == fp and not force:
        existing.pop("_id", None)
        return existing

    await ontology.ensure_seeded(db)
    category_ids, country = await _map(db, facts)
    country = country or "GLOBAL"
    market_keys = []
    for cid in category_ids:
        m = await markets.ensure_market(db, cid, country)
        await markets.subscribe(db, m["key"], brand_id)
        market_keys.append(m["key"])
    await markets.unsubscribe_others(db, brand_id, market_keys)

    now = datetime.utcnow()
    doc = {
        "brand_id": brand_id, "user_id": user_id, "facts": facts, "fingerprint": fp,
        "markets": market_keys, "country": country,
        "private_terms": [t for t in [facts["brand_name"], *facts["competitors"]] if t],
        "status": "active", "prompt_version": PROMPT_VERSION, "updated_at": now,
        "version": int((existing or {}).get("version", 0)) + 1,
        "field_origins": {"facts": "playbook", "markets": "ai_mapping", "country": "playbook" if
                          _country_from_text(facts["customer_location"]) else "ai_mapping"},
    }
    await db["mi_listening_profiles"].update_one({"brand_id": brand_id},
                                                 {"$set": doc, "$setOnInsert": {"created_at": now}}, upsert=True)
    await db["mi_listening_profile_history"].insert_one({
        "brand_id": brand_id, "version": doc["version"], "markets": market_keys, "country": country,
        "fingerprint": fp, "at": now})
    return doc

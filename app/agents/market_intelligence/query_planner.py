"""
Uri Market Intelligence — search planning (wide net, then smart filter).

Social platforms' own search is word-based, so the net has to be cast with
words — but not with a handful of rigid phrases. A topic gets a *search
plan*: groups of the many ways real people actually say the same thing
(other wordings, abbreviations, local language such as Nigerian Pidgin,
common misspellings, buying and complaint phrasing), each group OR-ed into
one platform query. Meaning is applied afterwards: every collected post is
read by the classifier, which drops posts that only share the words
(classification/classify.py, on_topic).

The plan is stored on the topic and assembled at scan time as:
    base groups (AI, from the topic + Playbook)
  + the owner's own keywords, brand and competitors (always kept)
  + terms learned from useful posts (term_learning.py)
  − terms retired for bringing in junk
so each "keep updating" run searches a little more sharply than the last,
without the owner editing keywords.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from app.services.AIService import AIService

PLAN_VERSION = "mi-plan-v1"
MODEL_NAME = "gpt-4o-mini"

MAX_TERMS_PER_GROUP = 12
MAX_GROUPS = 6
MAX_QUERY_CHARS = 380      # X allows ~512; leave room for since/until/lang/exclusions
MAX_QUERIES_PER_SCAN = 8   # cost bound: each query collects up to ITEMS_PER_KEYWORD posts


class _PlanGroup(BaseModel):
    label: str = Field(description="Short name for what this group catches, e.g. 'Asking for a recommendation'.")
    terms: list[str] = Field(description="4-12 short search terms (1-4 words each) real people type.")


class _PlanLLM(BaseModel):
    groups: list[_PlanGroup] = Field(description="3-6 groups covering the topic from different angles.")
    excluded_terms: list[str] = Field(default_factory=list,
                                      description="Words that pull in an unrelated meaning. Empty if none.")


SYSTEM_PROMPT = """You plan social-media searches (X/Twitter, Instagram) for a small business's market research.
Platform search matches WORDS, not meaning, so you list the many ways real people actually phrase things.
A later step reads every post and throws away the ones that only share the words — so favour recall
(catching real conversations) over precision, but never add terms that would mostly pull unrelated posts.

Make 3-6 groups, each catching one angle, e.g.:
- the product/service in everyday words, abbreviations and slang
- people asking for it ("anyone know a", "recommend a", "where can I get", "who fit")
- complaints and frustrations in this category
- the outcome people want (what they're trying to achieve)
- (only if relevant) local-language phrasing

Rules:
- Each term 1-4 words, lower-case, written the way people type on social media. No hashtags, no sentences.
- If the market is Nigeria/West Africa or Pidgin (pcm) is among the languages, include common Nigerian
  Pidgin/English phrasings people really use (e.g. "who fit", "abeg", "how far").
- Include common abbreviations and frequent misspellings where they exist (e.g. "smm", "ig" for instagram).
- Never invent facts about the business; never add competitor or brand names that weren't given.
- Don't repeat the same term in two groups.
- excluded_terms only for obvious clashes (another common meaning of a key word)."""


# ── helpers ──────────────────────────────────────────────────────────────────

def norm(term: str) -> str:
    return " ".join(str(term or "").replace('"', " ").replace("(", " ").replace(")", " ").split()).strip().lower()


def _clean_terms(terms: list[str], limit: int = MAX_TERMS_PER_GROUP) -> list[str]:
    out: list[str] = []
    for t in terms or []:
        t = norm(re.sub(r"^#", "", str(t)))
        if t and len(t) <= 60 and len(t.split()) <= 5 and t not in out:
            out.append(t)
    return out[:limit]


def _or_term(term: str) -> str:
    return f'"{term}"' if " " in term else term


def _chunks(terms: list[str]) -> list[str]:
    """OR-joined, parenthesised expressions, each short enough for X."""
    out, cur = [], []
    for t in terms:
        trial = cur + [_or_term(t)]
        if cur and len("(" + " OR ".join(trial) + ")") > MAX_QUERY_CHARS:
            out.append("(" + " OR ".join(cur) + ")")
            cur = [_or_term(t)]
        else:
            cur = trial
    if cur:
        out.append("(" + " OR ".join(cur) + ")")
    return out


def term_matches(term: str, text: str) -> bool:
    """Does a post contain this term the way X would match it: every word of
    the term present (multi-word terms are phrases in the OR queries, but a
    post containing all the words is still a fair attribution)."""
    words = norm(term).split()
    if not words:
        return False
    hay = " " + re.sub(r"[^\w@#']+", " ", (text or "").lower()) + " "
    return all(f" {w} " in hay or f" #{w} " in hay or f" @{w} " in hay for w in words)


# ── building the plan ────────────────────────────────────────────────────────

async def build_base_plan(topic: Any, business_context: dict) -> dict:
    """Ask the AI for the variation groups. Falls back to the owner's own
    keywords as a single group — never fails a scan over planning."""
    facts = {
        "question": topic.question,
        "owner_keywords": list(topic.keywords or []),
        "market_area": topic.geographic_scope or business_context.get("market_area"),
        "languages": list(topic.languages or []),
        "brand": business_context.get("brand_name"),
        "industry": business_context.get("industry"),
        "products": business_context.get("key_products_services") or [],
    }
    facts_text = "\n".join(f"{k}: {v}" for k, v in facts.items() if v)
    groups: list[dict] = []
    excluded: list[str] = []
    try:
        req = AIService.build_ai_model(
            model=MODEL_NAME,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": f"Research topic:\n{facts_text}\n\nPlan the searches."}],
            temperature=0.3,
        )
        completion = await AIService.structured_chat_completion(req, response_model=_PlanLLM)
        if not (isinstance(completion, dict) and "error" in completion):
            choice = completion.choices[0]
            parsed: Optional[_PlanLLM] = None if getattr(choice.message, "refusal", None) else choice.message.parsed
            if parsed:
                used: set[str] = set()
                for g in parsed.groups[:MAX_GROUPS]:
                    terms = [t for t in _clean_terms(g.terms) if t not in used]
                    used.update(terms)
                    if terms:
                        groups.append({"label": g.label.strip()[:60] or "Related wording", "terms": terms})
                excluded = _clean_terms(parsed.excluded_terms, limit=8)
    except Exception as e:
        print(f"[MI][plan] AI planning failed, using the owner's keywords: {e}")
    return {
        "version": PLAN_VERSION,
        "built_at": datetime.utcnow(),
        "question": topic.question,
        "seed_keywords": list(topic.keywords or []),
        "groups": groups,
        "excluded_terms": excluded,
    }


def plan_is_current(topic: Any) -> bool:
    plan = getattr(topic, "search_plan", None) or {}
    return (plan.get("version") == PLAN_VERSION and plan.get("question") == topic.question
            and plan.get("seed_keywords") == list(topic.keywords or []))


def assemble(topic: Any) -> dict:
    """The groups actually searched this scan, and the X-ready expressions.
    Owner keywords, brand terms and competitors are always present and can't
    be retired; learned terms are added; retired terms are dropped."""
    plan = getattr(topic, "search_plan", None) or {}
    retired = {norm(t) for t in (getattr(topic, "retired_terms", None) or [])}
    protected = _clean_terms(list(topic.keywords or []) + list(topic.competitors or []), limit=40)

    groups: list[dict] = []
    seen: set[str] = set()

    def add(label: str, terms: list[str], kind: str) -> None:
        keep = [t for t in _clean_terms(terms, limit=40)
                if t not in seen and (kind == "owner" or t not in retired)]
        seen.update(keep)
        if keep:
            groups.append({"label": label, "terms": keep, "kind": kind})

    add("Your keywords", list(topic.keywords or []), "owner")
    if topic.competitors:
        add("Competitors", list(topic.competitors), "owner")
    for g in plan.get("groups") or []:
        add(g.get("label") or "Related wording", g.get("terms") or [], "ai")
    learned = getattr(topic, "learned_terms", None) or []
    if learned:
        add("Learned from real posts", learned, "learned")

    expressions: list[str] = []
    for g in groups:
        expressions += _chunks(g["terms"])
    dropped = 0
    if len(expressions) > MAX_QUERIES_PER_SCAN:
        dropped = len(expressions) - MAX_QUERIES_PER_SCAN
        expressions = expressions[:MAX_QUERIES_PER_SCAN]
    excluded = _clean_terms(list(topic.excluded_keywords or []) + list(plan.get("excluded_terms") or []), limit=12)
    return {
        "groups": groups,
        "expressions": expressions,
        "excluded": excluded,
        "terms": [t for g in groups for t in g["terms"]],
        "protected": protected,
        "dropped_queries": dropped,
    }

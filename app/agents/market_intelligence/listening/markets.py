"""
Markets and their searches.

A market is a category in a country (key "hair_wigs:NG"). Its searches are
compiled from data, never written by hand:
  • core   — the category's own terms (plus local-language terms)
  • intent — intent phrases × core terms, e.g. ("who sells" OR "where can i buy") (wig OR "frontal wig")
  • learned— terms discovered in useful posts and promoted to monitoring
Each search is a well-formed OR-group short enough for X; exclusions drop
other meanings of the words. One market's searches serve every business
subscribed to it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from . import ontology

MAX_QUERY_CHARS = 380

COUNTRY_LANGS = {"NG": ["en", "pcm"], "GH": ["en", "pcm"], "KE": ["en", "sw"], "ZA": ["en"], "GB": ["en"], "US": ["en"]}
COUNTRY_NAMES = {"NG": "Nigeria", "GH": "Ghana", "KE": "Kenya", "ZA": "South Africa", "GB": "United Kingdom",
                 "US": "United States", "GLOBAL": "Global"}


def market_key(category_id: str, country: str) -> str:
    return f"{category_id}:{(country or 'GLOBAL').upper()}"


def _clean(terms: list[str]) -> list[str]:
    out: list[str] = []
    for t in terms or []:
        t = " ".join(str(t).replace('"', " ").replace("(", " ").replace(")", " ").split()).lower()
        if t and t not in out:
            out.append(t)
    return out


def _q(term: str) -> str:
    return f'"{term}"' if " " in term else term


def or_groups(terms: list[str], prefix: str = "", limit: int = MAX_QUERY_CHARS) -> list[str]:
    """Pack terms into '(a OR "b c" OR d)' groups; an optional prefix group is
    AND-ed in front of each (used for intent × product)."""
    out, cur = [], []
    head = f"{prefix} " if prefix else ""
    for t in _clean(terms):
        trial = cur + [_q(t)]
        if cur and len(head + "(" + " OR ".join(trial) + ")") > limit:
            out.append(head + "(" + " OR ".join(cur) + ")")
            cur = [_q(t)]
        else:
            cur = trial
    if cur:
        out.append(head + "(" + " OR ".join(cur) + ")")
    return out


def compile_queries(category: dict, country: str, patterns: dict, learned: list[str],
                    max_queries: int) -> dict:
    """The searches for one market run, in priority order, plus exclusions."""
    langs = COUNTRY_LANGS.get((country or "").upper(), ["en"])
    core = _clean(category.get("core_terms") or [])
    local = []
    for lang in langs:
        local += (category.get("local_terms") or {}).get(lang, [])
    queries: list[dict] = []

    # Intent × the most central product terms: buying, recommendation and
    # problem talk is the most commercially useful, so it goes first.
    head_terms = core[:6]
    for lang in langs:
        lp = patterns.get(lang) or {}
        intents = _clean((lp.get("purchase") or [])[:5] + (lp.get("recommendation") or [])[:3])
        problems = _clean((lp.get("problem") or [])[:4] + (lp.get("how_to") or [])[:2])
        for label, phrases in (("intent", intents), ("problem", problems)):
            if not phrases or not head_terms:
                continue
            prefix = "(" + " OR ".join(_q(p) for p in phrases) + ")"
            for g in or_groups(head_terms, prefix=prefix):
                queries.append({"kind": f"{label}:{lang}", "q": g})

    for g in or_groups(core + _clean(local)):
        queries.append({"kind": "core", "q": g})
    for g in or_groups(learned):
        queries.append({"kind": "learned", "q": g})

    return {"queries": queries[:max_queries], "dropped": max(0, len(queries) - max_queries),
            "excluded": _clean(category.get("excluded") or [])[:10], "languages": langs}


async def ensure_market(db, category_id: str, country: str, label: Optional[str] = None,
                        pilot: bool = False) -> dict:
    key = market_key(category_id, country)
    now = datetime.utcnow()
    existing = await db["mi_markets"].find_one({"key": key})
    if existing:
        if pilot and not existing.get("pilot"):
            await db["mi_markets"].update_one({"key": key}, {"$set": {"pilot": True}})
        existing.pop("_id", None)
        return existing
    cat = await ontology.category(db, category_id) or {}
    doc = {
        "key": key, "category_id": category_id, "country": (country or "GLOBAL").upper(),
        "label": f"{label or cat.get('label') or category_id} · {COUNTRY_NAMES.get((country or 'GLOBAL').upper(), country)}",
        "status": "active", "pilot": pilot, "subscribers": [], "learned_terms": [],
        "created_at": now, "last_collected_at": None,
    }
    await db["mi_markets"].insert_one(dict(doc))
    return doc


async def subscribe(db, key: str, brand_id: str) -> None:
    await db["mi_markets"].update_one({"key": key}, {"$addToSet": {"subscribers": brand_id}})


async def unsubscribe_others(db, brand_id: str, keep: list[str]) -> None:
    await db["mi_markets"].update_many({"subscribers": brand_id, "key": {"$nin": keep}},
                                       {"$pull": {"subscribers": brand_id}})


async def due_markets(db, cfg: dict, now: Optional[datetime] = None, only: Optional[list[str]] = None) -> list[dict]:
    """Markets with a subscriber (or marked pilot) whose last collection is
    older than the cadence."""
    from datetime import timedelta
    now = now or datetime.utcnow()
    q: dict[str, Any] = {"status": "active", "$or": [{"subscribers.0": {"$exists": True}}, {"pilot": True}]}
    if only:
        q = {"key": {"$in": only}}
    out = []
    async for m in db["mi_markets"].find(q):
        m.pop("_id", None)
        last = m.get("last_collected_at")
        if only or not last or now - last >= timedelta(hours=cfg["cadence_hours"]):
            out.append(m)
    return out

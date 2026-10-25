"""
The market ontology: categories (core terms, local-language terms,
exclusions) and the intent-phrase library. Stored in `mi_ontology`; the JSON
file only seeds an empty database. Categories the AI creates for businesses
that fit none of the seeded ones are stored here too (status "auto"), so the
system grows without code changes.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Optional

_SEED = os.path.join(os.path.dirname(__file__), "ontology_seed.json")


def _seed() -> dict:
    with open(_SEED, encoding="utf-8") as fh:
        return json.load(fh)


async def ensure_seeded(db) -> None:
    """Seed missing categories and the intent library; never overwrite edits."""
    seed = _seed()
    if not await db["mi_ontology"].find_one({"kind": "intent_patterns"}):
        await db["mi_ontology"].insert_one({"kind": "intent_patterns", "id": "intent_patterns",
                                            "patterns": seed["intent_patterns"], "version": seed["version"],
                                            "created_at": datetime.utcnow()})
    for cat in seed["categories"]:
        if not await db["mi_ontology"].find_one({"kind": "category", "id": cat["id"]}):
            await db["mi_ontology"].insert_one({**cat, "kind": "category", "status": "seeded",
                                                "created_at": datetime.utcnow()})


async def categories(db) -> list[dict]:
    out = []
    async for c in db["mi_ontology"].find({"kind": "category", "status": {"$ne": "retired"}}):
        c.pop("_id", None)
        out.append(c)
    return out


async def category(db, category_id: str) -> Optional[dict]:
    c = await db["mi_ontology"].find_one({"kind": "category", "id": category_id})
    if c:
        c.pop("_id", None)
    return c


async def intent_patterns(db) -> dict:
    doc = await db["mi_ontology"].find_one({"kind": "intent_patterns"})
    return (doc or {}).get("patterns") or _seed()["intent_patterns"]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:40] or "category"


async def add_auto_category(db, label: str, core_terms: list[str], excluded: list[str],
                            local_terms: Optional[dict] = None) -> dict:
    """A category the AI defined for a business no seeded category fits."""
    cid = slug(label)
    existing = await category(db, cid)
    if existing:
        return existing
    doc = {"kind": "category", "id": cid, "label": label.strip()[:60], "core_terms": core_terms[:16],
           "local_terms": local_terms or {}, "excluded": excluded[:8], "status": "auto",
           "created_at": datetime.utcnow()}
    await db["mi_ontology"].insert_one(dict(doc))
    return doc


async def add_term(db, category_id: str, term: str, lang: Optional[str] = None) -> Optional[dict]:
    """A curator typing a term straight into a category (PRD §6.1: the
    ontology is "grown continuously... reviewed through an internal
    curation console") — today that only happens through the discovery
    pipeline surfacing a candidate first; this is the direct path for a
    term the curator already knows belongs, without waiting to see it
    discovered. lang=None adds to core_terms; a language code adds to
    that language's local_terms list instead."""
    term = (term or "").strip()
    if not term:
        return None
    field = f"local_terms.{lang}" if lang else "core_terms"
    result = await db["mi_ontology"].update_one({"kind": "category", "id": category_id},
                                                {"$addToSet": {field: term}})
    if result.matched_count == 0:
        return None
    return await category(db, category_id)


async def remove_term(db, category_id: str, term: str, lang: Optional[str] = None) -> Optional[dict]:
    field = f"local_terms.{lang}" if lang else "core_terms"
    result = await db["mi_ontology"].update_one({"kind": "category", "id": category_id},
                                                {"$pull": {field: term}})
    if result.matched_count == 0:
        return None
    return await category(db, category_id)

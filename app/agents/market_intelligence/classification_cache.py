"""
Uri Market Intelligence — classification cache (PRD §23: "Cache
classification by content and model version; regenerate summaries only for
material changes... Use cheaper deterministic filters before expensive
inference.").

Keyed by exact text + BRAND + model + prompt version — never by text alone.
classify.py's own prompt is explicitly context-dependent (its "Apple the
fruit vs. the device" resolution example only works because business
context is fed in alongside the text), so identical text posted about two
different businesses can legitimately classify differently. Caching by
content_hash + brand_id (a stand-in for "the exact business_context used,"
since business_context is derived 1:1 from brand_id in this pipeline)
avoids ever serving a classification computed under someone else's
business context — a real correctness bug this cache must not introduce
just to save a few LLM calls.

Exposes get/store primitives rather than a combined wrapper around
classify_evidence() so scan_runner's own call to classify_evidence stays
exactly as it was — tests patch that name directly, and this cache is a
layer scan_runner applies around it, not a replacement for it.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from .classification.classify import MODEL_NAME, PROMPT_VERSION
from .models import Classification


def _cache_key(text: str, brand_id: str) -> str:
    digest = hashlib.sha256(text.strip().lower().encode("utf-8")).hexdigest()
    return f"{brand_id}:{digest}:{MODEL_NAME}:{PROMPT_VERSION}"


async def get_cached_classification(db: AsyncIOMotorDatabase, text: str, brand_id: str) -> Optional[Classification]:
    cached = await db["mi_classification_cache"].find_one({"cache_key": _cache_key(text, brand_id)})
    if cached is None:
        return None
    payload = dict(cached["result"])
    payload["evidence_id"] = ""  # stamped by the caller, exactly like a fresh classification
    payload["classified_at"] = datetime.utcnow()
    return Classification(**payload)


async def store_classification_cache(
    db: AsyncIOMotorDatabase, text: str, brand_id: str, classification: Classification
) -> None:
    """Only ever called with a genuine successful classification — a failed
    call (classify_evidence returning None) is never cached, since there's
    nothing reusable about a failure."""
    await db["mi_classification_cache"].update_one(
        {"cache_key": _cache_key(text, brand_id)},
        {"$set": {
            "cache_key": _cache_key(text, brand_id),
            "result": classification.dict(),
            "cached_at": datetime.utcnow(),
        }},
        upsert=True,
    )

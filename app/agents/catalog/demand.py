"""Enquiries for things that couldn't be sold. A signal about missed demand — never proof of lost revenue."""
from __future__ import annotations

import uuid
from typing import Optional

from pymongo.errors import DuplicateKeyError

from .models import DEMAND, utcnow


async def record(db, business_id: str, item_id: str, variant_id: Optional[str], reason: str, query: str = "",
                 source: str = "onebox", key: str = "") -> bool:
    doc = {"_id": uuid.uuid4().hex, "business_id": business_id, "item_id": item_id, "variant_id": variant_id or None,
           "reason": reason, "query": query[:200], "source": source, "created_at": utcnow()}
    if key:
        doc["dedupe_key"] = f"{business_id}:{key}"
    try:
        await db[DEMAND].insert_one(doc)
    except DuplicateKeyError:
        return False
    return True

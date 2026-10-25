"""
Semantic embeddings (PRD §8 stage 4 "Embed": "multilingual semantic vector
per item... very cheap per item, reused everywhere").

Scoped pragmatically: rather than embedding every single collected post (a
much bigger, continuously-running cost the module doesn't need yet), this
embeds exactly the two things PRD §9.1 actually names for relevance —
"semantic similarity between the signal and the business profile
embedding" — and caches both sides so each is computed once, not per call:

  * One embedding per business profile (profile.py, recomputed only when
    the Playbook's own fingerprint changes — the same cache-invalidation
    trigger that already gates category re-mapping).
  * One embedding per topic (topics.py, computed once when a topic is
    first created) — shared across every business subscribed to that
    market, matching the module's "collect once, serve many" principle.

AIService.create_embedding has no error handling of its own (unlike
chat_completion/structured_chat_completion) — embed_text wraps it so a
transient failure (rate limit, network) degrades to "no embedding this
time" rather than breaking profile building or topic creation.
"""
from __future__ import annotations

import math
from typing import Optional

from app.domain.models.chat_model import EmbeddingModel
from app.services.AIService import AIService

EMBED_MODEL = "text-embedding-3-small"


async def embed_text(text: str) -> Optional[list[float]]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        resp = await AIService.create_embedding(EmbeddingModel(input=text[:4000]))
        vec = resp.data[0].embedding
        return list(vec) if vec else None
    except Exception as e:
        print(f"[MI][listen] embedding failed: {e}")
        return None


def cosine_similarity(a: Optional[list[float]], b: Optional[list[float]]) -> Optional[float]:
    """None when either vector is missing — the caller must treat that as
    "no signal," never as a zero similarity score."""
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    norm_a, norm_b = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return None
    return max(-1.0, min(1.0, dot / (norm_a * norm_b)))

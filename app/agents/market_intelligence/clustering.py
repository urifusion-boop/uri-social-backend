"""
Uri Market Intelligence — clustering (PRD §11).

Groups classified evidence of the same primary_type into Cluster records.

MEASURED, not assumed: PRD §17 says "introduce a dedicated vector service only
if volume or measured search performance requires it," so this started as
plain keyword-overlap (Jaccard) clustering with no embeddings call. Testing it
against fixture data proved that doesn't work — two posts written to be
obviously the same complaint ("delayed for 6 days" vs "taking over a week")
scored a Jaccard similarity of 0.071, nowhere near usable. Short, paraphrased
social text just doesn't share enough literal tokens for keyword overlap to
detect real semantic similarity. That's the "measured" requirement PRD §17
names — so this uses real embeddings (text-embedding-3-small, already used
elsewhere in this codebase per AIService.create_embedding) with cosine
similarity, falling back to Jaccard + same-thread only for any single item
whose embedding call fails (never silently drops evidence from clustering).

Similar text about DIFFERENT businesses must never merge (PRD §19) — not a risk
here since every Evidence passed in is already scoped to one topic/brand before
it reaches this module.
"""
from __future__ import annotations

import asyncio
import math
import re
import uuid
from datetime import datetime
from typing import Optional

from app.services.AIService import client as openai_client
from .models import Cluster, Evidence, EvidenceType, Lifecycle

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "to", "of", "in", "on", "for",
    "and", "or", "but", "i", "my", "me", "it", "this", "that", "has", "have",
    "been", "with", "at", "be", "am", "do", "does", "did", "so", "if", "as",
}

# Cosine-similarity threshold for text-embedding-3-small on short social posts —
# a documented, tunable pilot default (same framing as PRD §12's thresholds),
# not a claimed-universal constant. Jaccard is only the last-resort fallback
# when an embedding couldn't be obtained for one of the two items being compared.
EMBEDDING_SIMILARITY_THRESHOLD = 0.78
JACCARD_FALLBACK_THRESHOLD = 0.35
EMBEDDING_MODEL = "text-embedding-3-small"


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z']+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def _embed_texts(texts: list[str]) -> dict[str, list[float]]:
    """One embedding per unique text. AIService.create_embedding is `async def`
    but its body calls the OpenAI SDK synchronously with no run_in_executor —
    it would block the event loop under load, so this calls the client
    directly via run_in_executor instead of going through that method. A
    failed embedding is dropped from the result dict, not raised — the caller
    falls back to Jaccard for exactly the items missing a vector, so one bad
    call degrades clustering quality for one item rather than failing the
    whole scan."""
    unique_texts = list({t for t in texts if t.strip()})
    if not unique_texts:
        return {}

    loop = asyncio.get_running_loop()
    try:
        response = await loop.run_in_executor(
            None,
            lambda: openai_client.embeddings.create(model=EMBEDDING_MODEL, input=unique_texts),
        )
    except Exception as e:
        print(f"[MI][clustering] embedding call failed for {len(unique_texts)} texts: {e}")
        return {}

    return {unique_texts[i]: response.data[i].embedding for i in range(len(unique_texts))}


async def cluster_evidence(
    evidence_list: list[Evidence],
    topic_id: str,
    brand_id: str,
    primary_types: dict[str, EvidenceType],
    now: Optional[datetime] = None,
) -> list[Cluster]:
    """`primary_types` maps evidence.id -> its Classification.primary_type
    (kept out of Evidence itself, so Evidence stays a pure collection record —
    see models.py). Only groups evidence whose type actually clusters
    meaningfully (concern/unmet_need/trend/competitor_movement); inquiries and
    developments are handled individually elsewhere, not clustered."""
    now = now or datetime.utcnow()
    clusterable_types = {
        EvidenceType.CUSTOMER_CONCERN,
        EvidenceType.UNMET_NEED,
        EvidenceType.EMERGING_TREND,
        EvidenceType.COMPETITOR_MOVEMENT,
    }

    by_type: dict[EvidenceType, list[Evidence]] = {}
    for e in evidence_list:
        t = primary_types.get(e.id)
        if t in clusterable_types:
            by_type.setdefault(t, []).append(e)

    if not by_type:
        return []

    all_texts = [e.text for items in by_type.values() for e in items]
    embeddings = await _embed_texts(all_texts)

    clusters: list[Cluster] = []

    for evidence_type, items in by_type.items():
        groups: list[list[Evidence]] = []
        group_tokens: list[set[str]] = []
        group_parents: list[set[str]] = []
        group_embeddings: list[list[list[float]]] = []  # per group, all member embeddings seen so far

        for item in items:
            item_tokens = _tokens(item.text)
            item_embedding = embeddings.get(item.text)
            placed = False

            for idx, members in enumerate(groups):
                same_thread = item.parent_id is not None and item.parent_id in group_parents[idx]

                semantic_match = False
                if item_embedding is not None and group_embeddings[idx]:
                    semantic_match = any(
                        _cosine(item_embedding, other) >= EMBEDDING_SIMILARITY_THRESHOLD
                        for other in group_embeddings[idx]
                    )
                elif not group_embeddings[idx] or item_embedding is None:
                    # Embedding missing for this item or the whole group — fall
                    # back to keyword overlap rather than refusing to compare.
                    semantic_match = _jaccard(item_tokens, group_tokens[idx]) >= JACCARD_FALLBACK_THRESHOLD

                if same_thread or semantic_match:
                    members.append(item)
                    group_tokens[idx] |= item_tokens
                    if item_embedding is not None:
                        group_embeddings[idx].append(item_embedding)
                    if item.parent_id:
                        group_parents[idx].add(item.parent_id)
                    placed = True
                    break

            if not placed:
                groups.append([item])
                group_tokens.append(item_tokens)
                group_embeddings.append([item_embedding] if item_embedding is not None else [])
                group_parents.append({item.parent_id} if item.parent_id else set())

        for members, parents in zip(groups, group_parents):
            independent_accounts = len({m.author_handle for m in members if m.author_handle})
            # A "thread" is either a real parent_id, or — for standalone posts with
            # no shared parent — each distinct post counts as its own original
            # thread (PRD's "three original conversation threads" bar assumes
            # posts, not just replies, can each originate a thread).
            thread_ids = {m.parent_id for m in members if m.parent_id} or {m.source_id for m in members}
            dated = [m.published_at for m in members if m.published_at]

            clusters.append(
                Cluster(
                    id=str(uuid.uuid4()),
                    brand_id=brand_id,
                    topic_id=topic_id,
                    primary_type=evidence_type,
                    theme=_theme_label(members),
                    evidence_ids=[m.id for m in members],
                    independent_account_count=independent_accounts,
                    original_thread_count=len(thread_ids),
                    first_seen=min(dated) if dated else now,
                    last_updated=max(dated) if dated else now,
                    lifecycle=Lifecycle.UNKNOWN,
                )
            )

    return clusters


def _theme_label(members: list[Evidence]) -> str:
    """Short human label — the most common non-stopword tokens across the
    cluster's evidence, not an LLM call. Good enough to tell clusters apart in
    a list; insight_composer.py generates the real headline via evidence-backed
    prose."""
    counts: dict[str, int] = {}
    for m in members:
        for tok in _tokens(m.text):
            counts[tok] = counts.get(tok, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:4]
    return " / ".join(t for t, _ in top) if top else "Untitled cluster"

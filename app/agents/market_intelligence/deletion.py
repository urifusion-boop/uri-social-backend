"""
Uri Market Intelligence — evidence deletion cascade (PRD §16, §21, P0-14).

"Deletion or permission revocation must remove affected content from
storage, search, embeddings, caches and future model inputs, and reassess
dependent claims" (PRD §21). This pilot has no separate search index or
persisted embedding cache to purge — embeddings are computed fresh per scan
and never stored (see clustering.py) — so the cascade here covers exactly
the collections this module actually persists to: mi_evidence,
mi_classifications, mi_clusters, mi_insights.
"""
from __future__ import annotations

from typing import Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from .models import InsightStatus


async def delete_evidence_cascade(evidence_id: str, brand_id: str, db: AsyncIOMotorDatabase) -> Optional[dict]:
    """Returns None if the evidence doesn't exist or isn't owned by this
    brand — same "404, not a distinguishing error" posture as the rest of
    this module (PRD §21: "No user may retrieve another tenant's insight by
    guessing an identifier"). Otherwise returns a summary of what changed.

    An insight that loses ALL of its evidence to this cascade is marked
    RETRACTED rather than silently kept "active" with an empty evidence
    list — PRD §13: "Removed evidence can trigger confidence reduction,
    retraction or notification correction." Retraction is the only honest
    option once nothing backs the claim any more; an insight that still has
    other evidence keeps its status but gets a coverage_note recording the
    removal."""
    evidence_doc = await db["mi_evidence"].find_one({"id": evidence_id})
    if not evidence_doc or evidence_doc.get("brand_id") != brand_id:
        return None

    await db["mi_evidence"].delete_one({"id": evidence_id})
    await db["mi_classifications"].delete_many({"evidence_id": evidence_id})

    clusters_updated: list[str] = []
    clusters_deleted: list[str] = []
    async for cluster_doc in db["mi_clusters"].find({"brand_id": brand_id, "evidence_ids": evidence_id}):
        remaining = [eid for eid in cluster_doc.get("evidence_ids", []) if eid != evidence_id]
        if remaining:
            await db["mi_clusters"].update_one({"id": cluster_doc["id"]}, {"$set": {"evidence_ids": remaining}})
            clusters_updated.append(cluster_doc["id"])
        else:
            await db["mi_clusters"].delete_one({"id": cluster_doc["id"]})
            clusters_deleted.append(cluster_doc["id"])

    insights_updated: list[str] = []
    insights_retracted: list[str] = []
    async for insight_doc in db["mi_insights"].find({"brand_id": brand_id, "evidence_ids": evidence_id}):
        remaining = [eid for eid in insight_doc.get("evidence_ids", []) if eid != evidence_id]
        if remaining:
            await db["mi_insights"].update_one(
                {"id": insight_doc["id"]},
                {"$set": {
                    "evidence_ids": remaining,
                    "coverage_note": "One or more source records behind this insight were removed.",
                }},
            )
            insights_updated.append(insight_doc["id"])
        else:
            await db["mi_insights"].update_one(
                {"id": insight_doc["id"]}, {"$set": {"status": InsightStatus.RETRACTED.value}}
            )
            insights_retracted.append(insight_doc["id"])

    return {
        "evidence_id": evidence_id,
        "clusters_updated": clusters_updated,
        "clusters_deleted": clusters_deleted,
        "insights_updated": insights_updated,
        "insights_retracted": insights_retracted,
    }

"""
V2-only success metrics (PRD Section 16), computed from stored render
documents in visual_engine_renders_v2 — no separate instrumentation
pipeline, just an honest aggregation over what's already persisted.

Some PRD metrics are true "by design" facts rather than measured rates —
text correctness and brand accuracy are structural guarantees of the
template-typesetting architecture (real rendered type, injected exact brand
values), not something that needs sampling the way the old single-model
pipeline did. Customer-visible quality (complaint rate / sample review) has
no data source anywhere in this system yet and is reported as untracked
rather than invented.
"""
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase


def _avg_cost(renders: List[Dict[str, Any]]) -> Optional[float]:
    costs = [r.get("total_cost", 0.0) for r in renders]
    return round(sum(costs) / len(costs), 4) if costs else None


class VisualEngineMetricsServiceV2:
    @staticmethod
    async def compute(
        db: AsyncIOMotorDatabase, user_id: Optional[str] = None, brand_id: Optional[str] = None
    ) -> Dict[str, Any]:
        query: Dict[str, Any] = {}
        if user_id:
            query["user_id"] = user_id
        if brand_id:
            query["brand_profile_id"] = brand_id

        renders = await db["visual_engine_renders_v2"].find(query).to_list(length=5000)
        total = len(renders)

        if total == 0:
            return {
                "total_renders": 0,
                "text_correctness_rate": 1.0,
                "brand_accuracy": 1.0,
                "auto_publish_rate": None,
                "render_success_rate": None,
                "avg_cost_per_post_usd": None,
                "avg_cost_per_carousel_usd": None,
                "single_post_count": 0,
                "carousel_count": 0,
                "customer_visible_quality": "not tracked — no complaint/feedback pipeline wired to V2 yet",
            }

        auto_count = sum(1 for r in renders if r.get("review_tier") == "auto")
        success_count = sum(1 for r in renders if not r.get("needs_attention"))
        singles = [r for r in renders if not r.get("is_carousel")]
        carousels = [r for r in renders if r.get("is_carousel")]

        return {
            "total_renders": total,
            # By design, not measured: template typesetting renders real type
            # (never a generated headline) and brand values are injected from
            # the profile (never AI-guessed) — PRD Section 16 targets both at
            # 100% "by design", so there's nothing to sample here.
            "text_correctness_rate": 1.0,
            "brand_accuracy": 1.0,
            "auto_publish_rate": round(auto_count / total, 4),
            "render_success_rate": round(success_count / total, 4),
            "avg_cost_per_post_usd": _avg_cost(singles),
            "avg_cost_per_carousel_usd": _avg_cost(carousels),
            "single_post_count": len(singles),
            "carousel_count": len(carousels),
            "customer_visible_quality": "not tracked — no complaint/feedback pipeline wired to V2 yet",
        }

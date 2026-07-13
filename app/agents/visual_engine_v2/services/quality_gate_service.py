"""
Quality Gate Service - Visual Engine V2
Human review queue for quality control

PRD Section 13: Quality Gate & Human Review — tiered by risk:
- auto: standard posts, complete brand profile, confidence above threshold — posts on schedule
- soft: new client's first posts, or borderline confidence — surfaced for quick approve/reject,
        auto-publishes if not rejected within a window
- mandatory: AI re-composited photos (premium) or anything flagged by a safety/quality
             check (including this module's own fallback path) — never auto-publishes
"""

from typing import Dict, Optional, List, Tuple
from datetime import datetime, timedelta
from bson import ObjectId

from app.agents.visual_engine_v2.models.visual_engine_models import (
    VisualEngineRenderV2,
    VisualEngineReviewQueueV2
)
from app.agents.visual_engine_v2.config.vendor_config import FeatureFlags

SOFT_REVIEW_WINDOW = timedelta(hours=24)


class QualityGateService:
    """
    Quality gate and review queue management.

    Flow:
    1. Render completes → compute quality score + review tier
    2. auto → publish on schedule, no queue entry
    3. soft/mandatory → review queue entry; soft auto-approves if not rejected
       within SOFT_REVIEW_WINDOW (see sweep_expired_soft_reviews), mandatory never does
    """

    def __init__(self, db):
        self.db = db
        self.feature_flags = FeatureFlags()

    async def evaluate_render(
        self,
        render: VisualEngineRenderV2,
        user_id: str
    ) -> Dict[str, any]:
        """
        Evaluate render quality, assign a review tier, and queue it if needed.

        Returns:
            {
                "approved": bool,
                "requires_review": bool,
                "review_tier": "auto" | "soft" | "mandatory",
                "quality_score": float,
                "issues": List[str],
                "review_queue_id": Optional[str]
            }
        """
        print(f"🔍 Quality Gate: Evaluating render {render.id}")

        quality_score, issues = await self._calculate_quality_score(render)
        tier, expires_at, reason = await self._determine_review_tier(render, quality_score)

        # Mutate the render so the caller's subsequent DB save persists the tier/expiry
        render.review_tier = tier
        render.review_expires_at = expires_at
        render.confidence_score = quality_score

        print(f"📊 Quality Score: {quality_score:.2f} (threshold: {self.feature_flags.MIN_CONFIDENCE_AUTO_PUBLISH}) → tier: {tier}")

        if tier == "auto":
            print("✅ Quality Gate: AUTO-PUBLISH")
            return {
                "approved": True,
                "requires_review": False,
                "review_tier": tier,
                "quality_score": quality_score,
                "issues": issues,
                "review_queue_id": None
            }

        review_queue_id = await self._add_to_review_queue(render, quality_score, issues, user_id, tier, reason)
        print(f"⚠️ Quality Gate: {tier.upper()} REVIEW REQUIRED (queue_id={review_queue_id}) — {reason}")

        return {
            "approved": False,
            "requires_review": True,
            "review_tier": tier,
            "quality_score": quality_score,
            "issues": issues,
            "review_queue_id": review_queue_id
        }

    async def _determine_review_tier(
        self,
        render: VisualEngineRenderV2,
        quality_score: float
    ) -> Tuple[str, Optional[datetime], str]:
        """
        PRD Section 13 tiering, in priority order:
        1. mandatory — a fallback was used (needs_attention), or the imagery went
           through AI re-compositing (premium; not yet implemented upstream, but the
           hook is here so it's honored the moment that cleanup level is added)
        2. soft — first-N posts for this brand, or confidence below threshold
        3. auto — everything else
        """
        if render.needs_attention:
            return "mandatory", None, "Render used a fallback background/image and needs a human look"

        if render.imagery_layer.metadata.get("cleanup_level") == "ai_recomposite":
            return "mandatory", None, "AI re-composited product photo (premium) always requires review"

        if await self._is_within_first_n_posts(render.brand_profile_id):
            n = self.feature_flags.REQUIRE_REVIEW_FIRST_N_POSTS
            return "soft", datetime.utcnow() + SOFT_REVIEW_WINDOW, f"Within this brand's first {n} posts"

        if quality_score < self.feature_flags.MIN_CONFIDENCE_AUTO_PUBLISH:
            return (
                "soft",
                datetime.utcnow() + SOFT_REVIEW_WINDOW,
                f"Confidence score {quality_score:.2f} below {self.feature_flags.MIN_CONFIDENCE_AUTO_PUBLISH} threshold"
            )

        return "auto", None, ""

    async def _is_within_first_n_posts(self, brand_profile_id: str) -> bool:
        """PRD Section 13: a new client's first N posts get soft review regardless of confidence."""
        n = self.feature_flags.REQUIRE_REVIEW_FIRST_N_POSTS
        count = await self.db["visual_engine_renders_v2"].count_documents(
            {"brand_profile_id": brand_profile_id}
        )
        return count < n

    async def _calculate_quality_score(
        self,
        render: VisualEngineRenderV2
    ) -> tuple[float, List[str]]:
        """
        Calculate quality score (0.0 - 1.0) and detect issues.

        Scoring factors:
        - Content quality (headline/subtext length, clarity)
        - Imagery quality (resolution, aspect ratio match)
        - Brand consistency (logo present, colors applied)
        - Template render success
        """
        score = 1.0
        issues = []

        # Check 1: Content layer
        content_data = render.content_layer.data
        if not content_data.get("headline"):
            score -= 0.3
            issues.append("missing_headline")
        elif len(content_data["headline"]) < 3:
            score -= 0.2
            issues.append("headline_too_short")

        if not content_data.get("cta"):
            score -= 0.2
            issues.append("missing_cta")

        # Check 2: Imagery layer
        imagery_data = render.imagery_layer.data
        if not imagery_data.get("imagery_url"):
            score -= 0.4
            issues.append("missing_imagery")

        # Check 3: Brand layer
        brand_data = render.brand_layer.data
        if not brand_data.get("logo_url"):
            score -= 0.1
            issues.append("missing_logo")

        if not brand_data.get("primary_color"):
            score -= 0.1
            issues.append("missing_primary_color")

        # Check 4: Typesetting layer (render success)
        typesetting_data = render.typesetting_layer.data
        rendered_urls = typesetting_data.get("rendered_urls", [])
        if not rendered_urls:
            score -= 0.5
            issues.append("render_failed")

        # Ensure score is in range [0.0, 1.0]
        score = max(0.0, min(1.0, score))

        return score, issues

    async def _add_to_review_queue(
        self,
        render: VisualEngineRenderV2,
        quality_score: float,
        issues: List[str],
        user_id: str,
        review_tier: str = "soft",
        reason: str = ""
    ) -> str:
        """
        Add render to review queue for human approval.
        """
        review_queue_item = VisualEngineReviewQueueV2(
            render_id=str(render.id),
            user_id=user_id,
            brand_profile_id=render.brand_profile_id,
            review_tier=review_tier,
            review_reason=reason or (", ".join(issues) if issues else "Below confidence threshold"),
            quality_score=quality_score,
            detected_issues=issues,
            preview_url=(render.final_outputs[0] if render.final_outputs else ""),
            content_preview={
                "headline": render.content_layer.data.get("headline", ""),
                "subtext": render.content_layer.data.get("subtext", ""),
                "cta": render.content_layer.data.get("cta", ""),
            },
            status="pending",
            created_at=datetime.utcnow(),
            reviewed_at=None,
            reviewer_notes=None
        )

        result = await self.db["visual_engine_review_queue_v2"].insert_one(
            review_queue_item.model_dump()
        )

        return str(result.inserted_id)

    async def approve_render(self, review_queue_id: str, reviewer_notes: Optional[str] = None) -> bool:
        """
        Manually approve a render from review queue. Also flips the underlying
        render document's own status — this is what a publish step actually
        gates on, not the review-queue entry (which is just a UI worklist).

        Args:
            review_queue_id: Review queue item ID
            reviewer_notes: Optional notes from human reviewer

        Returns:
            True if approved successfully
        """
        queue_item = await self.db["visual_engine_review_queue_v2"].find_one(
            {"_id": ObjectId(review_queue_id)}
        )
        if not queue_item:
            return False

        now = datetime.utcnow()
        result = await self.db["visual_engine_review_queue_v2"].update_one(
            {"_id": ObjectId(review_queue_id)},
            {
                "$set": {
                    "status": "approved",
                    "reviewed_at": now,
                    "reviewer_notes": reviewer_notes
                }
            }
        )

        if result.modified_count > 0:
            await self.db["visual_engine_renders_v2"].update_one(
                {"_id": queue_item["render_id"]},
                {"$set": {"status": "approved", "reviewed_at": now}}
            )
            print(f"✅ Review approved: {review_queue_id} (render {queue_item['render_id']})")
            return True

        return False

    async def reject_render(self, review_queue_id: str, reviewer_notes: Optional[str] = None) -> bool:
        """
        Manually reject a render from review queue. Also flips the underlying
        render document's own status so a publish step can never act on it.

        Args:
            review_queue_id: Review queue item ID
            reviewer_notes: Reason for rejection

        Returns:
            True if rejected successfully
        """
        queue_item = await self.db["visual_engine_review_queue_v2"].find_one(
            {"_id": ObjectId(review_queue_id)}
        )
        if not queue_item:
            return False

        now = datetime.utcnow()
        result = await self.db["visual_engine_review_queue_v2"].update_one(
            {"_id": ObjectId(review_queue_id)},
            {
                "$set": {
                    "status": "rejected",
                    "reviewed_at": now,
                    "reviewer_notes": reviewer_notes
                }
            }
        )

        if result.modified_count > 0:
            await self.db["visual_engine_renders_v2"].update_one(
                {"_id": queue_item["render_id"]},
                {"$set": {"status": "rejected", "reviewed_at": now}}
            )
            print(f"❌ Review rejected: {review_queue_id} (render {queue_item['render_id']})")
            return True

        return False

    async def get_pending_reviews(self, user_id: Optional[str] = None) -> List[VisualEngineReviewQueueV2]:
        """
        Get all pending review queue items.

        Args:
            user_id: Optional filter by user ID

        Returns:
            List of pending review queue items
        """
        query = {"status": "pending"}
        if user_id:
            query["user_id"] = user_id

        cursor = self.db["visual_engine_review_queue_v2"].find(query).sort("created_at", -1)
        items = await cursor.to_list(length=100)

        return [VisualEngineReviewQueueV2(**item) for item in items]

    async def sweep_expired_soft_reviews(self) -> Dict[str, int]:
        """
        PRD Section 13: soft-review renders auto-publish if not explicitly rejected
        within their review window. Mandatory-tier renders are never touched here.

        This is intentionally just a plain method + router endpoint, not wired into
        the shared APScheduler/GitHub Actions cron infrastructure — this module is
        still isolated for testing (see the Visual Engine V2 build note). Point an
        external cron at POST /social-media/visual-engine/v2/review-queue/sweep-expired
        once ready to move past that.
        """
        now = datetime.utcnow()

        cursor = self.db["visual_engine_renders_v2"].find({
            "review_tier": "soft",
            "review_expires_at": {"$lte": now},
        })
        expired = await cursor.to_list(length=500)

        auto_approved = 0
        for render_doc in expired:
            render_id = render_doc.get("id") or str(render_doc.get("_id"))

            # Don't auto-approve if a human already rejected it
            rejected = await self.db["visual_engine_review_queue_v2"].find_one({
                "render_id": render_id,
                "status": "rejected"
            })
            if rejected:
                continue

            await self.db["visual_engine_renders_v2"].update_one(
                {"_id": render_doc["_id"]},
                {"$set": {"status": "approved", "reviewed_at": now}}
            )
            await self.db["visual_engine_review_queue_v2"].update_many(
                {"render_id": render_id, "status": "pending"},
                {"$set": {
                    "status": "approved",
                    "reviewed_at": now,
                    "reviewer_notes": "Auto-approved: soft-review window expired without rejection"
                }}
            )
            auto_approved += 1

        print(f"🧹 Soft-review sweep: checked {len(expired)}, auto-approved {auto_approved}")
        return {"checked": len(expired), "auto_approved": auto_approved}

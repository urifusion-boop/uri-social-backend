"""
Quality Gate Service - Visual Engine V2
Human review queue for quality control

PRD Section 14: Quality Gate & Human Review
- Beta users: Quality gate enabled by default
- Production users: Quality gate disabled (auto-approve)
- Scoring system for failure detection
- Review queue for manual approval
"""

from typing import Dict, Optional, List
from datetime import datetime
from bson import ObjectId

from app.agents.visual_engine_v2.models.visual_engine_models import (
    VisualEngineRenderV2,
    VisualEngineReviewQueueV2
)
from app.agents.visual_engine_v2.config.vendor_config import FeatureFlags


class QualityGateService:
    """
    Quality gate and review queue management.

    Flow:
    1. Render completes → QualityGate check
    2. If quality_gate_enabled && score < threshold → Review Queue
    3. Else → Auto-approve
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
        Evaluate render quality and determine approval/review.

        Args:
            render: Completed VisualEngineRenderV2
            user_id: User ID for feature flag check

        Returns:
            {
                "approved": bool,
                "requires_review": bool,
                "quality_score": float,
                "issues": List[str],
                "review_queue_id": Optional[str]
            }
        """
        print(f"🔍 Quality Gate: Evaluating render {render.id}")

        # Check if quality gate is enabled for this user
        quality_gate_enabled = await self._is_quality_gate_enabled(user_id)

        if not quality_gate_enabled:
            print("✓ Quality Gate: DISABLED for this user (auto-approve)")
            return {
                "approved": True,
                "requires_review": False,
                "quality_score": 1.0,
                "issues": [],
                "review_queue_id": None
            }

        # Run quality checks
        quality_score, issues = await self._calculate_quality_score(render)

        print(f"📊 Quality Score: {quality_score:.2f} (threshold: {self.feature_flags.quality_gate_threshold})")

        # Determine if review is required
        requires_review = quality_score < self.feature_flags.quality_gate_threshold

        if requires_review:
            # Add to review queue
            review_queue_id = await self._add_to_review_queue(render, quality_score, issues, user_id)
            print(f"⚠️ Quality Gate: REVIEW REQUIRED (queue_id={review_queue_id})")

            return {
                "approved": False,
                "requires_review": True,
                "quality_score": quality_score,
                "issues": issues,
                "review_queue_id": review_queue_id
            }
        else:
            print("✅ Quality Gate: PASSED (auto-approve)")
            return {
                "approved": True,
                "requires_review": False,
                "quality_score": quality_score,
                "issues": issues,
                "review_queue_id": None
            }

    async def _is_quality_gate_enabled(self, user_id: str) -> bool:
        """
        Check if quality gate is enabled for this user.

        PRD: Beta users have quality gate enabled by default.
        """
        # Check if user is in beta list
        if user_id in self.feature_flags.beta_users:
            return True

        # Check user settings (allow manual override)
        user = await self.db["users"].find_one({"_id": ObjectId(user_id)})
        if user:
            return user.get("quality_gate_enabled", False)

        return False

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
        user_id: str
    ) -> str:
        """
        Add render to review queue for human approval.
        """
        review_queue_item = VisualEngineReviewQueueV2(
            render_id=str(render.id),
            user_id=user_id,
            brand_profile_id=render.brand_profile_id,
            quality_score=quality_score,
            detected_issues=issues,
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
        Manually approve a render from review queue.

        Args:
            review_queue_id: Review queue item ID
            reviewer_notes: Optional notes from human reviewer

        Returns:
            True if approved successfully
        """
        result = await self.db["visual_engine_review_queue_v2"].update_one(
            {"_id": ObjectId(review_queue_id)},
            {
                "$set": {
                    "status": "approved",
                    "reviewed_at": datetime.utcnow(),
                    "reviewer_notes": reviewer_notes
                }
            }
        )

        if result.modified_count > 0:
            print(f"✅ Review approved: {review_queue_id}")
            return True

        return False

    async def reject_render(self, review_queue_id: str, reviewer_notes: Optional[str] = None) -> bool:
        """
        Manually reject a render from review queue.

        Args:
            review_queue_id: Review queue item ID
            reviewer_notes: Reason for rejection

        Returns:
            True if rejected successfully
        """
        result = await self.db["visual_engine_review_queue_v2"].update_one(
            {"_id": ObjectId(review_queue_id)},
            {
                "$set": {
                    "status": "rejected",
                    "reviewed_at": datetime.utcnow(),
                    "reviewer_notes": reviewer_notes
                }
            }
        )

        if result.modified_count > 0:
            print(f"❌ Review rejected: {review_queue_id}")
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

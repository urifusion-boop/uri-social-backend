"""
Publish Bridge Service - Visual Engine V2

Bridges a completed V2 render into the EXISTING, working posting pipeline
(ApprovalWorkflowService + the social_connections collection) rather than
reimplementing platform API calls. V2 owns content/imagery/brand/typesetting
and its own tiered review gate; once a render clears that gate, this service
is the only thing that talks to the production drafts/posting system, and it
does so by constructing a real content_drafts document in the exact shape
approval_workflow_service.py already expects, then calling its real publish/
schedule functions.
"""

from typing import Dict, List, Optional, Any
from datetime import datetime
from uuid import uuid4

from app.agents.social_media_manager.services.approval_workflow_service import ApprovalWorkflowService

# Platforms _publish_to_platform (approval_workflow_service.py) actually
# dispatches on. WhatsApp is excluded — it has its own separate flow_service
# and isn't handled by that dispatch at all.
SUPPORTED_PLATFORMS = ["instagram", "facebook", "x", "linkedin"]

# social_connections.platform is stored inconsistently for the X family
# (Outstand's network name is "x", but some direct-OAuth flows may store
# "twitter") — check both, matching approval_workflow_service.py's own
# `platform in ("x", "twitter")` handling.
PLATFORM_CONNECTION_LITERALS: Dict[str, List[str]] = {
    "instagram": ["instagram"],
    "facebook": ["facebook"],
    "x": ["x", "twitter"],
    "linkedin": ["linkedin"],
}


class PublishBridgeService:
    def __init__(self, db):
        self.db = db

    async def get_connected_platforms(self, user_id: str) -> List[str]:
        """
        Which platforms does this user actually have an active connection for
        right now — the exact same {"user_id", "platform", "connection_status":
        "active"} query approval_workflow_service.py runs before it ever posts.
        """
        connected: List[str] = []
        for platform, literals in PLATFORM_CONNECTION_LITERALS.items():
            doc = await self.db["social_connections"].find_one({
                "user_id": user_id,
                "platform": {"$in": literals},
                "connection_status": "active",
            })
            if doc:
                connected.append(platform)
        return connected

    async def publish_render(
        self,
        user_id: str,
        brand_id: Optional[str],
        render: Dict[str, Any],
        platform: str,
        scheduled_datetime: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Turn a completed V2 render into a real content_drafts document and hand
        it to the actual posting pipeline — published immediately or scheduled,
        via the same functions the production Drafts UI already uses.
        """
        if platform not in SUPPORTED_PLATFORMS:
            return {"success": False, "error": f"Platform '{platform}' is not supported for publishing."}

        # PRD Section 13: a render must have cleared V2's own review gate —
        # auto-tier is fine as-is; soft/mandatory must already be "approved"
        # (via the review queue's approve action, or the soft-review sweep).
        tier = render.get("review_tier", "auto")
        status = render.get("status")
        if tier != "auto" and status != "approved":
            return {
                "success": False,
                "error": f"Render is tier='{tier}' with status='{status}' — approve it in the review queue before publishing."
            }
        if render.get("needs_attention"):
            return {"success": False, "error": "Render is flagged needs_attention — cannot publish until resolved."}

        literals = PLATFORM_CONNECTION_LITERALS[platform]
        connection = await self.db["social_connections"].find_one({
            "user_id": user_id,
            "platform": {"$in": literals},
            "connection_status": "active",
        })
        if not connection:
            return {"success": False, "error": f"No active {platform} connection for this account."}

        content_data = (render.get("content_layer") or {}).get("data", {})
        caption = self._build_caption(content_data)

        final_outputs: List[str] = render.get("final_outputs") or []
        is_carousel = len(final_outputs) > 1

        draft_id = str(uuid4())
        draft_doc: Dict[str, Any] = {
            "id": draft_id,
            "user_id": user_id,
            "platform": platform,
            "content": caption,
            "status": "draft",
            "created_at": datetime.utcnow(),
            "source": "visual_engine_v2",
            "visual_engine_render_id": render.get("id"),
        }
        if brand_id:
            draft_doc["brand_id"] = brand_id

        if is_carousel:
            draft_doc["post_type"] = "carousel"
            draft_doc["slides"] = [
                {
                    "slide_number": i + 1,
                    "headline": content_data.get("headline", "") if i == 0 else "",
                    "body": content_data.get("subtext", "") if i == 0 else "",
                    "image_url": url,
                    "image_specs": {"width": 1080, "height": 1080},
                    "image_retry_count": 0,
                    "image_failed": False,
                }
                for i, url in enumerate(final_outputs)
            ]
        else:
            draft_doc["post_type"] = "feed"
            draft_doc["image_url"] = final_outputs[0] if final_outputs else None
            draft_doc["has_image"] = bool(final_outputs)

        await self.db["content_drafts"].insert_one(draft_doc)

        if scheduled_datetime:
            publish_result = await ApprovalWorkflowService.schedule_content(
                db=self.db,
                user_id=user_id,
                draft_ids=[draft_id],
                scheduled_datetime=scheduled_datetime,
            )
            new_render_status = "scheduled"
        else:
            publish_result = await ApprovalWorkflowService._trigger_immediate_publishing(
                db=self.db,
                user_id=user_id,
                draft_ids=[draft_id],
            )
            new_render_status = "published"

        await self.db["visual_engine_renders_v2"].update_one(
            {"_id": render["id"]},
            {"$set": {
                "status": new_render_status,
                "content_draft_id": draft_id,
                "published_at": datetime.utcnow() if not scheduled_datetime else None,
            }}
        )

        return {"success": True, "draft_id": draft_id, "platform": platform, "result": publish_result}

    @staticmethod
    def _build_caption(content_data: Dict[str, Any]) -> str:
        """V2 has no single 'caption' field — synthesize one from the content layer."""
        parts = [content_data.get("headline", ""), content_data.get("subtext", "")]
        if content_data.get("promo"):
            parts.append(content_data["promo"])
        if content_data.get("cta"):
            parts.append(content_data["cta"])
        return "\n\n".join(p for p in parts if p)

"""
Jane Message Service - First Message Generation & Management
PRD: URI-Social-Jane-First-Message-PRD.pdf

Core service for Jane's personalized first message feature.
Goal: Turn passive signups into active users with ONE contextual offer.

PRD Section 3: Offer First, Create When They Say Yes
- Generate message on request (not pre-generated for every signup)
- Content only generated after user accepts
- Message carries all the weight
"""
import asyncio
from datetime import datetime
from typing import Dict, Optional
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.SeasonalHookService import SeasonalHookService
from app.domain.models.jane_models import JaneFirstMessage, JaneMessageResponse
from app.domain.responses.uri_response import UriResponse


class JaneMessageService:
    """
    PRD Section 2: The Reframe - This Is Not a Welcome Message

    Don't welcome them. Notice something specific about their world,
    and offer to act on it. Arrive having already paid attention.
    """

    COLLECTION_MESSAGES = "jane_messages"
    COLLECTION_USERS = "users"
    COLLECTION_BRAND_PROFILES = "brand_profiles"

    @staticmethod
    async def should_show_first_message(user_id: str, db: AsyncIOMotorDatabase) -> bool:
        """
        Determine if user should see Jane's first message.

        Requirements:
        1. User hasn't seen first message yet
        2. User has completed brand profile (at least brand_name + industry)
        3. User is not an agency (agencies get different flow)
        """
        # Check user flags
        user = await db[JaneMessageService.COLLECTION_USERS].find_one(
            {"id": user_id},
            {"first_message_shown": 1, "role": 1}
        )

        if not user:
            return False

        # Don't show to agencies
        if user.get("role") == "agency":
            return False

        # Already shown
        if user.get("first_message_shown"):
            return False

        # Check if brand profile exists with minimum fields
        brand_profile = await db[JaneMessageService.COLLECTION_BRAND_PROFILES].find_one(
            {"user_id": user_id},
            {"brand_name": 1, "industry": 1}
        )

        if not brand_profile:
            return False

        # Must have at least brand name OR industry
        has_minimum = bool(brand_profile.get("brand_name") or brand_profile.get("industry"))

        return has_minimum

    @staticmethod
    async def generate_first_message(
        user_id: str,
        db: AsyncIOMotorDatabase
    ) -> UriResponse:
        """
        PRD Section 4: Generate Jane's first message with 4 parts:
        1. Prove it was listening (references their business/industry)
        2. Genuinely specific, timely hook
        3. Low-effort offer
        4. One clear next step

        PRD Section 3.1: Offer first, create on yes (don't pre-generate content)
        """
        # Check if should show
        should_show = await JaneMessageService.should_show_first_message(user_id, db)
        if not should_show:
            return UriResponse.get_error_response(
                "first_message_not_eligible",
                "User not eligible for first message"
            )

        # Get brand profile
        brand_profile = await db[JaneMessageService.COLLECTION_BRAND_PROFILES].find_one(
            {"user_id": user_id}
        )

        if not brand_profile:
            return UriResponse.get_error_response(
                "brand_profile_not_found",
                "Brand profile required"
            )

        brand_name = brand_profile.get("brand_name", "")
        industry = brand_profile.get("industry", "")
        location = brand_profile.get("location", "Lagos")

        # Generate hook using seasonal service (PRD Section 5)
        hook_data = SeasonalHookService.match_industry_to_season(
            industry=industry,
            business_name=brand_name
        )

        # Build full message (PRD Section 6: Tone like a friend)
        message_text = SeasonalHookService.generate_first_message(
            brand_name=brand_name,
            industry=industry,
            location=location
        )

        # Suggest platforms based on industry
        suggested_platforms = JaneMessageService._suggest_platforms(industry)

        # Create message record
        message_id = str(ObjectId())
        now = datetime.utcnow()

        jane_message = JaneFirstMessage(
            message_id=message_id,
            user_id=user_id,
            message_text=message_text,
            proof_listening=hook_data["proof_listening"],
            timely_hook=hook_data["timely_hook"],
            offer_text=hook_data["offer"],
            hook_type="seasonal",  # PRD 5.1: Seasonal backbone
            hook_source=f"Nigerian Calendar - {hook_data['event_name']}",
            seed_content=hook_data["seed_content"],
            platforms_suggested=suggested_platforms,
            status="shown",
            created_at=now,
            shown_at=now
        )

        # Save to database
        await db[JaneMessageService.COLLECTION_MESSAGES].insert_one(
            jane_message.dict()
        )

        # Mark user as having seen first message
        await db[JaneMessageService.COLLECTION_USERS].update_one(
            {"id": user_id},
            {
                "$set": {
                    "first_message_shown": True,
                    "first_message_generated_at": now
                }
            }
        )

        # Return response (PRD Section 3.2: Message carries all the weight)
        response = JaneMessageResponse(
            message_id=message_id,
            message=message_text,
            hook=hook_data["timely_hook"],
            seed_content=hook_data["seed_content"],
            platforms_suggested=suggested_platforms
        )

        return UriResponse.get_single_data_response("jane_message", response.dict())

    @staticmethod
    async def accept_first_message(
        message_id: str,
        user_id: str,
        db: AsyncIOMotorDatabase,
        platforms: Optional[list] = None
    ) -> UriResponse:
        """
        PRD Section 8: After the Yes - What Happens Next

        User accepted Jane's offer. Now we:
        1. Mark message as accepted
        2. Return seed content + platforms for content generation
        3. Let caller trigger actual content generation

        PRD Section 3.3: The content MUST deliver on the promise.
        """
        # Find message
        message = await db[JaneMessageService.COLLECTION_MESSAGES].find_one({
            "message_id": message_id,
            "user_id": user_id
        })

        if not message:
            return UriResponse.get_error_response(
                "message_not_found",
                "Message not found"
            )

        # Update message status
        await db[JaneMessageService.COLLECTION_MESSAGES].update_one(
            {"message_id": message_id},
            {
                "$set": {
                    "status": "accepted",
                    "responded_at": datetime.utcnow()
                }
            }
        )

        # Mark user as accepted
        await db[JaneMessageService.COLLECTION_USERS].update_one(
            {"id": user_id},
            {
                "$set": {
                    "first_message_accepted": True
                }
            }
        )

        # Use provided platforms or fall back to suggested
        final_platforms = platforms or message.get("platforms_suggested", ["facebook"])

        # Return data for content generation
        return UriResponse.get_single_data_response("accepted", {
            "seed_content": message.get("seed_content"),
            "platforms": final_platforms,
            "message_id": message_id
        })

    @staticmethod
    async def decline_first_message(
        message_id: str,
        user_id: str,
        db: AsyncIOMotorDatabase
    ) -> UriResponse:
        """
        PRD Section 9: If They Don't Say Yes

        Handle graceful decline. Don't nag.
        """
        # Update message status
        result = await db[JaneMessageService.COLLECTION_MESSAGES].update_one(
            {"message_id": message_id, "user_id": user_id},
            {
                "$set": {
                    "status": "declined",
                    "responded_at": datetime.utcnow()
                }
            }
        )

        if result.modified_count == 0:
            return UriResponse.get_error_response(
                "message_not_found",
                "Message not found"
            )

        return UriResponse.get_success_response("Message declined gracefully")

    @staticmethod
    async def link_draft_to_message(
        message_id: str,
        draft_id: str,
        db: AsyncIOMotorDatabase
    ) -> None:
        """
        PRD Section 9.1: Track if user publishes after accepting message.
        Link generated draft to the first message for metrics.
        """
        await db[JaneMessageService.COLLECTION_MESSAGES].update_one(
            {"message_id": message_id},
            {"$set": {"draft_id": draft_id}}
        )

    @staticmethod
    async def mark_first_content_published(
        user_id: str,
        db: AsyncIOMotorDatabase
    ) -> None:
        """
        PRD Section 9.1: KEY METRIC

        "Measure not just 'did they open/reply' but the real test:
        did they end up PUBLISHING something after the first message."
        """
        await db[JaneMessageService.COLLECTION_USERS].update_one(
            {"id": user_id},
            {
                "$set": {
                    "first_content_published": True,
                    "first_publish_timestamp": datetime.utcnow()
                }
            }
        )

    @staticmethod
    def _suggest_platforms(industry: str) -> list[str]:
        """
        Suggest platforms based on industry.
        PRD: One clear next step - don't overwhelm with choices.
        """
        industry_lower = industry.lower() if industry else ""

        # Industry -> platform mapping
        if any(kw in industry_lower for kw in ["fashion", "beauty", "clothing", "style"]):
            return ["instagram", "facebook"]
        elif any(kw in industry_lower for kw in ["b2b", "consulting", "professional", "tech"]):
            return ["linkedin"]
        elif any(kw in industry_lower for kw in ["restaurant", "food", "cafe"]):
            return ["instagram", "facebook"]
        elif any(kw in industry_lower for kw in ["event", "photography"]):
            return ["instagram"]
        else:
            # Default to Facebook (most universal in Nigeria)
            return ["facebook", "instagram"]

"""
Jane's First Message Router
PRD: URI-Social-Jane-First-Message-PRD.pdf

API endpoints for Jane's personalized first message feature.

Endpoints:
- GET /first-message - Fetch Jane's first message for current user
- POST /accept - Accept message and get content generation params
- POST /decline - Decline message gracefully
"""
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.dependencies import get_current_user, get_db_dependency
from app.services.JaneMessageService import JaneMessageService
from app.domain.models.jane_models import AcceptMessageRequest
from app.domain.responses.uri_response import UriResponse


router = APIRouter(prefix="/jane", tags=["Jane's First Message"])


@router.get("/first-message")
async def get_first_message(
    user_id: str = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    PRD Section 4: Generate Jane's first message

    Returns personalized message based on:
    - User's brand profile (industry, business name)
    - Current Nigerian seasonal context
    - Specific, timely hook

    PRD Section 3.1: Offer first, create on yes (no pre-generation)
    """
    try:
        result = await JaneMessageService.generate_first_message(
            user_id=user_id,
            db=db
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/accept")
async def accept_first_message(
    request: AcceptMessageRequest,
    user_id: str = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    PRD Section 8: After the Yes - What Happens Next

    User accepted Jane's offer. Returns:
    - seed_content: What to generate
    - platforms: Suggested platforms
    - message_id: For linking draft later

    PRD Section 3.3: The content after yes must deliver on the promise.

    Frontend should then:
    1. Call /generate/content with seed_content + platforms
    2. Navigate to drafts to show generated content
    3. Track if user publishes (key metric)
    """
    try:
        result = await JaneMessageService.accept_first_message(
            message_id=request.message_id,
            user_id=user_id,
            db=db,
            platforms=request.platforms
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/decline")
async def decline_first_message(
    message_id: str,
    user_id: str = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    PRD Section 9: If They Don't Say Yes

    Handle graceful decline:
    - No wahala — I'm here whenever you want to make something
    - Don't nag, don't repeat
    - Leave door open for later

    PRD: "A nudge that's always earned stays trusted;
    one that repeats becomes noise"
    """
    try:
        result = await JaneMessageService.decline_first_message(
            message_id=message_id,
            user_id=user_id,
            db=db
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/should-show")
async def should_show_first_message(
    user_id: str = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Check if user should see Jane's first message.

    Returns: { "should_show": boolean }

    Frontend can call this on workspace load to decide
    whether to fetch and display the welcome card.
    """
    try:
        should_show = await JaneMessageService.should_show_first_message(
            user_id=user_id,
            db=db
        )
        return UriResponse.get_single_data_response("should_show", should_show)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

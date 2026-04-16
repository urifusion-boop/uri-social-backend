"""
Notification System API Router
Aligned with Notification System PRD V1

Endpoints:
- GET /notifications - Notification history (PRD 9)
- GET /notifications/unread-count - Unread notification count
- PUT /notifications/preferences - Update notification preferences (PRD 12)
- PUT /notifications/{notification_id}/read - Mark notification as read
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional
from app.core.auth_bearer import JWTBearer
from app.services.NotificationService import notification_service
from app.database import get_db
from app.dependencies import get_db_dependency
from motor.motor_asyncio import AsyncIOMotorDatabase
from datetime import datetime

router = APIRouter(prefix="/notifications", tags=["Notifications"])


def _get_user_id(token: dict) -> str:
    """Extract user_id from JWT payload — matches JWT structure {claims: {userId: ...}}."""
    claims = token.get("claims", {})
    return claims.get("userId", "")


@router.get("/")
async def get_notifications(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    notification_type: Optional[str] = None,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """PRD 9: Retrieve notification history for the authenticated user."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    query = {"user_id": user_id}
    if notification_type:
        query["type"] = notification_type

    skip = (page - 1) * page_size

    total = await db["notifications"].count_documents(query)
    cursor = db["notifications"].find(query).sort("created_at", -1).skip(skip).limit(page_size)
    notifications = []
    async for doc in cursor:
        doc.pop("_id", None)
        notifications.append(doc)

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Notifications retrieved.",
        "responseData": {
            "notifications": notifications,
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    }


@router.get("/unread-count")
async def get_unread_count(
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Get count of unread notifications."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    count = await db["notifications"].count_documents({
        "user_id": user_id,
        "read": {"$ne": True},
    })

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Unread count retrieved.",
        "responseData": {"unread_count": count},
    }


@router.put("/{notification_id}/read")
async def mark_as_read(
    notification_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Mark a single notification as read."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    result = await db["notifications"].update_one(
        {"notification_id": notification_id, "user_id": user_id},
        {"$set": {"read": True, "read_at": datetime.utcnow()}},
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Notification not found")

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Notification marked as read.",
    }


@router.put("/preferences")
async def update_preferences(
    preferences: dict,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """PRD 12: Update user notification preferences (opt-out)."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    update_fields = {}
    if "opt_out" in preferences:
        update_fields["notification_opt_out"] = bool(preferences["opt_out"])
    if "email_notifications" in preferences:
        update_fields["email_notifications"] = bool(preferences["email_notifications"])

    if update_fields:
        await db["users"].update_one(
            {"userId": user_id},
            {"$set": update_fields},
        )

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Notification preferences updated.",
    }

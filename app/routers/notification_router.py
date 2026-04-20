"""
Notification System API Router
Aligned with Notification System PRD V1

Endpoints:
- GET /notifications - Notification history (PRD 9)
- GET /notifications/unread-count - Unread notification count
- PUT /notifications/preferences - Update notification preferences (PRD 12)
- PUT /notifications/{notification_id}/read - Mark notification as read
- PUT /notifications/mark-all-read - Mark all unread as read (batch)
- PUT /notifications/{notification_id}/archive - Archive notification
- DELETE /notifications/{notification_id} - Delete notification permanently
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional
from app.core.auth_bearer import JWTBearer
from app.services.NotificationService import notification_service
from app.database import get_db
from app.dependencies import get_db_dependency
from motor.motor_asyncio import AsyncIOMotorDatabase
from datetime import datetime
from pymongo import ASCENDING, DESCENDING

router = APIRouter(prefix="/notifications", tags=["Notifications"])

# Initialize indexes on startup
_indexes_created = False

async def _ensure_indexes(db: AsyncIOMotorDatabase):
    """Create database indexes for optimal query performance."""
    global _indexes_created
    if _indexes_created:
        return

    try:
        notifications = db["notifications"]

        # Compound index for main query (user_id + archived + created_at)
        await notifications.create_index([
            ("user_id", ASCENDING),
            ("archived", ASCENDING),
            ("created_at", DESCENDING)
        ], name="user_archived_created")

        # Index for unread count query
        await notifications.create_index([
            ("user_id", ASCENDING),
            ("read", ASCENDING),
            ("archived", ASCENDING)
        ], name="user_read_archived")

        # Index for rate limiting queries
        await notifications.create_index([
            ("user_id", ASCENDING),
            ("channel", ASCENDING),
            ("status", ASCENDING),
            ("sent_at", DESCENDING)
        ], name="rate_limit_check")

        # Index for deduplication queries
        await notifications.create_index([
            ("user_id", ASCENDING),
            ("type", ASCENDING),
            ("status", ASCENDING),
            ("sent_at", DESCENDING)
        ], name="deduplication_check")

        # Index for notification_id lookups
        await notifications.create_index([
            ("notification_id", ASCENDING),
            ("user_id", ASCENDING)
        ], name="notification_id_user", unique=True)

        print("✅ Notification indexes created successfully")
        _indexes_created = True
    except Exception as e:
        print(f"⚠️ Failed to create notification indexes: {e}")


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
    # Ensure indexes are created on first request
    await _ensure_indexes(db)

    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    query = {"user_id": user_id, "archived": {"$ne": True}}
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
        "archived": {"$ne": True},
    })

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Unread count retrieved.",
        "responseData": {"unread_count": count},
    }


@router.put("/mark-all-read")
async def mark_all_as_read(
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Mark all unread notifications as read."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    result = await db["notifications"].update_many(
        {"user_id": user_id, "read": {"$ne": True}, "archived": {"$ne": True}},
        {"$set": {"read": True, "read_at": datetime.utcnow()}},
    )

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": f"{result.modified_count} notifications marked as read.",
        "responseData": {"count": result.modified_count},
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


@router.put("/{notification_id}/archive")
async def archive_notification(
    notification_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Archive a notification (soft delete - hidden from list but not deleted). Auto-marks as read."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    result = await db["notifications"].update_one(
        {"notification_id": notification_id, "user_id": user_id},
        {"$set": {"archived": True, "archived_at": datetime.utcnow(), "read": True, "read_at": datetime.utcnow()}},
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Notification not found")

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Notification archived.",
    }


@router.put("/bulk-archive")
async def bulk_archive_notifications(
    notification_ids: list[str],
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Archive multiple notifications at once."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    if not notification_ids or len(notification_ids) == 0:
        raise HTTPException(status_code=400, detail="notification_ids is required")

    result = await db["notifications"].update_many(
        {"notification_id": {"$in": notification_ids}, "user_id": user_id},
        {"$set": {"archived": True, "archived_at": datetime.utcnow(), "read": True, "read_at": datetime.utcnow()}},
    )

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": f"{result.modified_count} notifications archived.",
        "responseData": {"count": result.modified_count},
    }


@router.delete("/bulk-delete")
async def bulk_delete_notifications(
    notification_ids: list[str],
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Permanently delete multiple notifications at once."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    if not notification_ids or len(notification_ids) == 0:
        raise HTTPException(status_code=400, detail="notification_ids is required")

    result = await db["notifications"].delete_many(
        {"notification_id": {"$in": notification_ids}, "user_id": user_id}
    )

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": f"{result.deleted_count} notifications deleted.",
        "responseData": {"count": result.deleted_count},
    }


@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
):
    """Permanently delete a notification."""
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    result = await db["notifications"].delete_one(
        {"notification_id": notification_id, "user_id": user_id}
    )

    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Notification not found")

    return {
        "status": True,
        "responseCode": 200,
        "responseMessage": "Notification deleted.",
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

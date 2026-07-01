"""
Admin-only endpoints for user management
Only accessible by admin email: urisocialingsight@gmail.com
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from typing import List, Optional
from datetime import datetime
from app.core.auth_bearer import JWTBearer
from app.database import get_db

router = APIRouter(
    prefix="/api/admin",
    tags=["Admin"],
)

ADMIN_EMAIL = "urisocialingsight@gmail.com"

async def verify_admin(jwt_payload: dict = Depends(JWTBearer())) -> dict:
    """Verify that the user is an admin"""
    if not jwt_payload:
        raise HTTPException(status_code=401, detail="Invalid authentication token")

    # Extract email from JWT claims
    claims = jwt_payload.get("claims", {})
    user_email = claims.get("email")

    if not user_email:
        raise HTTPException(status_code=401, detail="Invalid token: email not found")

    if user_email != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Access denied. Admin only.")

    return jwt_payload


@router.get("/users")
async def get_all_users(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    search: Optional[str] = None,
    sort_by: str = Query("createdAt", enum=["createdAt", "email", "name"]),
    sort_order: str = Query("desc", enum=["asc", "desc"]),
    admin_user: dict = Depends(verify_admin),
    db: AsyncIOMotorDatabase = Depends(get_db)
):
    """
    Get all users with pagination, search, and sorting
    Admin only endpoint
    """
    # Build query
    query = {}
    if search:
        query["$or"] = [
            {"email": {"$regex": search, "$options": "i"}},
            {"firstName": {"$regex": search, "$options": "i"}},
            {"lastName": {"$regex": search, "$options": "i"}},
            {"name": {"$regex": search, "$options": "i"}},
        ]

    # Count total users
    total_users = await db["users"].count_documents(query)

    # Calculate pagination
    skip = (page - 1) * limit
    total_pages = (total_users + limit - 1) // limit

    # Sort order
    sort_direction = -1 if sort_order == "desc" else 1

    # Fetch users
    cursor = db["users"].find(query).sort(sort_by, sort_direction).skip(skip).limit(limit)
    users = []

    async for user in cursor:
        user_data = {
            "id": str(user.get("_id")),
            "email": user.get("email"),
            "firstName": user.get("firstName"),
            "lastName": user.get("lastName"),
            "name": user.get("name"),
            "createdAt": user.get("createdAt"),
            "subscription_tier": user.get("subscription_tier"),
            "trial_start": user.get("trial_start"),
            "trial_end": user.get("trial_end"),
            "credits_balance": user.get("credits_balance", 0),
            "phone": user.get("phone"),
        }
        users.append(user_data)

    return {
        "users": users,
        "pagination": {
            "total": total_users,
            "page": page,
            "limit": limit,
            "total_pages": total_pages,
        }
    }


@router.get("/users/recent")
async def get_recent_users(
    days: int = Query(7, ge=1, le=90),
    admin_user: dict = Depends(verify_admin),
    db: AsyncIOMotorDatabase = Depends(get_db)
):
    """
    Get recently signed up users
    Admin only endpoint
    """
    from datetime import timedelta

    cutoff_date = datetime.utcnow() - timedelta(days=days)

    query = {
        "createdAt": {"$gte": cutoff_date}
    }

    cursor = db["users"].find(query).sort("createdAt", -1)
    users = []

    async for user in cursor:
        user_data = {
            "id": str(user.get("_id")),
            "email": user.get("email"),
            "firstName": user.get("firstName"),
            "lastName": user.get("lastName"),
            "name": user.get("name"),
            "createdAt": user.get("createdAt"),
            "subscription_tier": user.get("subscription_tier"),
            "trial_end": user.get("trial_end"),
        }
        users.append(user_data)

    return {
        "users": users,
        "count": len(users),
        "days": days
    }


@router.get("/users/{user_id}")
async def get_user_details(
    user_id: str,
    admin_user: dict = Depends(verify_admin),
    db: AsyncIOMotorDatabase = Depends(get_db)
):
    """
    Get detailed information about a specific user
    Admin only endpoint
    """
    from bson import ObjectId

    user = await db["users"].find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Get user's brand profiles
    brand_profiles = []
    async for profile in db["brand_profiles"].find({"user_id": user_id}):
        brand_profiles.append({
            "id": str(profile.get("_id")),
            "brand_name": profile.get("brand_name"),
            "industry": profile.get("industry"),
            "created_at": profile.get("created_at"),
        })

    # Get user's content count
    content_count = await db["generated_content"].count_documents({"user_id": user_id})

    # Get user's workspaces
    workspaces = []
    async for workspace in db["workspaces"].find({"owner_id": user_id}):
        workspaces.append({
            "id": str(workspace.get("_id")),
            "name": workspace.get("name"),
            "created_at": workspace.get("created_at"),
        })

    user_data = {
        "id": str(user.get("_id")),
        "email": user.get("email"),
        "firstName": user.get("firstName"),
        "lastName": user.get("lastName"),
        "name": user.get("name"),
        "phone": user.get("phone"),
        "createdAt": user.get("createdAt"),
        "subscription_tier": user.get("subscription_tier"),
        "trial_start": user.get("trial_start"),
        "trial_end": user.get("trial_end"),
        "credits_balance": user.get("credits_balance", 0),
        "brand_profiles": brand_profiles,
        "content_count": content_count,
        "workspaces": workspaces,
    }

    return user_data


@router.get("/stats")
async def get_admin_stats(
    admin_user: dict = Depends(verify_admin),
    db: AsyncIOMotorDatabase = Depends(get_db)
):
    """
    Get overall platform statistics
    Admin only endpoint
    """
    from datetime import timedelta

    # Total users
    total_users = await db["users"].count_documents({})

    # Users in last 7 days
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    new_users_7d = await db["users"].count_documents({"createdAt": {"$gte": seven_days_ago}})

    # Users in last 30 days
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    new_users_30d = await db["users"].count_documents({"createdAt": {"$gte": thirty_days_ago}})

    # Users by subscription tier
    subscription_stats = {}
    async for doc in db["users"].aggregate([
        {"$group": {"_id": "$subscription_tier", "count": {"$sum": 1}}}
    ]):
        tier = doc["_id"] or "free"
        subscription_stats[tier] = doc["count"]

    # Total content generated
    total_content = await db["generated_content"].count_documents({})

    # Total brand profiles
    total_brands = await db["brand_profiles"].count_documents({})

    # Total workspaces
    total_workspaces = await db["workspaces"].count_documents({})

    return {
        "total_users": total_users,
        "new_users_7d": new_users_7d,
        "new_users_30d": new_users_30d,
        "subscription_stats": subscription_stats,
        "total_content": total_content,
        "total_brands": total_brands,
        "total_workspaces": total_workspaces,
    }


@router.get("/users/export/emails")
async def export_user_emails(
    admin_user: dict = Depends(verify_admin),
    db: AsyncIOMotorDatabase = Depends(get_db)
):
    """
    Export all user emails
    Admin only endpoint
    """
    cursor = db["users"].find({}, {"email": 1, "firstName": 1, "lastName": 1, "createdAt": 1}).sort("createdAt", -1)
    emails = []

    async for user in cursor:
        emails.append({
            "email": user.get("email"),
            "name": f"{user.get('firstName', '')} {user.get('lastName', '')}".strip() or "N/A",
            "registered_at": user.get("createdAt"),
        })

    return {
        "emails": emails,
        "total": len(emails)
    }

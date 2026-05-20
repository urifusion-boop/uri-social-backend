"""
API Key Management Router

Dashboard endpoints for users to manage their API keys.
Uses JWT authentication (not API key auth) since this is for the web dashboard.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from typing import List
from datetime import datetime, timedelta
from bson import ObjectId

from app.dependencies import get_db_dependency
from app.core.auth_bearer import JWTBearer
from app.models.api_key import (
    APIKey,
    APIKeyScope,
    CreateAPIKeyRequest,
    CreateAPIKeyResponse,
    UpdateAPIKeyRequest
)

router = APIRouter(prefix="/social-media/api-keys", tags=["API Key Management"])


def _get_user_id(token: dict) -> str | None:
    """Extract user_id from JWT payload"""
    if not isinstance(token, dict):
        return None

    for k in ("user_id", "userId", "id", "sub"):
        v = token.get(k)
        if v:
            return str(v)

    claims = token.get("claims") or {}
    if isinstance(claims, dict):
        for k in ("userId", "user_id", "id", "sub"):
            v = claims.get(k)
            if v:
                return str(v)

    return None


@router.post("/create", response_model=CreateAPIKeyResponse, status_code=201)
async def create_api_key(
    request: CreateAPIKeyRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Create a new API key for SDK authentication

    **Returns**: The full API key (only shown once!)

    **Security**: This endpoint requires JWT authentication from the web dashboard.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token: user_id not found"
        )

    # Check if user exists
    user = await db.users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Generate API key
    api_key_string = APIKey.generate_api_key()
    key_hash = APIKey.hash_api_key(api_key_string)
    key_prefix = APIKey.get_key_prefix(api_key_string)

    # Set scopes
    scopes = request.scopes if request.scopes else APIKeyScope.get_default_scopes()

    # Validate scopes
    valid_scopes = APIKeyScope.get_all_scopes()
    for scope in scopes:
        if scope not in valid_scopes:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid scope: {scope}. Valid scopes: {', '.join(valid_scopes)}"
            )

    # Calculate expiration
    expires_at = None
    if request.expires_in_days:
        expires_at = datetime.utcnow() + timedelta(days=request.expires_in_days)

    # Create API key document
    api_key = APIKey(
        user_id=user_id,
        key_prefix=key_prefix,
        key_hash=key_hash,
        name=request.name,
        description=request.description,
        scopes=scopes,
        environment=request.environment,
        expires_at=expires_at
    )

    # Set custom rate limits if provided
    if request.rate_limit_requests_per_hour:
        api_key.rate_limits.requests_per_hour = request.rate_limit_requests_per_hour

    # Insert into database
    result = await db.api_keys.insert_one(api_key.to_dict())
    api_key.id = str(result.inserted_id)

    # Return response with full API key (only time it's shown!)
    return CreateAPIKeyResponse(
        api_key=api_key_string,  # IMPORTANT: Only shown once!
        api_key_id=api_key.id,
        key_prefix=key_prefix,
        name=api_key.name,
        scopes=api_key.scopes,
        environment=api_key.environment,
        created_at=api_key.created_at.isoformat(),
        expires_at=api_key.expires_at.isoformat() if api_key.expires_at else None,
        warning="Store this API key securely. You won't be able to see it again!"
    )


@router.get("/list")
async def list_api_keys(
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    List all API keys for the current user

    **Returns**: List of API keys (without the actual key values)
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Get all API keys for user
    keys_cursor = db.api_keys.find({"user_id": user_id}).sort("created_at", -1)
    keys = await keys_cursor.to_list(length=100)

    # Convert to public format
    public_keys = []
    for key_doc in keys:
        key_doc["_id"] = str(key_doc["_id"])
        api_key = APIKey(**key_doc)
        public_keys.append(api_key.to_public_dict())

    return {
        "api_keys": public_keys,
        "total": len(public_keys)
    }


@router.get("/{key_id}")
async def get_api_key(
    key_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Get details of a specific API key

    **Returns**: API key details (without the actual key value)
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Get API key
    key_doc = await db.api_keys.find_one({
        "_id": ObjectId(key_id),
        "user_id": user_id
    })

    if not key_doc:
        raise HTTPException(status_code=404, detail="API key not found")

    key_doc["_id"] = str(key_doc["_id"])
    api_key = APIKey(**key_doc)

    return api_key.to_public_dict()


@router.patch("/{key_id}")
async def update_api_key(
    key_id: str,
    request: UpdateAPIKeyRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Update API key details

    **Note**: Cannot update the actual key value, only metadata
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Check if key exists and belongs to user
    key_doc = await db.api_keys.find_one({
        "_id": ObjectId(key_id),
        "user_id": user_id
    })

    if not key_doc:
        raise HTTPException(status_code=404, detail="API key not found")

    # Build update dict
    update_data = {"updated_at": datetime.utcnow()}

    if request.name:
        update_data["name"] = request.name
    if request.description is not None:
        update_data["description"] = request.description
    if request.scopes:
        # Validate scopes
        valid_scopes = APIKeyScope.get_all_scopes()
        for scope in request.scopes:
            if scope not in valid_scopes:
                raise HTTPException(status_code=400, detail=f"Invalid scope: {scope}")
        update_data["scopes"] = request.scopes
    if request.status:
        if request.status == "revoked":
            update_data["status"] = "revoked"
            update_data["revoked_at"] = datetime.utcnow()
            update_data["revoked_reason"] = "Revoked by user"
        else:
            update_data["status"] = request.status
    if request.rate_limit_requests_per_hour:
        update_data["rate_limits.requests_per_hour"] = request.rate_limit_requests_per_hour

    # Update in database
    await db.api_keys.update_one(
        {"_id": ObjectId(key_id)},
        {"$set": update_data}
    )

    # Return updated key
    updated_key_doc = await db.api_keys.find_one({"_id": ObjectId(key_id)})
    updated_key_doc["_id"] = str(updated_key_doc["_id"])
    api_key = APIKey(**updated_key_doc)

    return api_key.to_public_dict()


@router.delete("/{key_id}")
async def revoke_api_key(
    key_id: str,
    reason: str = "Revoked by user",
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Revoke (soft delete) an API key

    **Note**: This doesn't delete the key, just marks it as revoked
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Check if key exists
    key_doc = await db.api_keys.find_one({
        "_id": ObjectId(key_id),
        "user_id": user_id
    })

    if not key_doc:
        raise HTTPException(status_code=404, detail="API key not found")

    # Revoke the key
    await db.api_keys.update_one(
        {"_id": ObjectId(key_id)},
        {
            "$set": {
                "status": "revoked",
                "revoked_at": datetime.utcnow(),
                "revoked_reason": reason,
                "updated_at": datetime.utcnow()
            }
        }
    )

    return {
        "success": True,
        "message": f"API key '{key_doc['name']}' has been revoked"
    }


@router.post("/{key_id}/regenerate")
async def regenerate_api_key(
    key_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Regenerate an API key (creates new key, revokes old one)

    **Returns**: New API key (only shown once!)
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Get old key
    old_key_doc = await db.api_keys.find_one({
        "_id": ObjectId(key_id),
        "user_id": user_id
    })

    if not old_key_doc:
        raise HTTPException(status_code=404, detail="API key not found")

    # Revoke old key
    await db.api_keys.update_one(
        {"_id": ObjectId(key_id)},
        {
            "$set": {
                "status": "revoked",
                "revoked_at": datetime.utcnow(),
                "revoked_reason": "Regenerated",
                "updated_at": datetime.utcnow()
            }
        }
    )

    # Generate new API key
    new_api_key_string = APIKey.generate_api_key()
    new_key_hash = APIKey.hash_api_key(new_api_key_string)
    new_key_prefix = APIKey.get_key_prefix(new_api_key_string)

    old_key_doc["_id"] = str(old_key_doc["_id"])
    old_api_key = APIKey(**old_key_doc)

    # Create new key with same settings
    new_api_key = APIKey(
        user_id=user_id,
        key_prefix=new_key_prefix,
        key_hash=new_key_hash,
        name=old_api_key.name + " (Regenerated)",
        description=old_api_key.description,
        scopes=old_api_key.scopes,
        environment=old_api_key.environment,
        rate_limits=old_api_key.rate_limits,
        expires_at=old_api_key.expires_at
    )

    # Insert new key
    result = await db.api_keys.insert_one(new_api_key.to_dict())
    new_api_key.id = str(result.inserted_id)

    return CreateAPIKeyResponse(
        api_key=new_api_key_string,
        api_key_id=new_api_key.id,
        key_prefix=new_key_prefix,
        name=new_api_key.name,
        scopes=new_api_key.scopes,
        environment=new_api_key.environment,
        created_at=new_api_key.created_at.isoformat(),
        expires_at=new_api_key.expires_at.isoformat() if new_api_key.expires_at else None,
        warning="Store this API key securely. You won't be able to see it again!"
    )


@router.get("/{key_id}/usage")
async def get_api_key_usage(
    key_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Get usage statistics for an API key
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token")

    key_doc = await db.api_keys.find_one({
        "_id": ObjectId(key_id),
        "user_id": user_id
    })

    if not key_doc:
        raise HTTPException(status_code=404, detail="API key not found")

    key_doc["_id"] = str(key_doc["_id"])
    api_key = APIKey(**key_doc)

    return {
        "key_id": api_key.id,
        "key_prefix": api_key.key_prefix,
        "usage_stats": {
            "total_requests": api_key.usage_stats.total_requests,
            "requests_today": api_key.usage_stats.requests_today,
            "requests_this_hour": api_key.usage_stats.requests_this_hour,
            "last_request_at": api_key.usage_stats.last_request_at.isoformat() if api_key.usage_stats.last_request_at else None,
            "last_request_ip": api_key.usage_stats.last_request_ip,
            "last_request_endpoint": api_key.usage_stats.last_request_endpoint
        },
        "rate_limits": {
            "requests_per_hour": api_key.rate_limits.requests_per_hour,
            "requests_per_day": api_key.rate_limits.requests_per_day,
            "image_generations_per_hour": api_key.rate_limits.image_generations_per_hour,
            "content_generations_per_hour": api_key.rate_limits.content_generations_per_hour
        }
    }

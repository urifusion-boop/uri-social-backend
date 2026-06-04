from fastapi import Request, Header
from typing import Generator, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from fastapi import Depends, HTTPException, Query
from app.database import get_db
from app.core.auth_bearer import JWTBearer


def get_db_dependency() -> Generator[AsyncIOMotorDatabase, None, None]:
    db = get_db()
    try:
        yield db
    finally:
        pass


async def get_current_workspace_context(
    token: dict = Depends(JWTBearer()),
    workspace_id: Optional[str] = Query(None, description="Workspace ID"),
) -> dict:
    """Extract user_id from JWT and optional workspace_id from query param."""
    claims = token.get("claims", {})
    user_id = claims.get("userId")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token: user_id not found")
    return {"user_id": user_id, "workspace_id": workspace_id}


async def get_current_user(token: dict = Depends(JWTBearer())) -> dict:
    """Extract and return the current user claims from the JWT token."""
    claims = token.get("claims", {})
    user_id = claims.get("userId")
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")
    return claims


async def flexible_auth(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key")
) -> dict:
    """
    Accept EITHER JWT token OR API key for authentication.
    
    Priority:
    1. X-API-Key header (SDK authentication)
    2. Authorization: Bearer header (Dashboard JWT authentication)
    
    Returns:
        dict with 'user_id' and 'auth_type' keys
        - auth_type: "api_key" or "jwt"
    
    Raises:
        HTTPException 401 if neither auth method is provided or both are invalid
    """
    from fastapi import Request, Header
    from app.middleware.api_key_auth import api_key_auth_service
    
    # Try API key first (for SDK users)
    if x_api_key:
        try:
            api_key_obj = await api_key_auth_service.verify_api_key(
                api_key=x_api_key,
                request=request
            )
            return {
                "user_id": api_key_obj.user_id,
                "auth_type": "api_key",
                "api_key_obj": api_key_obj  # Full object for scope checking if needed
            }
        except HTTPException:
            # API key failed, re-raise the error
            raise
    
    # Fallback to JWT (for dashboard users)
    authorization = request.headers.get("authorization", "")
    if authorization.startswith("Bearer "):
        jwt_bearer = JWTBearer(auto_error=True)
        try:
            jwt_payload = await jwt_bearer(request)
            claims = jwt_payload.get("claims", {})
            user_id = claims.get("userId") or claims.get("user_id")
            if not user_id:
                raise HTTPException(status_code=401, detail="User ID not found in JWT token")
            return {
                "user_id": user_id,
                "auth_type": "jwt",
                "jwt_payload": jwt_payload  # Full payload for backwards compatibility
            }
        except HTTPException:
            raise
    
    # No valid authentication provided
    raise HTTPException(
        status_code=401,
        detail="Authentication required. Provide either X-API-Key header (SDK) or Authorization: Bearer header (Dashboard)."
    )


def extract_user_id_from_auth(auth: dict) -> str:
    """
    Helper function to extract user_id from flexible auth result.
    
    Args:
        auth: Result from flexible_auth dependency
        
    Returns:
        user_id as string
    """
    return auth.get("user_id")

from fastapi import Request, Header
from typing import Generator, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase
from fastapi import Depends, HTTPException, Query, Header
from app.database import get_db
from app.core.auth_bearer import JWTBearer


def get_db_dependency() -> Generator[AsyncIOMotorDatabase, None, None]:
    db = get_db()
    try:
        yield db
    finally:
        pass


async def get_active_brand_context(
    token: dict = Depends(JWTBearer()),
    brand_id: Optional[str] = Query(None, description="Active brand id"),
    x_brand_id: Optional[str] = Header(None, alias="X-Brand-Id"),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
) -> dict:
    """
    Resolve the active brand context for a Jane request (Agency Accounts).

    Priority:
      1. Explicit brand_id (query param or X-Brand-Id header) — verified for access.
      2. The user's personal solo brand (auto-created on first use).

    Returns {"user_id", "brand_id", "agency_id"}. Raises 403 on denied access.
    """
    # Imported here to avoid circulars at module load
    from app.services.AgencyService import AgencyService
    from app.services.BrandAccountService import BrandAccountService

    claims = token.get("claims", {})
    user_id = claims.get("userId")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token: user_id not found")

    requested = brand_id or x_brand_id

    if requested and await AgencyService.user_has_access_to_brand(user_id, requested, db):
        brand = await BrandAccountService.get_brand(requested, db)
        return {
            "user_id": user_id,
            "brand_id": requested,
            "agency_id": brand.agency_id if brand else None,
        }

    # No brand requested, OR a stale/forbidden brand id (e.g. left in the browser
    # from a previous user/session) → fall back to this user's own personal brand.
    # Safe: we only ever fall back to the caller's OWN brand, never another's, so
    # there is no cross-brand data leak — it just avoids 403-ing the whole app.
    if requested:
        print(f"⚠️ brand context: user {user_id} has no access to brand {requested}; falling back to personal brand")
    personal = await BrandAccountService.get_or_create_personal_brand(user_id, db)
    return {"user_id": user_id, "brand_id": personal.brand_id, "agency_id": None}


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

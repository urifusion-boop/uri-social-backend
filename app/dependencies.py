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

    if requested:
        if not await AgencyService.user_has_access_to_brand(user_id, requested, db):
            raise HTTPException(status_code=403, detail="Access denied to brand")
        brand = await BrandAccountService.get_brand(requested, db)
        return {
            "user_id": user_id,
            "brand_id": requested,
            "agency_id": brand.agency_id if brand else None,
        }

    # No explicit brand → personal solo brand
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

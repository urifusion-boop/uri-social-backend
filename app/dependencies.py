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

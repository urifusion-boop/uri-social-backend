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
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    x_end_user_id: Optional[str] = Header(None, alias="X-End-User-ID"),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
) -> dict:
    """
    Accept EITHER JWT token OR API key for authentication.
    Supports optional multi-tenant mode via X-End-User-ID header.

    Priority:
    1. X-API-Key header (SDK authentication)
       - If X-End-User-ID provided → Multi-tenant mode (SDK client with end-users)
       - If X-End-User-ID missing → Single-tenant mode (developer's own account)
    2. Authorization: Bearer header (Dashboard JWT authentication)

    Returns:
        dict with 'user_id' and 'auth_type' keys
        - auth_type: "api_key" or "jwt"
        - If multi-tenant: includes 'sdk_client_id', 'end_user_id', 'is_multi_tenant'

    Raises:
        HTTPException 401 if neither auth method is provided or both are invalid
    """
    from fastapi import Request, Header
    from app.middleware.api_key_auth import api_key_auth_service
    from app.services.MultiTenantService import MultiTenantService

    # Try API key first (for SDK users)
    if x_api_key:
        try:
            api_key_obj = await api_key_auth_service.verify_api_key(
                api_key=x_api_key,
                request=request
            )

            # Check for multi-tenant mode (X-End-User-ID header present)
            if x_end_user_id:
                # Multi-tenant mode: SDK client with end-users
                # Get or create SDK client profile
                sdk_client = await MultiTenantService.get_or_create_sdk_client(
                    api_key_hash=api_key_obj.key_hash,
                    api_key_prefix=api_key_obj.key_prefix,
                    developer_id=api_key_obj.user_id,
                    company_name=api_key_obj.name or "SDK Client",
                    db=db
                )

                if not sdk_client:
                    raise HTTPException(
                        status_code=500,
                        detail="Failed to create SDK client profile"
                    )

                # Check if SDK client can create more end-users
                if not await MultiTenantService.can_create_end_user(sdk_client.sdk_client_id, db):
                    raise HTTPException(
                        status_code=403,
                        detail=f"SDK client has reached maximum end-user limit ({sdk_client.limits.max_end_users})"
                    )

                # Get or create end-user
                end_user = await MultiTenantService.get_or_create_end_user(
                    sdk_client_id=sdk_client.sdk_client_id,
                    external_user_id=x_end_user_id,
                    external_email=None,  # Can be passed via request body if needed
                    external_name=None,   # Can be passed via request body if needed
                    db=db
                )

                if not end_user:
                    raise HTTPException(
                        status_code=500,
                        detail="Failed to create end-user profile"
                    )

                # Update activity timestamps
                await MultiTenantService.update_sdk_client_activity(sdk_client.sdk_client_id, db)
                await MultiTenantService.update_end_user_activity(end_user.end_user_id, db)

                return {
                    "user_id": api_key_obj.user_id,  # Developer ID (for credit checks)
                    "auth_type": "api_key",
                    "api_key_obj": api_key_obj,
                    # Multi-tenant context
                    "is_multi_tenant": True,
                    "sdk_client_id": sdk_client.sdk_client_id,
                    "end_user_id": end_user.end_user_id,
                    "external_user_id": x_end_user_id,
                    "sdk_client": sdk_client,
                    "end_user": end_user
                }
            else:
                # Single-tenant mode: Developer's own account (backward compatible)
                return {
                    "user_id": api_key_obj.user_id,
                    "auth_type": "api_key",
                    "api_key_obj": api_key_obj,
                    "is_multi_tenant": False
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
                "jwt_payload": jwt_payload,
                "is_multi_tenant": False
            }
        except HTTPException:
            raise

    # No valid authentication provided
    raise HTTPException(
        status_code=401,
        detail="Authentication required. Provide either X-API-Key header (SDK) or Authorization: Bearer header (Dashboard)."
    )


async def get_sdk_context(
    request: Request,
    x_api_key: str = Header(..., alias="X-API-Key"),
    x_end_user_id: Optional[str] = Header(None, alias="X-End-User-ID"),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
) -> dict:
    """
    Auth + brand resolution for the dedicated /api/v1/* SDK surface
    (sdk_router.py). API-key-only by design — no JWT fallback should ever be
    reachable here, unlike flexible_auth/get_flexible_brand_context which
    also serves the dashboard's /social-media/* routes.

    Mirrors flexible_auth's multi-tenant branch + get_flexible_brand_context's
    resolution exactly, calling the same MultiTenantService helper, so both
    SDK entry points can't drift apart on how end-user brands are resolved.

    Returns {"api_key_obj", "user_id", "brand_id", "is_multi_tenant", "end_user_id"}.
    """
    from app.middleware.api_key_auth import api_key_auth_service
    from app.services.MultiTenantService import MultiTenantService
    from app.services.BrandAccountService import BrandAccountService

    api_key_obj = await api_key_auth_service.verify_api_key(api_key=x_api_key, request=request)

    if not x_end_user_id:
        # Backward-compatible single-tenant path — unchanged from today.
        personal = await BrandAccountService.get_or_create_personal_brand(api_key_obj.user_id, db)
        return {
            "api_key_obj": api_key_obj,
            "user_id": api_key_obj.user_id,
            "brand_id": personal.brand_id,
            "is_multi_tenant": False,
            "end_user_id": None,
        }

    sdk_client = await MultiTenantService.get_or_create_sdk_client(
        api_key_hash=api_key_obj.key_hash,
        api_key_prefix=api_key_obj.key_prefix,
        developer_id=api_key_obj.user_id,
        company_name=api_key_obj.name or "SDK Client",
        db=db,
    )
    if not sdk_client:
        raise HTTPException(status_code=500, detail="Failed to create SDK client profile")

    if not await MultiTenantService.can_create_end_user(sdk_client.sdk_client_id, db):
        raise HTTPException(
            status_code=403,
            detail=f"SDK client has reached maximum end-user limit ({sdk_client.limits.max_end_users})",
        )

    end_user = await MultiTenantService.get_or_create_end_user(
        sdk_client_id=sdk_client.sdk_client_id,
        external_user_id=x_end_user_id,
        external_email=None,
        external_name=None,
        db=db,
    )
    if not end_user:
        raise HTTPException(status_code=500, detail="Failed to create end-user profile")

    await MultiTenantService.update_sdk_client_activity(sdk_client.sdk_client_id, db)
    await MultiTenantService.update_end_user_activity(end_user.end_user_id, db)

    resolved_brand_id = await MultiTenantService.get_or_create_end_user_brand_id(
        end_user=end_user, developer_user_id=api_key_obj.user_id, db=db,
    )

    return {
        "api_key_obj": api_key_obj,
        "user_id": api_key_obj.user_id,
        "brand_id": resolved_brand_id,
        "is_multi_tenant": True,
        "end_user_id": end_user.end_user_id,
    }


def extract_user_id_from_auth(auth: dict) -> str:
    """
    Helper function to extract user_id from flexible auth result.

    Args:
        auth: Result from flexible_auth dependency

    Returns:
        user_id as string
    """
    return auth.get("user_id")


async def get_flexible_brand_context(
    brand_id: Optional[str] = Query(None, description="Active brand id"),
    x_brand_id: Optional[str] = Header(None, alias="X-Brand-Id"),
    auth: dict = Depends(flexible_auth),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
) -> dict:
    """
    Flexible version of get_active_brand_context that accepts BOTH JWT and API key.

    Priority:
      1. Explicit brand_id (query param or X-Brand-Id header) — verified for access.
      2. The user's personal solo brand (auto-created on first use).

    Returns {"user_id", "brand_id", "agency_id", "auth_type"}
    Raises 403 on denied access.
    """
    from app.services.AgencyService import AgencyService
    from app.services.BrandAccountService import BrandAccountService
    from app.services.MultiTenantService import MultiTenantService

    user_id = auth["user_id"]
    requested = brand_id or x_brand_id

    if requested and await AgencyService.user_has_access_to_brand(user_id, requested, db):
        brand = await BrandAccountService.get_brand(requested, db)
        return {
            "user_id": user_id,
            "brand_id": requested,
            "agency_id": brand.agency_id if brand else None,
            "auth_type": auth["auth_type"],
        }

    if requested:
        print(f"⚠️ brand context: user {user_id} has no access to brand {requested}; falling back to personal/end-user brand")

    # SDK multi-tenant end-user: isolate to THIS end-user's own brand, never the
    # developer's personal brand — see MultiTenantService.get_or_create_end_user_brand_id.
    if auth.get("is_multi_tenant"):
        resolved_brand_id = await MultiTenantService.get_or_create_end_user_brand_id(
            end_user=auth["end_user"], developer_user_id=user_id, db=db,
        )
        return {
            "user_id": user_id,
            "brand_id": resolved_brand_id,
            "agency_id": None,
            "auth_type": auth["auth_type"],
        }

    # JWT dashboard users and single-tenant API-key developers (no X-End-User-ID) —
    # unchanged from before.
    personal = await BrandAccountService.get_or_create_personal_brand(user_id, db)
    return {
        "user_id": user_id,
        "brand_id": personal.brand_id,
        "agency_id": None,
        "auth_type": auth["auth_type"],
    }

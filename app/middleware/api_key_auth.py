"""
API Key Authentication Middleware

Enterprise-grade authentication for SDK requests with rate limiting,
scope validation, and usage tracking.

Modified to read API keys from SDK Gateway database.
"""

from fastapi import Security, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from typing import Optional, List, Callable
from datetime import datetime, timedelta
from functools import wraps
import logging

from app.models.api_key import APIKey, APIKeyScope
from app.config.database import get_sdk_gateway_database

logger = logging.getLogger(__name__)

# Security scheme for API key in header
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class APIKeyAuthService:
    """Service for API key authentication and validation"""

    def __init__(self):
        self.sdk_gateway_db = None

    async def get_sdk_gateway_db(self):
        """Get SDK Gateway database connection for API key lookups"""
        if self.sdk_gateway_db is None:
            self.sdk_gateway_db = await get_sdk_gateway_database()
        return self.sdk_gateway_db

    async def verify_api_key(
        self,
        api_key: str,
        required_scopes: Optional[List[str]] = None,
        operation_type: str = "general",
        request: Optional[Request] = None
    ) -> APIKey:
        """
        Verify and validate API key from SDK Gateway database

        Args:
            api_key: The API key to verify
            required_scopes: List of required scopes
            operation_type: Type of operation for rate limiting
            request: FastAPI request object for IP tracking

        Returns:
            APIKey object if valid

        Raises:
            HTTPException: If authentication fails
        """
        # Check API key format (accepts urisocial_ prefix from SDK Gateway)
        if not api_key or not api_key.startswith("urisocial_"):
            logger.warning(f"Invalid API key format: {api_key[:20] if api_key else 'None'}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key format. API key must start with 'urisocial_'",
                headers={"WWW-Authenticate": "ApiKey"}
            )

        # Hash the API key to look up in database
        key_hash = APIKey.hash_api_key(api_key)

        # Find API key in SDK Gateway database
        db = await self.get_sdk_gateway_db()
        key_doc = await db.api_keys.find_one({
            "key": key_hash,        # SDK Gateway uses "key" field
            "is_active": True       # SDK Gateway uses "is_active" field
        })

        if not key_doc:
            logger.warning(f"API key not found or inactive: {APIKey.get_key_prefix(api_key)}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked API key",
                headers={"WWW-Authenticate": "ApiKey"}
            )

        # Map SDK Gateway schema to URI Social Backend schema
        # SDK Gateway schema → URI Social Backend schema
        mapped_doc = {
            "_id": str(key_doc["_id"]),
            "key_hash": key_doc.get("key"),  # SDK: "key" → "key_hash"
            "key_prefix": key_doc.get("key_prefix", ""),
            "user_id": str(key_doc.get("developer_id", "")),  # SDK: "developer_id" → "user_id"
            "name": key_doc.get("name", ""),
            "description": key_doc.get("description"),
            "scopes": key_doc.get("scopes", []),
            "status": "active" if key_doc.get("is_active", False) else "revoked",  # SDK: "is_active" → "status"
            "environment": key_doc.get("environment", "production"),
            "allowed_ips": key_doc.get("whitelisted_ips", []),  # SDK: "whitelisted_ips" → "allowed_ips"
            "allowed_origins": [],
            "created_at": key_doc.get("created_at", datetime.utcnow()),
            "updated_at": datetime.utcnow(),
            "last_used_at": key_doc.get("last_used_at"),
            "expires_at": key_doc.get("expires_at"),
            "revoked_at": None,
            "revoked_reason": None,
            "rate_limits": {
                "requests_per_hour": 1000,
                "requests_per_day": 10000,
                "image_generations_per_hour": 50,
                "content_generations_per_hour": 100
            },
            "usage_stats": {
                "total_requests": 0,
                "requests_today": 0,
                "requests_this_hour": 0,
                "last_request_at": None,
                "last_request_ip": None,
                "last_request_endpoint": None
            }
        }

        # Convert to APIKey object
        api_key_obj = APIKey(**mapped_doc)

        # Check if key is valid (not expired, status active)
        if not api_key_obj.is_valid():
            logger.warning(f"API key expired or invalid: {api_key_obj.key_prefix}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key has expired or is no longer valid",
                headers={"WWW-Authenticate": "ApiKey"}
            )

        # Check IP whitelist if configured
        if api_key_obj.allowed_ips and request:
            client_ip = request.client.host
            if client_ip not in api_key_obj.allowed_ips:
                logger.warning(f"IP not allowed: {client_ip} for key {api_key_obj.key_prefix}")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"API key not authorized for IP address: {client_ip}"
                )

        # Check required scopes (Note: SDK Gateway may use different scope names)
        if required_scopes:
            # SDK Gateway uses "admin:*" for full access
            has_admin = "admin:*" in api_key_obj.scopes
            has_required = any(scope in api_key_obj.scopes for scope in required_scopes)
            
            if not (has_admin or has_required):
                logger.warning(f"Insufficient scopes for key {api_key_obj.key_prefix}: requires {required_scopes}")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"API key does not have required permissions: {', '.join(required_scopes)}"
                )

        # Note: Rate limiting and usage tracking are handled by SDK Gateway
        # We only do read-only validation here

        logger.info(f"API key authenticated: {api_key_obj.key_prefix} for user {api_key_obj.user_id}")
        return api_key_obj


# Global service instance
api_key_auth_service = APIKeyAuthService()


async def verify_api_key(
    api_key: str = Security(api_key_header),
    request: Request = None
) -> APIKey:
    """
    FastAPI dependency for API key authentication

    Usage:
        @router.get("/endpoint")
        async def endpoint(api_key: APIKey = Depends(verify_api_key)):
            user_id = api_key.user_id
            ...
    """
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required. Include 'X-API-Key' header with your API key.",
            headers={"WWW-Authenticate": "ApiKey"}
        )

    return await api_key_auth_service.verify_api_key(
        api_key=api_key,
        request=request
    )


def require_scopes(required_scopes: List[str], operation_type: str = "general"):
    """
    Decorator for endpoints that require specific API key scopes

    Usage:
        @router.post("/generate")
        @require_scopes([APIKeyScope.CONTENT_WRITE, APIKeyScope.IMAGES_GENERATE], operation_type="content_generation")
        async def generate_content(api_key: APIKey = Depends(verify_api_key)):
            ...
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract API key from kwargs (injected by FastAPI)
            api_key = kwargs.get('api_key')
            request = kwargs.get('request')

            if not api_key:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="API key dependency not properly configured"
                )

            # Check scopes
            has_admin = "admin:*" in api_key.scopes
            has_required = api_key.has_any_scope(required_scopes)
            
            if not (has_admin or has_required):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Insufficient permissions. Required: {', '.join(required_scopes)}"
                )

            return await func(*args, **kwargs)
        return wrapper
    return decorator


async def verify_api_key_with_scopes(
    required_scopes: List[str],
    operation_type: str = "general",
    api_key: str = Security(api_key_header),
    request: Request = None
) -> APIKey:
    """
    FastAPI dependency for API key authentication with scope validation

    Usage:
        @router.post("/generate")
        async def generate(
            api_key: APIKey = Depends(
                lambda api_key=Security(api_key_header), request=None:
                    verify_api_key_with_scopes(
                        required_scopes=[APIKeyScope.CONTENT_WRITE],
                        operation_type="content_generation",
                        api_key=api_key,
                        request=request
                    )
            )
        ):
            ...
    """
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required",
            headers={"WWW-Authenticate": "ApiKey"}
        )

    return await api_key_auth_service.verify_api_key(
        api_key=api_key,
        required_scopes=required_scopes,
        operation_type=operation_type,
        request=request
    )

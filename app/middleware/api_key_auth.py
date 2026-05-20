"""
API Key Authentication Middleware

Enterprise-grade authentication for SDK requests with rate limiting,
scope validation, and usage tracking.
"""

from fastapi import Security, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from typing import Optional, List, Callable
from datetime import datetime, timedelta
from functools import wraps
import logging

from app.models.api_key import APIKey, APIKeyScope
from app.config.database import get_database

logger = logging.getLogger(__name__)

# Security scheme for API key in header
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class APIKeyAuthService:
    """Service for API key authentication and validation"""

    def __init__(self):
        self.db = None

    async def get_db(self):
        """Get database connection"""
        if not self.db:
            self.db = await get_database()
        return self.db

    async def verify_api_key(
        self,
        api_key: str,
        required_scopes: Optional[List[str]] = None,
        operation_type: str = "general",
        request: Optional[Request] = None
    ) -> APIKey:
        """
        Verify and validate API key

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
        # Check API key format
        if not api_key or not api_key.startswith("uri_sk_"):
            logger.warning(f"Invalid API key format: {api_key[:20] if api_key else 'None'}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key format. API key must start with 'uri_sk_'",
                headers={"WWW-Authenticate": "ApiKey"}
            )

        # Hash the API key to look up in database
        key_hash = APIKey.hash_api_key(api_key)

        # Find API key in database
        db = await self.get_db()
        key_doc = await db.api_keys.find_one({
            "key_hash": key_hash,
            "status": "active"
        })

        if not key_doc:
            logger.warning(f"API key not found or inactive: {APIKey.get_key_prefix(api_key)}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked API key",
                headers={"WWW-Authenticate": "ApiKey"}
            )

        # Convert to APIKey object
        key_doc["_id"] = str(key_doc["_id"])
        api_key_obj = APIKey(**key_doc)

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

        # Check required scopes
        if required_scopes:
            if not api_key_obj.has_any_scope(required_scopes):
                logger.warning(f"Insufficient scopes for key {api_key_obj.key_prefix}: requires {required_scopes}")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"API key does not have required permissions: {', '.join(required_scopes)}"
                )

        # Check rate limits
        if not api_key_obj.check_rate_limit(operation_type):
            logger.warning(f"Rate limit exceeded for key {api_key_obj.key_prefix}: {operation_type}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded for {operation_type}. Please try again later.",
                headers={
                    "Retry-After": "3600",  # Retry after 1 hour
                    "X-RateLimit-Limit": str(api_key_obj.rate_limits.requests_per_hour),
                    "X-RateLimit-Remaining": "0"
                }
            )

        # Update usage statistics
        endpoint = request.url.path if request else "unknown"
        ip_address = request.client.host if request else "unknown"
        api_key_obj.increment_usage(endpoint, ip_address)

        # Update in database
        await db.api_keys.update_one(
            {"_id": key_doc["_id"]},
            {
                "$set": {
                    "usage_stats": api_key_obj.usage_stats.model_dump(),
                    "last_used_at": api_key_obj.last_used_at,
                    "updated_at": api_key_obj.updated_at
                },
                "$inc": {
                    "usage_stats.total_requests": 1
                }
            }
        )

        logger.info(f"API key authenticated: {api_key_obj.key_prefix} for user {api_key_obj.user_id}")
        return api_key_obj

    async def reset_hourly_limits(self):
        """
        Reset hourly rate limits for all API keys
        Should be called by a cron job every hour
        """
        db = await self.get_db()
        result = await db.api_keys.update_many(
            {},
            {"$set": {"usage_stats.requests_this_hour": 0}}
        )
        logger.info(f"Reset hourly limits for {result.modified_count} API keys")

    async def reset_daily_limits(self):
        """
        Reset daily rate limits for all API keys
        Should be called by a cron job every day
        """
        db = await self.get_db()
        result = await db.api_keys.update_many(
            {},
            {"$set": {"usage_stats.requests_today": 0}}
        )
        logger.info(f"Reset daily limits for {result.modified_count} API keys")


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
            if not api_key.has_any_scope(required_scopes):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Insufficient permissions. Required: {', '.join(required_scopes)}"
                )

            # Check rate limit for specific operation type
            if not api_key.check_rate_limit(operation_type):
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Rate limit exceeded for {operation_type}",
                    headers={"Retry-After": "3600"}
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

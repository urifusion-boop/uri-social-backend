"""
SDK Gateway Authentication Middleware

Handles requests from the SDK Gateway backend that have already been authenticated.
The SDK Gateway validates API keys and forwards requests with developer context.
"""

from fastapi import Request, HTTPException, status
from typing import Optional
import hmac
import logging

from app.core.config import settings

logger = logging.getLogger(__name__)


class SDKGatewayAuth:
    """
    Middleware to handle requests forwarded from SDK Gateway.

    SDK Gateway validates API keys and adds these headers:
    - X-Internal-Service: <SDK_GATEWAY_INTERNAL_SECRET>
    - X-Developer-ID: <developer_id>
    - X-API-Key-ID: <api_key_id>
    - X-Workspace-ID: <workspace_id> (optional)

    The value of X-Internal-Service must be a real, per-deployment secret
    (SDK_GATEWAY_INTERNAL_SECRET, shared with the gateway's own config) —
    NOT a fixed, publicly-documented literal. A static, guessable value
    here would let anyone impersonate any developer directly against this
    backend by simply sending that header plus an arbitrary
    X-Developer-ID, bypassing the gateway's API-key validation, rate
    limiting, and credit-quota checks entirely.
    """

    @classmethod
    def extract_developer_context(cls, request: Request) -> Optional[dict]:
        """
        Extract developer context from SDK Gateway headers.

        Returns:
            dict with developer_id, api_key_id, workspace_id if request is from SDK Gateway
            None if not from SDK Gateway
        """
        # Check if request is from SDK Gateway. Fails closed if the secret
        # isn't configured (empty string never matches, even an empty
        # header value) — see SDK_GATEWAY_INTERNAL_SECRET in core/config.py.
        internal_service = request.headers.get("x-internal-service") or ""
        expected = settings.SDK_GATEWAY_INTERNAL_SECRET
        if not expected or not hmac.compare_digest(internal_service, expected):
            return None

        # Extract developer context
        developer_id = request.headers.get("x-developer-id")
        api_key_id = request.headers.get("x-api-key-id")
        workspace_id = request.headers.get("x-workspace-id")

        if not developer_id or not api_key_id:
            logger.warning("SDK Gateway request missing required headers")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid SDK Gateway request: missing developer context"
            )

        logger.info(f"SDK Gateway request from developer: {developer_id}, API key: {api_key_id}")

        return {
            "developer_id": developer_id,
            "api_key_id": api_key_id,
            "workspace_id": workspace_id,
            "source": "sdk_gateway",
            "authenticated": True,
        }

    @classmethod
    def is_sdk_gateway_request(cls, request: Request) -> bool:
        """Check if request is from SDK Gateway"""
        internal_service = request.headers.get("x-internal-service") or ""
        expected = settings.SDK_GATEWAY_INTERNAL_SECRET
        return bool(expected) and hmac.compare_digest(internal_service, expected)


async def get_sdk_developer_context(request: Request) -> Optional[dict]:
    """
    FastAPI dependency to extract SDK Gateway developer context.

    Usage:
        @router.get("/endpoint")
        async def endpoint(
            sdk_context: Optional[dict] = Depends(get_sdk_developer_context)
        ):
            if sdk_context:
                developer_id = sdk_context["developer_id"]
                # Handle SDK Gateway request
            else:
                # Handle normal request (JWT auth)
    """
    return SDKGatewayAuth.extract_developer_context(request)

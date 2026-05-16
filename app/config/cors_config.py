"""
CORS Configuration for SDK

Enterprise-grade CORS setup allowing SDK requests from client browsers
while maintaining security.
"""

from fastapi.middleware.cors import CORSMiddleware
from typing import List

# Allowed origins for CORS
CORS_ORIGINS = [
    "http://localhost:3000",  # Local development
    "http://localhost:5173",  # Vite default port
    "http://localhost:8080",  # Common dev port
    "https://urisocial.com",  # Production website
    "https://www.urisocial.com",  # Production website with www
    "https://app.urisocial.com",  # Production app subdomain
    "https://dashboard.urisocial.com",  # Dashboard subdomain
    "https://*.urisocial.com",  # Any subdomain (not supported by all browsers)
]

# For development/testing, you might want to allow all origins
# SECURITY WARNING: Only use in development!
ALLOW_ALL_ORIGINS = False  # Set to True for development only


def get_cors_origins() -> List[str]:
    """Get list of allowed CORS origins"""
    if ALLOW_ALL_ORIGINS:
        return ["*"]
    return CORS_ORIGINS


def configure_cors(app):
    """
    Configure CORS middleware for the FastAPI app

    This allows SDK requests from web browsers while maintaining security.

    Features:
    - Allows specific origins (configurable per environment)
    - Supports credentials (cookies, authorization headers)
    - Allows all HTTP methods
    - Allows custom headers (X-API-Key, etc.)
    - Exposes rate limit headers to clients
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=get_cors_origins(),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Authorization",
            "X-API-Key",  # Custom header for SDK authentication
            "X-Request-ID",
            "Accept",
            "Origin",
            "User-Agent",
            "DNT",
            "Cache-Control",
            "X-Requested-With",
        ],
        expose_headers=[
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            "X-RateLimit-Reset",
            "Retry-After",
            "X-Request-ID",
        ],
        max_age=600,  # Cache preflight requests for 10 minutes
    )

    return app


# Environment-specific configuration
def configure_cors_for_environment(app, environment: str = "production"):
    """
    Configure CORS based on environment

    Args:
        app: FastAPI application
        environment: "production", "staging", or "development"
    """
    if environment == "development":
        # Allow all origins in development
        global ALLOW_ALL_ORIGINS
        ALLOW_ALL_ORIGINS = True

    elif environment == "staging":
        # Add staging domains
        CORS_ORIGINS.extend([
            "https://staging.urisocial.com",
            "https://staging-app.urisocial.com",
        ])

    # Apply CORS configuration
    return configure_cors(app)


# Utility function to check if origin is allowed
def is_origin_allowed(origin: str) -> bool:
    """
    Check if an origin is allowed

    Useful for custom origin validation logic
    """
    if ALLOW_ALL_ORIGINS:
        return True

    if origin in CORS_ORIGINS:
        return True

    # Check wildcard subdomains
    if origin.endswith(".urisocial.com") and "https://*.urisocial.com" in CORS_ORIGINS:
        return True

    return False

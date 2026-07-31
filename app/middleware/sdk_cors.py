"""
CORS for the SDK's public, API-key-authenticated surface.

/social-media/*, /agency/*, and /api/v1/* are called directly from
third-party developers' own browser-side apps — arbitrary origins we can't
enumerate in the main CORSMiddleware's fixed allowlist (that allowlist is
for our own first-party, cookie/session-authenticated properties). Origin
isn't a meaningful security boundary for these routes anyway: the real
credential is the X-API-Key header, validated regardless of Origin (a
server-to-server curl call doesn't send an Origin header at all and works
fine). So this middleware opens CORS wide for just this path prefix, while
the existing strict CORSMiddleware keeps guarding every other route exactly
as before.
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

SDK_CORS_ROOTS = ("/social-media", "/agency", "/api/v1")


def _is_sdk_route(path: str) -> bool:
    # Matches both the bare root (e.g. exact "/agency", used by
    # agency.get()) and any subpath (e.g. "/agency/brands") — a plain
    # startswith(root + "/") would miss the bare-root case.
    return any(path == root or path.startswith(root + "/") for root in SDK_CORS_ROOTS)


class SdkCorsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        is_sdk_route = _is_sdk_route(path)

        if is_sdk_route and request.method == "OPTIONS":
            # Short-circuit the preflight ourselves — the strict
            # CORSMiddleware further down the stack would otherwise ignore
            # (not block, just not authorize) an origin outside its
            # allowlist, leaving the browser without the header it needs.
            requested_headers = request.headers.get("access-control-request-headers", "*")
            return Response(
                status_code=200,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": requested_headers,
                    "Access-Control-Max-Age": "3600",
                },
            )

        response = await call_next(request)

        # Only add our permissive header if the strict CORSMiddleware
        # (which still runs first — see main.py's registration order)
        # didn't already authorize this origin itself, so we never send two
        # Access-Control-Allow-Origin headers on the same response.
        if is_sdk_route and "access-control-allow-origin" not in response.headers:
            response.headers["Access-Control-Allow-Origin"] = "*"

        return response

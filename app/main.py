import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi import HTTPException
from fastapi.staticfiles import StaticFiles

from app.database import connect_to_mongo
from app.core.config import settings
from app.core.sentry_config import initialize_sentry
from app.agents.social_media_manager.routers.complete_social_manager import router as social_media_router
from app.agents.social_media_manager.routers.whatsapp_router import router as whatsapp_router
from app.agents.social_media_manager.routers.x_router import router as x_router
from app.agents.social_media_manager.routers.linkedin_router import router as linkedin_router
from app.routers.auth_router import router as auth_router
from app.routers.billing_router import router as billing_router
from app.routers.notification_router import router as notification_router

# Initialize Sentry
initialize_sentry()

# Connect to MongoDB
connect_to_mongo(settings.MONGODB_DB)

app = FastAPI(
    title="URI Agent — Social Media Manager",
    description="Standalone social media manager agent API",
    version="v1",
    contact={"name": "Uri Fusion", "email": "urifusion@gmail.com"},
    license_info={"name": "MIT License"},
)


@app.on_event("startup")
async def startup_event():
    """
    Initialize billing system on startup
    PRD Section 5: Plan Structure - Seed default subscription tiers
    """
    try:
        from app.services.SubscriptionService import subscription_service
        await subscription_service.initialize_default_tiers()
        print("✅ Subscription tiers initialized successfully")
    except Exception as e:
        print(f"⚠️  Warning: Failed to initialize subscription tiers: {e}")

    # Start notification scheduler (PRD 8: Scheduled Jobs)
    try:
        from app.services.notification_scheduler import start_notification_scheduler
        start_notification_scheduler()
        print("✅ Notification scheduler started")
    except Exception as e:
        print(f"⚠️  Warning: Failed to start notification scheduler: {e}")

# CORS
# CORS is now handled at nginx level to avoid duplicate headers
# Commenting out FastAPI CORS middleware
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_credentials=False,
#     allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
#     allow_headers=["*"],
#     expose_headers=["*"],
#     max_age=3600,
# )

# Respect X-Forwarded-For / X-Forwarded-Proto when behind a proxy/load-balancer.
# Import inside a try/except so static analyzers or environments without the
# module won't fail; if available, register the middleware so request.url is
# reconstructed using X-Forwarded-* headers (what Twilio signs).
try:
    from starlette.middleware.proxy_headers import ProxyHeadersMiddleware  # type: ignore

    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
except Exception:
    # If the environment doesn't expose ProxyHeadersMiddleware, skip it.
    ProxyHeadersMiddleware = None


@app.exception_handler(HTTPException)
def http_exception_handler(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


app.include_router(
    social_media_router,
    prefix="/social-media",
    tags=["Social Media Manager"],
)

# Include auth under /social-media prefix for frontend compatibility
app.include_router(
    auth_router,
    prefix="/social-media",
    tags=["Auth"],
)

# Also mount auth at /auth for backwards compatibility
app.include_router(auth_router)

# Include billing router under /social-media prefix (PRD: Credit-Based Pricing System)
app.include_router(
    billing_router,
    prefix="/social-media",
    tags=["Billing"],
)

# Include notification router under /social-media prefix (PRD: Notification System)
app.include_router(
    notification_router,
    prefix="/social-media",
    tags=["Notifications"],
)

# Include additional social platform routers
app.include_router(whatsapp_router)
app.include_router(x_router)
app.include_router(linkedin_router)


# Serve generated images directly from backend (avoids third-party CDN like imgBB)
_STATIC_IMAGES_DIR = "/app/static/images"
os.makedirs(_STATIC_IMAGES_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory="/app/static"), name="static")


@app.get("/")
def read_root() -> dict:
    return {"message": "URI Agent — Social Media Manager API is running"}


@app.get("/health")
def health_check() -> dict:
    return {"status": "ok", "service": "uri-agent"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)

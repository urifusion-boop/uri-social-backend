"""
Main App Integration for SDK

Add these lines to your app/main.py to enable SDK support.
"""

# ============================================
# ADD THESE IMPORTS AT THE TOP
# ============================================

from app.agents.social_media_manager.routers.sdk_router import router as sdk_router
from app.agents.social_media_manager.routers.api_key_management_router import router as api_key_mgmt_router
from app.config.cors_config import configure_cors_for_environment


# ============================================
# ADD THIS AFTER YOUR APP CREATION
# ============================================

# Configure CORS for SDK requests
app = configure_cors_for_environment(app, environment="production")  # or "development", "staging"


# ============================================
# ADD THESE ROUTER INCLUSIONS AFTER OTHER ROUTERS
# ============================================

# SDK Router - /api/v1/* endpoints for SDK authentication
app.include_router(sdk_router)

# API Key Management Router - Dashboard endpoints for managing API keys
app.include_router(api_key_mgmt_router)


# ============================================
# COMPLETE EXAMPLE OF YOUR UPDATED main.py:
# ============================================

"""
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

# ⭐ NEW: SDK Routers
from app.agents.social_media_manager.routers.sdk_router import router as sdk_router
from app.agents.social_media_manager.routers.api_key_management_router import router as api_key_mgmt_router
from app.config.cors_config import configure_cors_for_environment

from app.routers.auth_router import router as auth_router
from app.routers.billing_router import router as billing_router
from app.routers.notification_router import router as notification_router
from app.routers.bug_report_router import router as bug_report_router

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

# ⭐ NEW: Configure CORS for SDK
app = configure_cors_for_environment(app, environment=os.getenv("ENVIRONMENT", "production"))

@app.on_event("startup")
async def startup_event():
    # ... existing startup code ...
    pass

# ⭐ Existing routers
app.include_router(social_media_router, prefix="/social-media")
app.include_router(whatsapp_router, prefix="/whatsapp")
app.include_router(x_router, prefix="/x")
app.include_router(linkedin_router, prefix="/linkedin")
app.include_router(auth_router, prefix="/auth")
app.include_router(billing_router, prefix="/billing")
app.include_router(notification_router, prefix="/notifications")
app.include_router(bug_report_router, prefix="/bug-reports")

# ⭐ NEW: SDK routers
app.include_router(sdk_router)  # No prefix - uses /api/v1 internally
app.include_router(api_key_mgmt_router)  # /social-media/api-keys

@app.get("/")
async def read_root():
    return {"message": "URI Social Media Manager API", "version": "v1", "sdk_enabled": True}

@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail}
    )
"""

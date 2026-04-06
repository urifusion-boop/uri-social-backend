from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi import HTTPException

from app.database import connect_to_mongo
from app.core.config import settings
from app.core.sentry_config import initialize_sentry
from app.agents.social_media_manager.routers.complete_social_manager import router as social_media_router
from app.agents.social_media_manager.routers.whatsapp_router import router as whatsapp_router
from app.routers.auth_router import router as auth_router

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

# CORS
# allow_credentials must be False when allow_origins=["*"].
# The frontend uses Bearer tokens (Authorization header), not cookies,
# so credentials mode is not needed.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.get("/")
def read_root() -> dict:
    return {"message": "URI Agent — Social Media Manager API is running"}


@app.get("/health")
def health_check() -> dict:
    return {"status": "ok", "service": "uri-agent"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)

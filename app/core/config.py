from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    MONGODB_URI: str
    MONGODB_DB: str
    MONGODB_USER: str = ""
    MONGODB_PASSWORD: str = ""
    MONGODB_HOST: str = ""
    OPENAI_API_KEY: str
    AUTHJWT_SECRET_KEY: str

    # URI microservices
    URI_GATEWAY_BASE_API_URL: str
    URI_BACKEND_BASE_URL: str
    URI_TRANSACTIONS_BASE_URL: str = ""
    URI_TASK_MANAGER_BASE_URL: str = ""
    URI_BACKEND_USER_DETAILS: str = ""
    URI_CLIENT_ID: str = ""
    URI_CLIENT_SECRET: str = ""

    # Social platforms
    FACEBOOK_API_VERSION: str = "v21.0"
    META_API_KEY: str = ""
    META_APP_ID: str = ""
    META_APP_SECRET: str = ""
    META_SYSTEM_TOKEN: str = ""

    # Instagram Business Login (separate app credentials from the Instagram product)
    INSTAGRAM_APP_ID: str = ""
    INSTAGRAM_APP_SECRET: str = ""

    # Outstand
    OUTSTAND_API_KEY: Optional[str] = None
    OUTSTAND_WEBHOOK_SECRET: Optional[str] = None  # For verifying Outstand webhook signatures

    # X (Twitter) OAuth 1.0a — direct posting without Outstand
    X_API_KEY: Optional[str] = None         # Consumer Key
    X_API_SECRET: Optional[str] = None      # Consumer Secret
    X_OAUTH_CALLBACK_URL: Optional[str] = None  # Public backend URL, e.g. https://api.yourdomain.com/x/callback

    # LinkedIn OAuth 2.0 — direct posting
    LINKEDIN_CLIENT_ID: Optional[str] = None
    LINKEDIN_CLIENT_SECRET: Optional[str] = None
    LINKEDIN_OAUTH_CALLBACK_URL: Optional[str] = None  # e.g. https://api.yourdomain.com/linkedin/callback

    # imgBB
    IMGBB_API_KEY: Optional[str] = None

    # Google Gemini (Nano Banana 2 image generation)
    GOOGLE_GEMINI_API_KEY: Optional[str] = None

    # Google OAuth (Sign in with Google)
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None

    # SQUAD Payment Gateway (PRD Section 6.2: Payment Integration)
    SQUAD_SECRET_KEY: Optional[str] = None
    SQUAD_PUBLIC_KEY: Optional[str] = None
    SQUAD_WEBHOOK_SECRET: Optional[str] = None
    SQUAD_CALLBACK_URL: str = "https://www.urisocial.com/checkout/callback"

    # SSL (optional for local dev)
    SSL_KEY_PATH: str = ""
    SSL_CERT_PATH: str = ""

    # Env flags
    ENV: str = "Development"
    DEV_ENV: str = "Development"
    WEB_APP_URL: str = ""

    # Public-facing API base URL used for OAuth callbacks (must be reachable by browsers)
    # e.g. https://api-staging.urisocial.com  or  http://localhost:9003
    PUBLIC_API_URL: str = ""

    # Bypass flags for local development
    BYPASS_SUBSCRIPTION_CHECK: bool = False
    BYPASS_FEATURE_LIMIT_CHECK: bool = False
    LOCAL_DEV_MODE: bool = False

    # Twilio (WhatsApp)
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_WHATSAPP_FROM: str = ""  # e.g. whatsapp:+14155238886

    # Sentry (optional)
    SENTRY_DSN: Optional[str] = None

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()

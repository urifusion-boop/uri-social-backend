from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    MONGODB_URI: str
    MONGODB_DB: str
    MONGODB_USER: str = ""
    MONGODB_PASSWORD: str = ""
    MONGODB_HOST: str = ""
    OPENAI_API_KEY: str
    # Optional dedicated OpenAI key for Jane + Ads only, isolated from the shared
    # OPENAI_API_KEY the rest of the app uses. Empty → Jane Ads falls back to the
    # shared key (see the jane_ads_openai_key property).
    ADS_OPENAI_API_KEY: str = ""
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
    # URI's own Meta Business Manager id — owned by Ibukun. Until this is set,
    # the ads page-connect flow still runs and stores the page token; only the
    # final "grant URI's Business Manager ADVERTISE access" step is skipped.
    META_BUSINESS_MANAGER_ID: str = ""
    # Numeric id only, no "act_" prefix (the Marketing API adds that itself).
    META_AD_ACCOUNT_ID: str = ""
    # Working credential for real ad-account calls. A long-lived USER access token
    # (~60 day expiry) obtained via /connect/facebook-ads OAuth consent — confirmed
    # live to work where a system-user-generated token (META_SYSTEM_TOKEN) did not,
    # for reasons not yet root-caused. Needs periodic manual refresh until that's
    # sorted out. The Facebook Page connected for Click-to-WhatsApp ads.
    META_ADS_ACCESS_TOKEN: str = ""
    META_ADS_PAGE_ID: str = ""

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

    # fal.ai image/video generation
    FAL_API_KEY: Optional[str] = None

    # Pexels stock video API (b-roll fetch for video production)
    PEXELS_API_KEY: Optional[str] = None

    # Cloudinary (cleaned video + b-roll hosting for Shotstack rendering)
    CLOUDINARY_CLOUD_NAME: Optional[str] = None
    CLOUDINARY_API_KEY: Optional[str] = None
    CLOUDINARY_API_SECRET: Optional[str] = None

    # Google OAuth (Sign in with Google)
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None

    # SQUAD Payment Gateway (PRD Section 6.2: Payment Integration)
    # Production: Always use live mode for real payments
    SQUAD_MODE: str = "live"  # Options: "sandbox" or "live"

    # Sandbox credentials (for testing)
    SQUAD_SANDBOX_SECRET_KEY: Optional[str] = None
    SQUAD_SANDBOX_PUBLIC_KEY: Optional[str] = None

    # Live credentials (for production)
    SQUAD_LIVE_SECRET_KEY: Optional[str] = None
    SQUAD_LIVE_PUBLIC_KEY: Optional[str] = None

    # Webhook secret (same for both modes)
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

    # Video editing — path to royalty-free music library on the server
    # Expected layout: {MUSIC_LIBRARY_PATH}/{mood}/*.mp3  e.g. /opt/uri-music/upbeat/track1.mp3
    # Leave empty to skip background music (pipeline still runs, just without audio overlay)
    MUSIC_LIBRARY_PATH: str = ""

    # Pixabay API key — retained in config but Pixabay has no public music API
    PIXABAY_API_KEY: Optional[str] = None

    # Jamendo client ID — used to fetch CC-licensed background music by mood
    # Default is Jamendo's public demo key (works immediately, rate-limited)
    # Register a free production key at https://devportal.jamendo.com/
    JAMENDO_CLIENT_ID: str = "b6747d04"

    # ── Video Polish — Clipping API (PRD §4) ─────────────────────────────
    # Sign up at https://reap.video to get your API key (entry tier, REST API available)
    # Phase 0: test Reap, OpusClip, and Vizard on Nigerian footage before committing
    REAP_API_KEY: Optional[str] = None

    # ── Video Production — Render Engine ─────────────────────────────────
    SHOTSTACK_API_KEY: Optional[str] = None
    SUBMAGIC_API_KEY: Optional[str] = None
    ZAPCAP_API_KEY: Optional[str] = None
    OPUSCLIP_API_KEY: Optional[str] = None   # Phase 0 testing only
    VIZARD_API_KEY: Optional[str] = None      # Phase 0 testing only
    # Set to 'reap' | 'opusclip' | 'vizard' after Phase 0 Pidgin test picks a winner
    CLIPPING_API_PROVIDER: str = "reap"

    # Bypass flags for local development
    BYPASS_SUBSCRIPTION_CHECK: bool = False
    BYPASS_FEATURE_LIMIT_CHECK: bool = False
    LOCAL_DEV_MODE: bool = False

    # Twilio (WhatsApp)
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_WHATSAPP_FROM: str = ""  # e.g. whatsapp:+14155238886

    # Email (SMTP) — Notification System PRD
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = "noreply@urisocial.com"
    SMTP_FROM_NAME: str = "URI Social"
    SMTP_USE_TLS: bool = True
    ADMIN_NOTIFICATION_EMAIL: str = ""
    # Comma-separated emails allowed to see the Jane Ads admin billing report
    # (all-users ad spend / margin). Overridable by the env var of the same name;
    # the default seeds the current admins so the report works without server config.
    JANE_ADS_ADMIN_EMAILS: str = "shorekoya@gmail.com,urisocialingsight@gmail.com"

    # Sentry (optional)
    SENTRY_DSN: Optional[str] = None

    # PostHog
    POSTHOG_API_KEY: str = ""
    POSTHOG_HOST: str = "https://us.i.posthog.com"

    @property
    def jane_ads_openai_key(self) -> str:
        """The OpenAI key Jane + Ads uses — its own dedicated key when set, otherwise
        the shared one. Keeps ad usage/quota isolated from the rest of the app."""
        return self.ADS_OPENAI_API_KEY or self.OPENAI_API_KEY

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()

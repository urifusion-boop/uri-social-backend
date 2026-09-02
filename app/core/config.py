from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    MONGODB_URI: str
    MONGODB_DB: str
    MONGODB_USER: str = ""
    MONGODB_PASSWORD: str = ""
    MONGODB_HOST: str = ""
    # ARN of the DocumentDB-managed master-password secret (set only where the
    # cluster has AWS-managed rotation enabled, e.g. prod). When set, a
    # background task polls this secret and rebuilds the Mongo connection the
    # moment the password changes — the whole point being that a rotation event
    # is a non-event for anyone using the app, instead of an outage discovered
    # only once someone can't log in. See app/services/docdb_credential_refresher.py.
    DOCDB_SECRET_ARN: str = ""
    OPENAI_API_KEY: str
    AUTHJWT_SECRET_KEY: str

    # Shared secret between the SDK Gateway and this backend for internal
    # service-to-service trust (see app/middleware/sdk_gateway_auth.py).
    # Left empty by default so an unconfigured deployment fails closed
    # (no value can ever match an empty secret) rather than trusting a
    # guessable default.
    SDK_GATEWAY_INTERNAL_SECRET: str = ""

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

    # ── Video Editing Billing PRD — default rate, overridden at runtime by
    # platform_settings once an admin sets one (see VideoBillingService.get_video_edit_rate) ──
    VIDEO_EDIT_CREDITS_PER_MINUTE: int = 4
    # Comma-separated allowlist gating PATCH /video-editing/pricing. Empty = nobody can change it.
    BILLING_ADMIN_EMAILS: str = ""
    # Comma-separated allowlist gating the /admin/* user-management router (view all
    # users, adjust credits/trial). Defaults to the same email the frontend's
    # AdminService.isAdmin() already hardcodes, so this matches today's real
    # security posture rather than silently locking everyone out until a .env
    # change ships — extend via env var for additional admins, never widen this
    # default to something guessable.
    ADMIN_EMAILS: str = "urisocialingsight@gmail.com"
    # Set to 'reap' | 'opusclip' | 'vizard' after Phase 0 Pidgin test picks a winner
    CLIPPING_API_PROVIDER: str = "reap"

    # jane-whatsapp-reply — shared secret this backend presents (as
    # X-Internal-Service) when proxying support-team requests to jane's own
    # /internal/* endpoints (see app/services/JaneEscalationClient.py). Must match
    # jane-whatsapp-reply's own JANE_WA_INTERNAL_SECRET exactly. Empty by default
    # so an unconfigured deployment fails closed on jane's side, same reasoning as
    # SDK_GATEWAY_INTERNAL_SECRET above.
    JANE_WA_INTERNAL_SECRET: str = ""
    JANE_WA_BASE_URL: str = "https://jane-whatsapp.urisocial.com"

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

    # Sentry (optional)
    SENTRY_DSN: Optional[str] = None

    # PostHog
    POSTHOG_API_KEY: str = ""
    POSTHOG_HOST: str = "https://us.i.posthog.com"


    # SDK Gateway Database (for API key authentication only)
    SDK_GATEWAY_MONGODB_URI: Optional[str] = None
    SDK_GATEWAY_DB: str = "sdk-gateway"
    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()


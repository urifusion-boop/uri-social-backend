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
    # sorted out. This is URI's OWN token, used to run every ad-account write for
    # every brand.
    META_ADS_ACCESS_TOKEN: str = ""
    # URI's own Facebook Page — every brand's ads run from this one Page (the
    # intended architecture: what distinguishes one brand's ads from another's is
    # the WhatsApp number leads land in and the creative, never a separate Page
    # identity). Must be a Page URI's Business Manager actually has ADVERTISE
    # access to (ads_connection.py's business_manager_shared tracks this per any
    # per-brand Page a client separately connects, but this shared Page's own
    # access has to be confirmed manually in Meta Business Settings).
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

    # Google Ads (Jane + Ads Google adapter) — a DEDICATED OAuth client, deliberately
    # separate from GOOGLE_CLIENT_ID/SECRET above (that pair is Sign-in-with-Google
    # only: a one-shot code->token->userinfo exchange with no refresh-token persistence,
    # almost certainly the wrong Google Cloud project/consent scope for the Ads API).
    # See app/agents/jane_ads/google_ads_connection.py.
    GOOGLE_ADS_CLIENT_ID: str = ""
    GOOGLE_ADS_CLIENT_SECRET: str = ""
    # Issued once per Manager Account (MCC), starts at Test Account Access tier until
    # Basic Access is approved.
    GOOGLE_ADS_DEVELOPER_TOKEN: str = ""
    # URI's own Manager Account (MCC) customer id, digits only, no dashes — the account
    # manager-link requests originate FROM and client accounts get created UNDER.
    # Analogous to META_BUSINESS_MANAGER_ID, NOT to META_AD_ACCOUNT_ID/META_ADS_PAGE_ID:
    # there is deliberately no "default ad account" setting here and never should be —
    # every real call operates against a specific brand's own customer_id, resolved
    # per-brand via google_ads_connection.resolve_customer_id_for_launch(), never a
    # shared/default one (see tests/test_jane_ads_google_no_fallback_account.py, which
    # fails the build if a shared- or default-account-shaped Google Ads setting ever
    # appears anywhere in the codebase — Meta's own META_ADS_PAGE_ID shared-fallback
    # is the exact mistake this guards against repeating).
    GOOGLE_ADS_MCC_CUSTOMER_ID: str = ""
    # v17 sunset 2025-06-04 (Google Ads API versions live ~1 year) — every request
    # against it now 404s at Google's edge with a generic HTML error page instead of
    # a JSON API error, which is what actually broke create-account/link-existing on
    # staging. Bump this periodically; check developers.google.com/google-ads/api/docs/sunset-dates.
    GOOGLE_ADS_API_VERSION: str = "v24"

    # TikTok Marketing API (Jane + Ads, Phase 1 — see app/agents/jane_ads/adapters/tiktok.py)
    # Unlike Meta/Google, Phase 1 does NOT run ads from the brand's own TikTok presence
    # (that's Spark Ads, which needs a manual per-video authorization code from the
    # creator — no OAuth path exists for it). Every brand's video ad is uploaded to and
    # launched from this one shared URI-owned advertiser account instead — the same
    # already-established pattern META_ADS_PAGE_ID uses for Meta, just made explicit
    # here from the start rather than growing into it.
    TIKTOK_ADS_ADVERTISER_ID: str = ""
    TIKTOK_ADS_ACCESS_TOKEN: str = ""
    TIKTOK_ADS_API_VERSION: str = "v1.3"
    # Confirmed live (2026-08-26) against a Sandbox Ad Account: sandbox tokens
    # 401/permission-error against the production host below and only work
    # against sandbox-ads.tiktok.com — a real host split TikTok doesn't
    # document clearly. Override to "https://sandbox-ads.tiktok.com" in
    # .env.staging while testing against a Sandbox Ad Account; leave unset
    # (production default) once TIKTOK_ADS_ADVERTISER_ID is a real, funded
    # account.
    TIKTOK_ADS_API_BASE: str = "https://business-api.tiktok.com"

    # TikTok Login Kit + Content Posting API — the "urisocial" app's own
    # credentials, distinct from TIKTOK_ADS_* above (a separate TikTok product/
    # app entirely). Powers direct organic posting (FILE_UPLOAD mode — no
    # domain verification needed, unlike PULL_FROM_URL) as an alternative to
    # the existing Outstand-mediated TikTok connection. See
    # app/agents/social_media_manager/services/tiktok_direct_service.py.
    TIKTOK_APP_CLIENT_KEY: str = ""
    TIKTOK_APP_CLIENT_SECRET: str = ""

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

    # Video Editing Billing PRD §4: credits charged per billable minute of
    # final video edited (rounded up to the next full minute). Configurable
    # via env var rather than hard-coded in the billing logic.
    VIDEO_EDIT_CREDITS_PER_MINUTE: int = 4

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

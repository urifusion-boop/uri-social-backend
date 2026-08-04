"""
Jane + Ads — per-brand Meta ads connection (Per-Brand Page Connection plan).

Ads must publish from the CLIENT's own Facebook Page, with the CLIENT's own WhatsApp
number as the message destination — not a shared URI Page. This module resolves,
per brand, whether that's actually possible right now, and if not, exactly which of
six explicit states blocks it (never inferred from a single boolean).

The OAuth grant itself already exists (`/social-media/connect/facebook-ads/initiate` +
`/callback` + `/finalize` in complete_social_manager.py) — it requests the right scopes
(ads_management, pages_manage_ads, business_management, ...), exchanges for a long-lived
token, and already shares the page with URI's Business Manager. This module is the
missing piece: reading that connection back out, per-brand, with real state and a live
health check — and refusing to fall back to a shared page when it's absent.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

import httpx

from app.core.config import settings

CONNECTIONS = "social_connections"

# The exact scopes /connect/facebook-ads/initiate requests (complete_social_manager.py).
# Every "facebook_ads" connection was granted this same fixed set, so a connection's mere
# existence implies the request was made for all of them — live-verified via
# GET /me/permissions, not just assumed, since Meta's dialog can grant a subset.
REQUIRED_ADS_SCOPES = {
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_metadata",
    "business_management",
    "pages_manage_ads",
    "ads_management",
}


class ConnectionState(str, Enum):
    NONE = "none"                        # no Meta connection of any kind
    CONTENT_ONLY = "content_only"        # posting connected; no ads permission
    ADS_NO_WHATSAPP = "ads_no_whatsapp"  # ads permission granted; WhatsApp not linked
    READY = "ready"                       # ads permission + WhatsApp linked
    EXPIRED = "expired"                   # token invalid or a required scope missing
    NO_PAGE = "no_page"                   # business has no Facebook Page at all


def _bm_share_was_already_done(ads: dict) -> bool:
    """Meta rejects re-sharing a Page that URI's Business Manager ALREADY has with
    "You are trying to assign a duplicated asset to this agency". That reads like a
    failure and gets stored as business_manager_shared=False, but it actually means
    the Page is already assigned — i.e. exactly the state we wanted.

    Live-confirmed the hard way: a Page carrying this error advertised perfectly, while
    a different Page with business_manager_shared=True was rejected by Meta at ad-creative
    time. Treating the duplicate-asset case as "not shared" would wrongly push a working
    brand Page back to the shared URI Page."""
    err = (ads.get("business_manager_error") or "").lower()
    return "duplicated asset" in err or "already" in err


class AdsConnectionRequired(Exception):
    """Raised when a campaign can't be built because the brand's Meta ads connection
    isn't READY. Carries the specific state so the caller can map it to the matching
    prompt — never a generic error."""

    def __init__(self, state: ConnectionState, page_name: str = "") -> None:
        super().__init__(state.value)
        self.state = state
        self.page_name = page_name


def _brand_scope(user_id: Optional[str], brand_id: Optional[str]) -> dict:
    """Matches the scoping the rest of social_media_manager already uses: an agency
    brand matches strictly by brand_id; a personal brand matches by user_id (connections
    made before any brand_id existed) OR that brand_id."""
    if brand_id and not str(brand_id).startswith("brnd_personal_"):
        return {"brand_id": brand_id}
    return {"$or": [{"user_id": user_id}, {"brand_id": brand_id}]} if brand_id else {"user_id": user_id}


async def get_content_connection(db, user_id: Optional[str], brand_id: Optional[str]) -> Optional[dict]:
    """The brand's organic posting connection (Outstand or facebook_direct_oauth) —
    used only to distinguish CONTENT_ONLY (already connected for posting, needs the
    upgrade prompt) from NONE (never connected anything)."""
    if db is None or not (user_id or brand_id):
        return None
    return await db[CONNECTIONS].find_one(
        {"platform": "facebook", **_brand_scope(user_id, brand_id)},
        sort=[("connected_at", -1)],
    )


async def get_ads_connection(db, user_id: Optional[str], brand_id: Optional[str]) -> Optional[dict]:
    """The brand's ads-scoped Facebook connection (platform="facebook_ads"), the one
    that actually carries a page_access_token with ads_management granted."""
    if db is None or not (user_id or brand_id):
        return None
    return await db[CONNECTIONS].find_one(
        {"platform": "facebook_ads", "connection_status": "active", **_brand_scope(user_id, brand_id)},
        sort=[("connected_at", -1)],
    )


async def verify_token_live(page_id: str, page_access_token: str, user_access_token: str = "") -> tuple[bool, set[str]]:
    """Live health check against the real Graph API — the ONLY reliable way to know a
    long-lived Page token is still valid and which of the granted scopes are actually
    still active (Meta can silently revoke via a password change, an app review, or the
    client removing the permission in their own Facebook settings). Returns
    (token_is_valid, granted_scope_names). Never raises — an unreachable API just
    reports invalid, matching 'never let a token failure silently stop delivery'.

    GET /me/permissions only returns meaningful data for a USER access token — called
    with a Page token it just returns an empty list every time (confirmed live), which
    silently broke this check for every connection until user_access_token was added.
    Falls back to the page token if no user token is on file (older connections from
    before this fix), which will under-report scopes rather than over-trust them."""
    graph_base = f"https://graph.facebook.com/{settings.FACEBOOK_API_VERSION}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            page_resp = await client.get(f"{graph_base}/{page_id}",
                                         params={"access_token": page_access_token, "fields": "id"})
            if "error" in page_resp.json():
                return False, set()
            perms_resp = await client.get(f"{graph_base}/me/permissions",
                                          params={"access_token": user_access_token or page_access_token})
            perms_data = perms_resp.json()
            granted = {p["permission"] for p in perms_data.get("data", []) if p.get("status") == "granted"}
            return True, granted
    except Exception as e:
        print(f"[AdsConnection] token verify error: {e}", flush=True)
        return False, set()


async def resolve_connection_state(
    db, user_id: Optional[str], brand_id: Optional[str], *, live_check: bool = True,
    require_whatsapp: bool = True,
) -> tuple[ConnectionState, Optional[dict]]:
    """The full state machine (Per-Brand Page Connection plan §3). Returns
    (state, ads_connection_doc_or_None). `live_check=False` skips the network round
    trip (used by fast, non-blocking reads like a status badge) — the real pre-flight
    gate before a campaign is built always uses the default True. `require_whatsapp=False`
    is for campaign goals that never route anywhere via WhatsApp (e.g. a followers/
    engagement campaign) — the Page connection alone is READY; there's nothing to link."""
    ads = await get_ads_connection(db, user_id, brand_id)
    if not ads:
        content = await get_content_connection(db, user_id, brand_id)
        return (ConnectionState.CONTENT_ONLY if content else ConnectionState.NONE), None

    if not ads.get("page_id"):
        return ConnectionState.NO_PAGE, ads

    # The OAuth callback (complete_social_manager.py) tries to grant URI's Business
    # Manager ADVERTISE access to the page at connect time and records whether that
    # actually succeeded — live-diagnosed real case: it can silently fail (e.g. Meta
    # rejects the share as "duplicated asset") while every other part of the
    # connection looks fine, and every real ad-account write (launch, pause/resume)
    # goes through URI's own shared ad-account token, which needs that grant to act
    # on this page. Missing the key entirely (older connections from before this was
    # tracked) is treated as shared, not blocking.
    if ads.get("business_manager_shared") is False and not _bm_share_was_already_done(ads):
        ads = dict(ads)
        ads["_business_manager_error"] = ads.get("business_manager_error") or ""
        return ConnectionState.EXPIRED, ads

    if live_check:
        valid, granted = await verify_token_live(
            ads["page_id"], ads.get("page_access_token", ""), ads.get("user_access_token", ""),
        )
        if not valid or not REQUIRED_ADS_SCOPES.issubset(granted):
            # A live-diagnosed real case: the token itself was valid but Facebook's own
            # consent screen didn't grant every requested permission (confirmed live —
            # a client reconnected and still came back missing ads_management etc.).
            # Stash which ones so the caller can show a precise reason instead of a
            # generic "needs reconnecting" — transient (not persisted), so a shallow
            # copy first.
            ads = dict(ads)
            ads["_missing_scopes"] = [] if not valid else sorted(REQUIRED_ADS_SCOPES - granted)
            return ConnectionState.EXPIRED, ads

    if require_whatsapp and not ads.get("whatsapp_page_linked"):
        return ConnectionState.ADS_NO_WHATSAPP, ads

    return ConnectionState.READY, ads


async def resolve_ads_page_for_launch(
    db, user_id: Optional[str], brand_id: Optional[str], *, require_whatsapp: bool = True,
) -> dict:
    """The pre-flight gate: returns {"page_id", "whatsapp_number", "page_name"} or
    raises AdsConnectionRequired — called before ANY campaign plan is built, never
    at launch, so a client is never let all the way through a conversation only to
    hit a wall.

    Prefers the brand's OWN connected Facebook Page, so their ads publish under their
    own name instead of every client's ad reading "URI". A brand gets one by connecting
    via /connect/facebook-ads (that grant requests ads_management + pages_manage_ads +
    business_management and shares the Page with URI's Business Manager, which is what
    lets the shared ad account advertise for it). Brands that haven't connected still
    fall back to URI's shared Page, so nobody is blocked from launching.

    The WhatsApp destination is deliberately NOT taken from the Page connection: ads
    route through a plain wa.me link (see adapters/meta.py), so the number only has to
    be saved against the brand (GET/PUT /jane-ads/whatsapp) and never linked to a Page
    inside Meta. That's why whatsapp_page_linked is irrelevant here and the connection
    is checked with require_whatsapp=False. `require_whatsapp=False` on THIS function is
    for a goal that never routes via WhatsApp at all (e.g. followers) — `whatsapp_number`
    in the returned dict may then be empty."""
    from .whatsapp import get_brand_whatsapp

    wa_number = await get_brand_whatsapp(db, brand_id) if require_whatsapp else ""
    if require_whatsapp and not wa_number:
        raise AdsConnectionRequired(ConnectionState.ADS_NO_WHATSAPP)

    # The brand's own Page wins when it's usable. WhatsApp linkage is not part of
    # "usable" any more (wa.me needs none), hence require_whatsapp=False below.
    state, ads = await resolve_connection_state(db, user_id, brand_id, require_whatsapp=False)
    if state == ConnectionState.READY and (ads or {}).get("page_id"):
        return {
            "page_id": ads["page_id"],
            "whatsapp_number": wa_number,
            "page_name": ads.get("account_name", ""),
        }

    if not settings.META_ADS_PAGE_ID:
        # No brand Page AND no shared Page configured — surface the brand's real state
        # (NONE/CONTENT_ONLY/EXPIRED/NO_PAGE) so the caller can prompt precisely.
        raise AdsConnectionRequired(state, (ads or {}).get("account_name", ""))

    return {
        "page_id": settings.META_ADS_PAGE_ID,
        "whatsapp_number": wa_number,
        "page_name": "URI Social",
    }


async def run_token_health_check(db) -> dict:
    """Daily proactive check (Per-Brand Page Connection plan §8). The pre-flight
    gate in resolve_connection_state already live-checks a token at the moment a
    NEW campaign is built — this job is what catches an ALREADY-LAUNCHED campaign
    whose token dies mid-flight: it pauses that brand's running campaigns (so a
    dead connection doesn't silently stop delivering while still burning wallet
    balance) and notifies the owning user once, not on every run."""
    from app.core.config import settings
    from app.services.NotificationService import notification_service
    from .adapters.meta import MetaAdPlatformAdapter, MetaAPIError

    checked = paused = notified = 0
    connections = await db[CONNECTIONS].find(
        {"platform": "facebook_ads", "connection_status": "active"}
    ).to_list(length=500)

    for conn in connections:
        page_id = conn.get("page_id")
        token = conn.get("page_access_token", "")
        if not page_id or not token:
            continue
        checked += 1
        valid, granted = await verify_token_live(page_id, token, conn.get("user_access_token", ""))
        if valid and REQUIRED_ADS_SCOPES.issubset(granted):
            continue

        brand_id = conn.get("brand_id")
        user_id = conn.get("user_id")
        if not user_id:
            continue

        if settings.META_AD_ACCOUNT_ID and settings.META_ADS_ACCESS_TOKEN:
            adapter = MetaAdPlatformAdapter(db, access_token=settings.META_ADS_ACCESS_TOKEN)
            campaigns = await db["jane_ads_meta_campaigns"].find(
                {"brand_id": brand_id, "paused_for_token_health": {"$ne": True}}
            ).to_list(length=200)
            for camp in campaigns:
                try:
                    await adapter.set_delivery(camp["campaign_id"], False)
                except MetaAPIError as e:
                    print(f"[AdsConnection] pause failed for {camp['campaign_id']}: {e}", flush=True)
                    continue
                await db["jane_ads_meta_campaigns"].update_one(
                    {"campaign_id": camp["campaign_id"]},
                    {"$set": {"paused_for_token_health": True}},
                )
                paused += 1

        if conn.get("token_expired_notified"):
            continue
        page_name = conn.get("account_name") or "your Facebook Page"
        await notification_service._log_notification(
            user_id=user_id,
            notification_type="ads_connection_expired",
            channel="email",
            subject="Your ad campaigns need reconnecting",
            status="sent",
            metadata={
                "brand_id": brand_id,
                "message": (
                    f"We paused your running ads — the connection to {page_name} has expired or "
                    "lost a permission. Reconnect it in Settings to resume."
                ),
            },
        )
        await db[CONNECTIONS].update_one({"id": conn["id"]}, {"$set": {"token_expired_notified": True}})
        notified += 1

    return {"checked": checked, "paused": paused, "notified": notified}


async def set_whatsapp_number(db, user_id: Optional[str], brand_id: Optional[str], number: str) -> str:
    """Store the brand's WhatsApp number against their ads connection. This does NOT
    itself prove the number is linked to their Page in Meta — that's inherently a
    manual, user-facing OTP step done in Meta's own Page Settings (no partner API
    exists to trigger it on someone's behalf) — but the very next real ad-set create
    will surface Meta's own specific rejection ('this WhatsApp phone number is not
    linked to your account') if it isn't, which is the actual, reliable verification."""
    from .whatsapp import normalize_wa_number

    normalized = normalize_wa_number(number)
    if not normalized:
        raise ValueError("That WhatsApp number doesn't look right — please type it in full, e.g. 0803 123 4567.")
    ads = await get_ads_connection(db, user_id, brand_id)
    if not ads:
        raise AdsConnectionRequired(ConnectionState.NONE)
    await db[CONNECTIONS].update_one(
        {"id": ads["id"]},
        {"$set": {"whatsapp_number": normalized, "whatsapp_page_linked": True}},
    )
    return normalized

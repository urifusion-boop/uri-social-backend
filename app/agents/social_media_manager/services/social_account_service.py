from typing import Dict, List, Any, Optional
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.domain.responses.uri_response import UriResponse
from app.core.config import settings

# force_account_selection maps to a real per-network re-auth override on
# Outstand's side (disable_auto_auth for TikTok, auth_type=reauthenticate for
# Facebook, force_reauth for Instagram) — for Facebook specifically that means
# Meta's own password-reentry checkpoint on every single connect, even for an
# already-logged-in browser. It was turned on for every network to fix one
# confirmed incident (TikTok silently attaching the wrong already-logged-in
# account with no picker shown) but the added friction elsewhere was never
# needed to fix that bug, so it's scoped to just the network it fixed.
NETWORKS_REQUIRING_ACCOUNT_SELECTION = {"tiktok"}


async def _release_inbox_claim(
    db: AsyncIOMotorDatabase,
    connection: Optional[Dict[str, Any]],
    user_id: str,
    brand_id: Optional[str],
) -> None:
    """Drop the inbox channel rows this connection provided.

    An inbox channel row is a claim on a provider account, and routing refuses
    an account two workspaces claim. Leaving the row behind after a disconnect
    means this workspace keeps claiming a Page it no longer connects, which
    silently breaks the inbox for whoever connects it next. Best-effort: a
    disconnect must still succeed for the customer if this fails.
    """
    if not connection:
        return

    from app.agents.inbox.channels import release_channels
    from app.models.brand_account import BrandAccount

    workspace_id = brand_id or BrandAccount.personal_brand_id(user_id)
    for external_id in (connection.get("page_id"), connection.get("ig_user_id")):
        if not external_id:
            continue
        try:
            await release_channels(db, workspace_id, external_account_id=str(external_id))
        except Exception as e:
            print(f"[Disconnect] could not release inbox claim on {external_id}: {e}",
                  flush=True)


def _brand_scope(user_id: str, brand_id: Optional[str]) -> Dict[str, Any]:
    """The active brand's connections: a personal brand's are the user's own (excluding any agency
    brand's), an agency brand's are matched by brand_id alone."""
    from app.models.brand_account import BrandAccount
    personal_bid = BrandAccount.personal_brand_id(user_id)
    if (not brand_id) or brand_id == personal_bid:
        return {"user_id": user_id,
                "$or": [{"brand_id": {"$exists": False}}, {"brand_id": None}, {"brand_id": personal_bid}]}
    return {"brand_id": brand_id}


def _tombstone_scope(user_id: str, brand_id: Optional[str]) -> Dict[str, Any]:
    from app.models.brand_account import BrandAccount
    if (not brand_id) or brand_id == BrandAccount.personal_brand_id(user_id):
        return {"user_id": user_id}
    return {"brand_id": brand_id}


class SocialAccountService:

    # -------------------------------------------------------------------------
    # 1. Initiate OAuth flow — returns auth URLs for each requested platform
    # -------------------------------------------------------------------------

    # -------------------------------------------------------------------------
    # 2. Get pending connection — returns pages available for the user to select
    # -------------------------------------------------------------------------

    # -------------------------------------------------------------------------
    # 3. Finalize connection — selects pages and stores accounts
    # -------------------------------------------------------------------------

    # -------------------------------------------------------------------------
    # 4. List connections — queries Outstand directly for live status
    # -------------------------------------------------------------------------

    @staticmethod
    async def get_user_connections(
        db: AsyncIOMotorDatabase,
        user_id: str,
        brand_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Return all social accounts connected for the active brand.

        Agency Accounts isolation: connections are scoped per brand. A solo
        user's personal brand keeps its legacy connections (tenant_id=user_id);
        each agency brand has its own isolated set (tenant_id=brand_id).
        All connections are direct ones held in the local DB.
        """
        from app.models.brand_account import BrandAccount

        is_personal = (not brand_id) or brand_id == BrandAccount.personal_brand_id(user_id)
        personal_bid = BrandAccount.personal_brand_id(user_id)
        # Personal brand: exclude docs that belong to an agency brand (they store
        # user_id too, so a plain user_id filter would leak them across brands).
        # Agency brand: match strictly by brand_id only.
        if is_personal:
            local_filter: Dict[str, Any] = {
                "user_id": user_id,
                "$or": [
                    {"brand_id": {"$exists": False}},
                    {"brand_id": None},
                    {"brand_id": personal_bid},
                ],
            }
        else:
            local_filter = {"brand_id": brand_id}
        local_filter["connection_status"] = "active"

        by_platform: Dict[str, list] = {}

        # Accounts the customer explicitly disconnected (see disconnect_account).
        tombstone_scope = {"user_id": user_id} if is_personal else {"brand_id": brand_id}
        disconnected_ids = {
            doc.get("account_id") or doc.get("outstand_account_id")
            async for doc in db["disconnected_accounts"].find(
                tombstone_scope, {"account_id": 1, "outstand_account_id": 1}
            )
        }

        try:
            # Every connection is a direct one held in our own database. An account still
            # recorded as connected through Outstand (retired) can't be posted to: it is listed
            # as needing a reconnect (unless the brand has connected that network directly
            # since), and its network does not count as connected.
            expired: Dict[str, Any] = {}
            retired: Dict[str, list] = {}
            async for doc in db["social_connections"].find(local_filter):
                platform = doc.get("platform", "unknown")
                key = doc.get("id") or doc.get("outstand_account_id")
                if key and key in disconnected_ids:
                    continue
                entry = {
                    "id": doc.get("id"),
                    "platform": platform,
                    "connected_via": doc.get("connected_via"),
                    "username": doc.get("username"),
                    "account_name": doc.get("account_name"),
                    "profile_picture_url": doc.get("profile_picture_url"),
                    "is_active": True,
                    "connected_at": doc.get("connected_at"),
                    "page_name": doc.get("account_name"),
                    "ig_user_id": doc.get("ig_user_id"),
                }
                if doc.get("connected_via") == "outstand":
                    entry.update({
                        "is_active": False,
                        "needs_reconnect": True,
                        "reconnect_reason": "Reconnect this account to keep posting to it.",
                    })
                    retired.setdefault(platform, []).append(entry)
                    continue
                if doc.get("needs_reconnect"):
                    expired[key or f"platform:{platform}"] = doc.get("reconnect_reason")
                    expired.setdefault(f"platform:{platform}", doc.get("reconnect_reason"))
                by_platform.setdefault(platform, []).append(entry)

            for platform, accs in by_platform.items():
                for acc in accs:
                    key = acc.get("id")
                    hit = key in expired if key else False
                    # A flagged brand doc with no account id covers that network's card.
                    if not hit and f"platform:{platform}" in expired and len(accs) == 1:
                        hit, key = True, f"platform:{platform}"
                    if hit:
                        acc["is_active"] = False
                        acc["needs_reconnect"] = True
                        acc["reconnect_reason"] = expired.get(key)

            connected_platforms = list(by_platform.keys())
            for platform, accs in retired.items():
                if platform not in by_platform:
                    by_platform[platform] = accs

            total = sum(len(v) for v in by_platform.values())
            return UriResponse.get_single_data_response("user_connections", {
                "user_id": user_id,
                "connected_platforms": connected_platforms,
                "connections": by_platform,
                "total_connections": total,
            })

        except Exception as e:
            return UriResponse.error_response(
                f"Failed to retrieve connections: {str(e)}"
            )

    # -------------------------------------------------------------------------
    # 5. Disconnect — removes account from Outstand and local mirror
    # -------------------------------------------------------------------------

    @staticmethod
    async def disconnect_account(
        db: AsyncIOMotorDatabase,
        user_id: str,
        account_id: str,
        brand_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Permanently disconnect one social account of the active brand: the connection's own `id`
        (or, for an account recorded from the retired Outstand, its old outstand_account_id).
        Recorded in `disconnected_accounts` so it never reappears.
        """
        brand_scope = _brand_scope(user_id, brand_id)
        match = {**brand_scope, "$and": [{"$or": [{"id": account_id}, {"outstand_account_id": account_id}]}]}
        local = await db["social_connections"].find_one(match)
        await db["social_connections"].delete_many(match)

        # Release the inbox's claim on this account too. A channel row outlives
        # the connection otherwise, and a workspace that still claims a Page it
        # no longer connects makes the next workspace to connect it a second
        # claimant — at which point webhooks for that Page are dropped, for both.
        await _release_inbox_claim(db, local, user_id, brand_id)

        now = datetime.utcnow()
        tombstone_scope = _tombstone_scope(user_id, brand_id)
        await db["disconnected_accounts"].update_one(
            {**tombstone_scope, "account_id": account_id},
            {"$set": {
                **tombstone_scope,
                "account_id": account_id,
                "platform": local.get("platform") if local else None,
                "disconnected_at": now,
            }},
            upsert=True,
        )

        return UriResponse.get_single_data_response("disconnection", {
            "account_id": account_id,
            "platform": local.get("platform") if local else None,
            "username": local.get("username") if local else None,
            "status": "disconnected",
            "disconnected_at": now.isoformat(),
        })

    # -------------------------------------------------------------------------
    # 5b. Disconnect all accounts for one platform — "Disconnect All"
    # -------------------------------------------------------------------------

    @staticmethod
    async def disconnect_all_for_platform(
        db: AsyncIOMotorDatabase,
        user_id: str,
        platform: str,
        brand_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Disconnect every account of one platform for the active brand (what get_user_connections
        shows the customer for it, including accounts that need reconnecting).
        """
        brand_scope = _brand_scope(user_id, brand_id)
        current = await SocialAccountService.get_user_connections(db, user_id, brand_id)
        accounts = (current.get("responseData") or {}).get("connections", {}).get(platform, [])

        results = []
        for acc in accounts:
            if acc.get("id"):
                result = await SocialAccountService.disconnect_account(db, user_id, acc["id"], brand_id)
                results.append({"account_id": acc["id"], "status": (result.get("responseData") or {}).get("status")})
                continue
            # A connection without an id: match it the way each direct flow's own disconnect does —
            # Instagram by ig_user_id, everything else (one connection per platform) by platform.
            direct_filter: Dict[str, Any] = {**brand_scope, "platform": platform}
            if platform == "instagram" and acc.get("ig_user_id"):
                direct_filter["ig_user_id"] = acc["ig_user_id"]
            # Read before deleting: the connection carries the page/Instagram ids
            # the inbox claims are keyed on, and they are gone after the delete.
            doomed = await db["social_connections"].find_one(direct_filter)
            await db["social_connections"].delete_one(direct_filter)
            await _release_inbox_claim(db, doomed, user_id, brand_id)
            results.append({"connected_via": acc.get("connected_via"), "status": "disconnected"})

        return UriResponse.get_single_data_response("disconnect_all", {
            "platform": platform,
            "disconnected_count": len(results),
            "results": results,
        })

    # -------------------------------------------------------------------------
    # 6. Onboarding status — step 2 completion check
    # -------------------------------------------------------------------------

    @staticmethod
    async def get_onboarding_status(
        db: AsyncIOMotorDatabase,
        user_id: str,
    ) -> Dict[str, Any]:
        """
        Returns onboarding completion status:
        - step_1_complete: brand profile saved
        - step_2_complete: at least one social account connected
        Used by the frontend to determine which onboarding step to show.
        """
        # Step 1: brand profile
        brand_profile = await db["brand_profiles"].find_one({"user_id": user_id})
        step_1_complete = brand_profile is not None and bool(brand_profile.get("brand_name"))

        # Step 2: a social account connected (directly; Outstand is retired)
        step_2_complete = await db["social_connections"].count_documents({
            "user_id": user_id,
            "connection_status": "active",
            "connected_via": {"$ne": "outstand"},
        }) > 0

        onboarding_complete = step_1_complete and step_2_complete

        return UriResponse.get_single_data_response("onboarding_status", {
            "user_id": user_id,
            "step_1_complete": step_1_complete,
            "step_2_complete": step_2_complete,
            "onboarding_complete": onboarding_complete,
            "current_step": 1 if not step_1_complete else (2 if not step_2_complete else None),
        })

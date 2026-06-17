from typing import Dict, List, Any, Optional
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.domain.responses.uri_response import UriResponse
from app.core.config import settings
from .outstand_service import OutstandService, PLATFORM_TO_NETWORK, SUPPORTED_PLATFORMS


class SocialAccountService:

    # -------------------------------------------------------------------------
    # 1. Initiate OAuth flow — returns auth URLs for each requested platform
    # -------------------------------------------------------------------------

    @staticmethod
    async def initiate_connection_flow(
        user_id: str,
        platforms: List[str],
        source: str = "onboarding",
        brand_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Build Outstand OAuth URLs for each requested platform.
        For agency brands, tenant_id is the brand_id so Outstand keeps
        each brand's accounts isolated. Personal brands use user_id.
        """
        from app.models.brand_account import BrandAccount
        is_personal = (not brand_id) or brand_id == BrandAccount.personal_brand_id(user_id)
        tenant_id = user_id if is_personal else brand_id

        outstand = OutstandService()
        # Use PUBLIC_API_URL if set (browser-reachable), otherwise fall back to gateway URL
        _base = (settings.PUBLIC_API_URL or settings.URI_GATEWAY_BASE_API_URL).rstrip("/")
        callback_url = f"{_base}/social-media/connect/callback/outstand?source={source}"

        auth_urls: Dict[str, str] = {}
        unsupported: List[str] = []

        failed: List[Dict[str, str]] = []

        for platform in platforms:
            network = PLATFORM_TO_NETWORK.get(platform.lower())
            if not network:
                unsupported.append(platform)
                continue

            try:
                url = await outstand.get_auth_url(
                    network=network,
                    tenant_id=tenant_id,
                    redirect_uri=callback_url,
                )
                auth_urls[platform.lower()] = url
            except Exception as e:
                err_str = str(e)
                print(f"Failed to get auth URL for {platform}: {err_str}")
                # Surface Outstand credential/config errors clearly
                if "401" in err_str:
                    return UriResponse.error_response(
                        f"Outstand API key is invalid or not configured. "
                        f"Check OUTSTAND_API_KEY in your environment and ensure the "
                        f"'{network}' network is registered via setup_outstand_networks.py.",
                        code=401,
                    )
                if "404" in err_str:
                    return UriResponse.error_response(
                        f"The '{network}' network is not configured in Outstand. "
                        f"Run setup_outstand_networks.py to register it first.",
                        code=404,
                    )
                failed.append({"platform": platform, "error": err_str})

        if not auth_urls:
            failure_detail = f" Failures: {failed}" if failed else ""
            return UriResponse.error_response(
                f"Could not generate auth URLs for any requested platform.{failure_detail} "
                f"Unsupported: {unsupported}. Supported: {sorted(SUPPORTED_PLATFORMS)}",
                code=400,
            )

        return UriResponse.get_single_data_response("connection_flow", {
            "user_id": user_id,
            "auth_urls": auth_urls,
            "platforms": list(auth_urls.keys()),
            "unsupported_platforms": unsupported,
            "failed_platforms": failed,
            "instructions": (
                "Open each auth_url for the user to authorise. "
                "After authorisation, Outstand will redirect to the callback URL. "
                "Then call GET /connect/pending/{sessionToken} and "
                "POST /connect/finalize to complete the connection."
            ),
        })

    # -------------------------------------------------------------------------
    # 2. Get pending connection — returns pages available for the user to select
    # -------------------------------------------------------------------------

    @staticmethod
    async def get_pending_connection(session_token: str, db=None) -> Dict[str, Any]:
        """
        After Outstand OAuth redirect, retrieve available pages/accounts.
        The frontend shows these to the user for selection before finalising.
        """
        outstand = OutstandService()
        try:
            result = await outstand.get_pending_connection(session_token)
            pending_data = result.get("data", {})
            raw_pages = pending_data.get("availablePages", [])
            print(f"[PendingConnection] raw availablePages: {raw_pages}")

            augmented_pages = list(raw_pages)

            return UriResponse.get_single_data_response("pending_connection", {
                "session_token": session_token,
                "network": pending_data.get("network"),
                "expires_at": pending_data.get("expiresAt"),
                "available_pages": augmented_pages,
            })
        except Exception as e:
            return UriResponse.error_response(
                f"Could not retrieve pending connection. "
                f"The session may have expired — please restart the connection flow. "
                f"Detail: {str(e)}",
                code=400,
            )

    # -------------------------------------------------------------------------
    # 3. Finalize connection — selects pages and stores accounts
    # -------------------------------------------------------------------------

    @staticmethod
    async def finalize_connection(
        db: AsyncIOMotorDatabase,
        user_id: str,
        session_token: str,
        selected_page_ids: List[str],
        brand_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Complete the OAuth flow by finalising the selected pages.
        Stores the connected account IDs in our local DB for fast publishing lookups.
        """
        from app.models.brand_account import BrandAccount
        is_personal = (not brand_id) or brand_id == BrandAccount.personal_brand_id(user_id)

        outstand = OutstandService()
        try:
            result = await outstand.finalize_connection(session_token, selected_page_ids)
            print(f"[Finalize] raw result: {result}")
            accounts = result.get("connectedAccounts") or result.get("data", [])
            if not isinstance(accounts, list):
                accounts = [accounts]
            print(f"[Finalize] accounts: {accounts}")

            now = datetime.utcnow()
            stored = []
            for acc in accounts:
                outstand_account_id = acc.get("id")
                network = acc.get("network")

                doc = {
                    "id": outstand_account_id,
                    "user_id": user_id,
                    "platform": network,
                    "outstand_account_id": outstand_account_id,
                    "username": acc.get("username"),
                    "account_name": acc.get("nickname") or acc.get("username"),
                    "profile_picture_url": acc.get("profilePictureUrl") or acc.get("profile_picture_url"),
                    "account_type": acc.get("accountType"),
                    "network_unique_id": acc.get("network_unique_id") or acc.get("networkUniqueId"),
                    "connection_status": "active",
                    "connected_via": "outstand",
                    "connected_at": now,
                    "updated_at": now,
                }
                if not is_personal:
                    doc["brand_id"] = brand_id

                # Scope the delete to this brand so we don't wipe another brand's connection
                brand_scope = {"user_id": user_id} if is_personal else {"brand_id": brand_id}
                await db["social_connections"].delete_many({
                    "$or": [
                        {"id": outstand_account_id},
                        {**brand_scope, "platform": network},
                    ]
                })
                await db["social_connections"].insert_one(doc)
                stored.append({
                    "outstand_account_id": outstand_account_id,
                    "platform": network,
                    "username": acc.get("username"),
                    "account_name": acc.get("nickname") or acc.get("username"),
                })

            print(f"[Finalize] stored platforms: {[s['platform'] for s in stored]}")
            await db["pending_page_tokens"].delete_many({"session_token": session_token})

            return UriResponse.get_single_data_response("accounts_connected", {
                "user_id": user_id,
                "accounts_connected": stored,
                "total": len(stored),
                "connected_at": now.isoformat(),
            })

        except Exception as e:
            import traceback as _tb
            print(f"[Finalize] EXCEPTION: {e}\n{_tb.format_exc()}")
            return UriResponse.error_response(
                f"Failed to finalise connection: {str(e)}",
                code=500,
            )

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
        Queries Outstand then merges direct connections from the local DB.
        """
        from app.models.brand_account import BrandAccount

        is_personal = (not brand_id) or brand_id == BrandAccount.personal_brand_id(user_id)
        personal_bid = BrandAccount.personal_brand_id(user_id)
        tenant = user_id if is_personal else brand_id
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

        # 1. Outstand-managed accounts — failures must not prevent direct connections
        try:
            outstand = OutstandService()
            result = await outstand.list_accounts(tenant_id=tenant)
            for acc in result.get("data", []):
                platform = acc.get("network", "unknown")
                by_platform.setdefault(platform, []).append({
                    "outstand_account_id": acc.get("id"),
                    "platform": platform,
                    "username": acc.get("username"),
                    "account_name": acc.get("nickname"),
                    "profile_picture_url": acc.get("profile_picture_url"),
                    "account_type": acc.get("accountType"),
                    "is_active": bool(acc.get("isActive")),
                    "connected_at": acc.get("createdAt"),
                })
        except Exception as e:
            print(f"[get_user_connections] Outstand list_accounts failed (non-fatal): {e}")

        try:
            # 2. All active connections from local DB.
            # For Outstand-managed accounts already returned in step 1, skip to avoid duplicates.
            # This ensures connections are shown even when the Outstand API is unavailable.
            outstand_ids_seen = {
                acc.get("outstand_account_id")
                for accs in by_platform.values()
                for acc in accs
                if acc.get("outstand_account_id")
            }
            local_cursor = db["social_connections"].find(local_filter)
            async for doc in local_cursor:
                platform = doc.get("platform", "unknown")
                doc_outstand_id = doc.get("outstand_account_id") or doc.get("id")
                if doc_outstand_id and doc_outstand_id in outstand_ids_seen:
                    continue
                by_platform.setdefault(platform, []).append({
                    "platform": platform,
                    "connected_via": doc.get("connected_via"),
                    "outstand_account_id": doc.get("outstand_account_id"),
                    "username": doc.get("username"),
                    "account_name": doc.get("account_name"),
                    "profile_picture_url": doc.get("profile_picture_url"),
                    "is_active": True,
                    "connected_at": doc.get("connected_at"),
                    "page_name": doc.get("account_name"),
                    "ig_user_id": doc.get("ig_user_id"),
                })

            total = sum(len(v) for v in by_platform.values())
            return UriResponse.get_single_data_response("user_connections", {
                "user_id": user_id,
                "connected_platforms": list(by_platform.keys()),
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
        outstand_account_id: str,
        brand_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Permanently disconnect a social account scoped to the active brand.
        outstand_account_id is the ID returned by Outstand (e.g. '9dyJS').
        """
        from app.models.brand_account import BrandAccount
        is_personal = (not brand_id) or brand_id == BrandAccount.personal_brand_id(user_id)
        personal_bid = BrandAccount.personal_brand_id(user_id)
        if is_personal:
            brand_scope = {
                "user_id": user_id,
                "$or": [
                    {"brand_id": {"$exists": False}},
                    {"brand_id": None},
                    {"brand_id": personal_bid},
                ],
            }
        else:
            brand_scope = {"brand_id": brand_id}

        local = await db["social_connections"].find_one({
            **brand_scope,
            "outstand_account_id": outstand_account_id,
        })
        if not local:
            return UriResponse.error_response(
                "Account not found or does not belong to this brand.", code=404
            )

        outstand = OutstandService()
        try:
            await outstand.delete_account(outstand_account_id)

            # Remove from local mirror
            await db["social_connections"].delete_one({
                **brand_scope,
                "outstand_account_id": outstand_account_id,
            })

            return UriResponse.get_single_data_response("disconnection", {
                "outstand_account_id": outstand_account_id,
                "platform": local.get("platform"),
                "username": local.get("username"),
                "status": "disconnected",
                "disconnected_at": datetime.utcnow().isoformat(),
            })

        except Exception as e:
            return UriResponse.error_response(
                f"Disconnection failed: {str(e)}", code=500
            )

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

        # Step 2: social accounts connected (check local mirror first, then Outstand)
        local_connections = await db["social_connections"].count_documents({
            "user_id": user_id,
            "connection_status": "active",
        })

        if local_connections == 0:
            # Double-check with Outstand in case mirror is stale
            try:
                outstand = OutstandService()
                result = await outstand.list_accounts(tenant_id=user_id)
                live_count = len(result.get("data", []))
                step_2_complete = live_count > 0
            except Exception:
                step_2_complete = False
        else:
            step_2_complete = True

        onboarding_complete = step_1_complete and step_2_complete

        return UriResponse.get_single_data_response("onboarding_status", {
            "user_id": user_id,
            "step_1_complete": step_1_complete,
            "step_2_complete": step_2_complete,
            "onboarding_complete": onboarding_complete,
            "current_step": 1 if not step_1_complete else (2 if not step_2_complete else None),
        })

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
    ) -> Dict[str, Any]:
        """
        Build Outstand OAuth URLs for each requested platform.
        tenant_id is set to the URI user_id so Outstand can associate the
        connected account with the correct user.
        The frontend should open each auth_url for the user to authorise.
        """
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
                    tenant_id=user_id,
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

            # Store page access tokens server-side so finalize_connection can use
            # them to auto-detect linked Instagram Business Accounts.
            augmented_pages = list(raw_pages)
            if raw_pages and db is not None:
                from .instagram_direct_service import InstagramDirectService
                token_docs = []
                for p in raw_pages:
                    if not p.get("id") or not p.get("pageAccessToken"):
                        continue
                    token_docs.append({
                        "session_token": session_token,
                        "page_id": p["id"],
                        "page_access_token": p["pageAccessToken"],
                        "page_name": p.get("name", ""),
                    })
                    # Preview linked Instagram account for the selector UI
                    ig = await InstagramDirectService.get_instagram_account_from_page(
                        p["id"], p["pageAccessToken"]
                    )
                    if ig:
                        augmented_pages.append({
                            "id": ig["id"],
                            "name": ig.get("name") or ig.get("username"),
                            "username": ig.get("username"),
                            "type": "instagram_business_account",
                            "network": "instagram",
                            "profilePictureUrl": ig.get("profile_picture_url"),
                            "auto_connect": True,
                            "linked_page_id": p["id"],
                        })
                if token_docs:
                    await db["pending_page_tokens"].delete_many({"session_token": session_token})
                    await db["pending_page_tokens"].insert_many(token_docs)

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
    ) -> Dict[str, Any]:
        """
        Complete the OAuth flow by finalising the selected pages.
        Stores the connected account IDs in our local DB for fast publishing lookups.
        """
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
                await db["social_connections"].replace_one(
                    {
                        "user_id": user_id,
                        "platform": network,
                    },
                    doc,
                    upsert=True,
                )
                stored.append({
                    "outstand_account_id": outstand_account_id,
                    "platform": network,
                    "username": acc.get("username"),
                    "account_name": acc.get("nickname") or acc.get("username"),
                })

            # Auto-detect Instagram Business Accounts linked to connected Facebook Pages.
            # Page access tokens were captured server-side in get_pending_connection.
            print(f"[Finalize] stored platforms: {[s['platform'] for s in stored]}")
            if any(s["platform"] == "facebook" for s in stored):
                from .instagram_direct_service import InstagramDirectService
                for page_id in selected_page_ids:
                    token_doc = await db["pending_page_tokens"].find_one(
                        {"session_token": session_token, "page_id": page_id}
                    )
                    if not token_doc or not token_doc.get("page_access_token"):
                        continue
                    page_token = token_doc["page_access_token"]
                    ig = await InstagramDirectService.get_instagram_account_from_page(page_id, page_token)
                    if not ig:
                        print(f"ℹ️ No Instagram Business Account linked to Facebook Page {page_id}")
                        continue
                    ig_doc = {
                        "id": ig["id"],
                        "user_id": user_id,
                        "platform": "instagram",
                        "connected_via": "instagram_direct",
                        "ig_user_id": ig["id"],
                        "page_id": page_id,
                        "page_access_token": page_token,
                        "username": ig.get("username"),
                        "account_name": ig.get("name") or ig.get("username"),
                        "profile_picture_url": ig.get("profile_picture_url"),
                        "connection_status": "active",
                        "connected_at": now,
                        "updated_at": now,
                    }
                    await db["social_connections"].replace_one(
                        {"user_id": user_id, "platform": "instagram", "ig_user_id": ig["id"]},
                        ig_doc,
                        upsert=True,
                    )
                    stored.append({
                        "platform": "instagram",
                        "username": ig.get("username"),
                        "account_name": ig.get("name") or ig.get("username"),
                    })
                    print(f"✅ Instagram direct: @{ig.get('username')} (ig_user_id={ig['id']})")
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
    ) -> Dict[str, Any]:
        """
        Return all social accounts connected by the user.
        Queries Outstand directly for live/accurate status.
        """
        outstand = OutstandService()
        try:
            result = await outstand.list_accounts(tenant_id=user_id)
            accounts = result.get("data", [])

            # Group by platform
            by_platform: Dict[str, list] = {}
            for acc in accounts:
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

            # Merge in direct connections (e.g. Instagram via Facebook Page token)
            direct_cursor = db["social_connections"].find({
                "user_id": user_id,
                "connected_via": {"$ne": "outstand"},
                "connection_status": "active",
            })
            async for doc in direct_cursor:
                platform = doc.get("platform", "unknown")
                by_platform.setdefault(platform, []).append({
                    "platform": platform,
                    "connected_via": doc.get("connected_via"),
                    "username": doc.get("username"),
                    "account_name": doc.get("account_name"),
                    "profile_picture_url": doc.get("profile_picture_url"),
                    "is_active": True,
                    "connected_at": doc.get("connected_at"),
                    "page_name": doc.get("account_name"),
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
    ) -> Dict[str, Any]:
        """
        Permanently disconnect a social account.
        outstand_account_id is the ID returned by Outstand (e.g. '9dyJS').
        """
        # Verify this account belongs to the user (local mirror check)
        local = await db["social_connections"].find_one({
            "user_id": user_id,
            "outstand_account_id": outstand_account_id,
        })
        if not local:
            return UriResponse.error_response(
                "Account not found or does not belong to this user.", code=404
            )

        outstand = OutstandService()
        try:
            await outstand.delete_account(outstand_account_id)

            # Remove from local mirror
            await db["social_connections"].delete_one({
                "user_id": user_id,
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

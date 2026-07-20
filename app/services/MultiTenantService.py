"""
Multi-Tenant Service - SDK Client and End-User Management

Handles SDK client profile creation, end-user management, and multi-tenant
operations for the URISocial SDK integration.
"""

from datetime import datetime
from typing import Optional, Dict, Any, List
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId

from app.models.sdk_client_profile import SDKClientProfile
from app.models.sdk_end_user import SDKEndUser


class MultiTenantService:
    """Service for managing SDK clients and end-users in multi-tenant mode"""

    SDK_CLIENTS_COLLECTION = "sdk_client_profiles"
    END_USERS_COLLECTION = "sdk_end_users"

    @staticmethod
    async def get_or_create_sdk_client(
        api_key_hash: str,
        api_key_prefix: str,
        developer_id: str,
        company_name: str,
        db: AsyncIOMotorDatabase
    ) -> Optional[SDKClientProfile]:
        """
        Get or create SDK client profile from API key authentication

        Args:
            api_key_hash: Hashed API key for lookup
            api_key_prefix: API key prefix for display
            developer_id: Developer user ID from SDK Gateway
            company_name: Company/application name
            db: Database connection

        Returns:
            SDKClientProfile object or None if creation fails
        """
        # Try to find existing SDK client by API key hash
        existing = await db[MultiTenantService.SDK_CLIENTS_COLLECTION].find_one({
            "api_key_hash": api_key_hash
        })

        if existing:
            existing["_id"] = str(existing["_id"])
            return SDKClientProfile(**existing)

        # Create new SDK client profile
        sdk_client_id = SDKClientProfile.generate_sdk_client_id()

        client_doc = {
            "sdk_client_id": sdk_client_id,
            "api_key_hash": api_key_hash,
            "api_key_prefix": api_key_prefix,
            "developer_id": developer_id,
            "company_name": company_name,
            "company_logo_url": None,
            "company_website": None,
            "settings": {
                "enable_custom_branding": False,
                "require_email_verification": True,
                "auto_create_end_users": True,
                "webhook_url": None,
                "custom_domain": None,
                "allowed_features": ["content_generation", "image_generation", "brand_profile"],
                "default_brand_preferences": {}
            },
            "limits": {
                "max_end_users": 10000,
                "max_brands_per_user": 3,
                "max_monthly_generations": 50000,
                "max_monthly_images": 10000
            },
            "stats": {
                "total_end_users": 0,
                "active_end_users_30d": 0,
                "total_generations_month": 0,
                "total_images_month": 0,
                "total_api_calls": 0,
                "last_activity_at": None
            },
            "shared_credits_with_developer": True,
            "dedicated_credits": 0,
            "status": "active",
            "metadata": {},
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "last_api_call_at": None
        }

        try:
            result = await db[MultiTenantService.SDK_CLIENTS_COLLECTION].insert_one(client_doc)
            client_doc["_id"] = str(result.inserted_id)
            return SDKClientProfile(**client_doc)
        except Exception as e:
            print(f"❌ Error creating SDK client profile: {e}")
            return None

    @staticmethod
    async def get_or_create_end_user(
        sdk_client_id: str,
        external_user_id: str,
        external_email: Optional[str],
        external_name: Optional[str],
        db: AsyncIOMotorDatabase
    ) -> Optional[SDKEndUser]:
        """
        Get or create end-user for an SDK client

        Args:
            sdk_client_id: Parent SDK client ID
            external_user_id: Client's user ID (from their system)
            external_email: User's email (optional)
            external_name: User's name (optional)
            db: Database connection

        Returns:
            SDKEndUser object or None if creation fails
        """
        # Try to find existing end-user by SDK client + external user ID
        existing = await db[MultiTenantService.END_USERS_COLLECTION].find_one({
            "sdk_client_id": sdk_client_id,
            "external_user_id": external_user_id
        })

        if existing:
            existing["_id"] = str(existing["_id"])
            return SDKEndUser(**existing)

        # Create new end-user
        end_user_id = SDKEndUser.generate_end_user_id()

        end_user_doc = {
            "end_user_id": end_user_id,
            "sdk_client_id": sdk_client_id,
            "external_user_id": external_user_id,
            "external_email": external_email,
            "external_name": external_name,
            "brand_profile_id": None,
            "onboarding_completed": False,
            "status": "active",
            "metadata": {},
            "last_active_at": None,
            "total_api_calls": 0,
            "total_generations": 0,
            "total_images": 0,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "deleted_at": None
        }

        try:
            result = await db[MultiTenantService.END_USERS_COLLECTION].insert_one(end_user_doc)
            end_user_doc["_id"] = str(result.inserted_id)

            # Increment SDK client's end-user count
            await db[MultiTenantService.SDK_CLIENTS_COLLECTION].update_one(
                {"sdk_client_id": sdk_client_id},
                {
                    "$inc": {"stats.total_end_users": 1},
                    "$set": {"updated_at": datetime.utcnow()}
                }
            )

            return SDKEndUser(**end_user_doc)
        except Exception as e:
            print(f"❌ Error creating SDK end-user: {e}")
            return None

    @staticmethod
    async def link_end_user_to_brand_profile(
        end_user_id: str,
        brand_profile_id: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """
        Link an end-user to a brand profile

        Args:
            end_user_id: End-user ID (enduser_xxxxx)
            brand_profile_id: Brand profile ID to link
            db: Database connection

        Returns:
            True if successful, False otherwise
        """
        result = await db[MultiTenantService.END_USERS_COLLECTION].update_one(
            {"end_user_id": end_user_id},
            {
                "$set": {
                    "brand_profile_id": brand_profile_id,
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return result.modified_count > 0

    @staticmethod
    async def get_or_create_end_user_brand_id(
        end_user: SDKEndUser,
        developer_user_id: str,
        db: AsyncIOMotorDatabase
    ) -> str:
        """
        Resolve (and lazily create) the brand_id an SDK end-user's brand/content
        data is isolated under. Idempotent: repeat calls for the same end_user
        return the same brand_id.

        This is the single place this resolution should happen — both the
        /social-media/* path (get_flexible_brand_context) and the /api/v1/*
        path (get_sdk_context) call this, so they can't drift apart. Before
        this existed, every end-user of a developer's SDK integration
        collided onto that developer's own personal brand, since nothing
        ever populated SDKEndUser.brand_profile_id despite the field
        existing specifically to prevent that.
        """
        if end_user.brand_profile_id:
            return end_user.brand_profile_id

        from app.services.BrandAccountService import BrandAccountService

        brand_name = end_user.external_name or f"{end_user.external_user_id}'s Brand"
        brand = await BrandAccountService.create_brand(
            owner_user_id=developer_user_id,
            name=brand_name,
            db=db,
        )
        await MultiTenantService.link_end_user_to_brand_profile(
            end_user.end_user_id, brand.brand_id, db
        )
        return brand.brand_id

    @staticmethod
    async def update_end_user_activity(
        end_user_id: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """
        Update end-user's last activity timestamp

        Args:
            end_user_id: End-user ID
            db: Database connection

        Returns:
            True if successful
        """
        now = datetime.utcnow()

        result = await db[MultiTenantService.END_USERS_COLLECTION].update_one(
            {"end_user_id": end_user_id},
            {
                "$set": {
                    "last_active_at": now,
                    "updated_at": now
                }
            }
        )

        return result.modified_count > 0

    @staticmethod
    async def increment_end_user_usage(
        end_user_id: str,
        metric: str,
        amount: int,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """
        Increment end-user usage statistics

        Args:
            end_user_id: End-user ID
            metric: Metric type (generation, image, api_call)
            amount: Amount to increment
            db: Database connection

        Returns:
            True if successful
        """
        metric_map = {
            "generation": "total_generations",
            "image": "total_images",
            "api_call": "total_api_calls"
        }

        field = metric_map.get(metric)
        if not field:
            return False

        result = await db[MultiTenantService.END_USERS_COLLECTION].update_one(
            {"end_user_id": end_user_id},
            {
                "$inc": {field: amount},
                "$set": {
                    "last_active_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return result.modified_count > 0

    @staticmethod
    async def update_sdk_client_activity(
        sdk_client_id: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """
        Update SDK client's last activity timestamp

        Args:
            sdk_client_id: SDK client ID
            db: Database connection

        Returns:
            True if successful
        """
        now = datetime.utcnow()

        result = await db[MultiTenantService.SDK_CLIENTS_COLLECTION].update_one(
            {"sdk_client_id": sdk_client_id},
            {
                "$set": {
                    "last_api_call_at": now,
                    "updated_at": now
                },
                "$inc": {"stats.total_api_calls": 1}
            }
        )

        return result.modified_count > 0

    @staticmethod
    async def increment_sdk_client_usage(
        sdk_client_id: str,
        metric: str,
        amount: int,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """
        Increment SDK client usage statistics

        Args:
            sdk_client_id: SDK client ID
            metric: Metric type (generation, image)
            amount: Amount to increment
            db: Database connection

        Returns:
            True if successful
        """
        metric_map = {
            "generation": "stats.total_generations_month",
            "image": "stats.total_images_month"
        }

        field = metric_map.get(metric)
        if not field:
            return False

        result = await db[MultiTenantService.SDK_CLIENTS_COLLECTION].update_one(
            {"sdk_client_id": sdk_client_id},
            {
                "$inc": {field: amount},
                "$set": {"updated_at": datetime.utcnow()}
            }
        )

        return result.modified_count > 0

    @staticmethod
    async def get_sdk_client_by_id(
        sdk_client_id: str,
        db: AsyncIOMotorDatabase
    ) -> Optional[SDKClientProfile]:
        """Get SDK client profile by ID"""
        doc = await db[MultiTenantService.SDK_CLIENTS_COLLECTION].find_one({
            "sdk_client_id": sdk_client_id
        })

        if not doc:
            return None

        doc["_id"] = str(doc["_id"])
        return SDKClientProfile(**doc)

    @staticmethod
    async def get_end_user_by_id(
        end_user_id: str,
        db: AsyncIOMotorDatabase
    ) -> Optional[SDKEndUser]:
        """Get end-user by ID"""
        doc = await db[MultiTenantService.END_USERS_COLLECTION].find_one({
            "end_user_id": end_user_id
        })

        if not doc:
            return None

        doc["_id"] = str(doc["_id"])
        return SDKEndUser(**doc)

    @staticmethod
    async def get_end_users_for_client(
        sdk_client_id: str,
        limit: int,
        skip: int,
        db: AsyncIOMotorDatabase
    ) -> List[SDKEndUser]:
        """
        Get paginated list of end-users for an SDK client

        Args:
            sdk_client_id: SDK client ID
            limit: Max number of results
            skip: Number of results to skip
            db: Database connection

        Returns:
            List of SDKEndUser objects
        """
        cursor = db[MultiTenantService.END_USERS_COLLECTION].find({
            "sdk_client_id": sdk_client_id,
            "status": {"$ne": "deleted"}
        }).sort("created_at", -1).skip(skip).limit(limit)

        end_users = []
        async for doc in cursor:
            doc["_id"] = str(doc["_id"])
            end_users.append(SDKEndUser(**doc))

        return end_users

    @staticmethod
    async def mark_end_user_onboarding_complete(
        end_user_id: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """Mark end-user onboarding as completed"""
        result = await db[MultiTenantService.END_USERS_COLLECTION].update_one(
            {"end_user_id": end_user_id},
            {
                "$set": {
                    "onboarding_completed": True,
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return result.modified_count > 0

    @staticmethod
    async def can_create_end_user(
        sdk_client_id: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """Check if SDK client can create more end-users"""
        client = await MultiTenantService.get_sdk_client_by_id(sdk_client_id, db)
        if not client:
            return False

        return client.can_create_end_user()

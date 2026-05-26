"""
Client Service - Business logic for client management

Handles all client-related operations including creation, updates,
credit management, and workspace limits.
"""

from datetime import datetime
from typing import Optional, List, Dict, Any
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId

from app.models.client import (
    Client,
    ClientBillingInfo,
    ClientSubscription,
    CreateClientRequest,
    UpdateClientRequest,
)


class ClientService:
    """Service for managing clients"""

    @staticmethod
    async def create_client(
        request: CreateClientRequest,
        owner_user_id: str,
        db: AsyncIOMotorDatabase
    ) -> Client:
        """
        Create a new client

        Args:
            request: Client creation request
            owner_user_id: User ID of the client owner
            db: Database connection

        Returns:
            Created Client object

        Raises:
            ValueError: If slug already exists or validation fails
        """
        # Generate unique client_id and slug
        client_id = Client.generate_client_id()
        slug = Client.generate_slug(request.name)

        # Check if slug already exists
        existing = await db.clients.find_one({"slug": slug})
        if existing:
            # Append random suffix to make it unique
            import secrets
            slug = f"{slug}-{secrets.token_hex(3)}"

        # Determine subscription based on tier
        subscription = ClientSubscription(tier=request.subscription_tier)
        if request.subscription_tier == "starter":
            subscription.total_credits = 1000
            subscription.max_workspaces = 3
            subscription.max_users_per_workspace = 10
        elif request.subscription_tier == "professional":
            subscription.total_credits = 5000
            subscription.max_workspaces = 10
            subscription.max_users_per_workspace = 25
        elif request.subscription_tier == "enterprise":
            subscription.total_credits = 20000
            subscription.max_workspaces = 50
            subscription.max_users_per_workspace = 100

        # Create client object
        client = Client(
            client_id=client_id,
            name=request.name,
            slug=slug,
            description=request.description,
            owner_user_id=owner_user_id,
            billing_info=ClientBillingInfo(
                billing_email=request.billing_email,
                company_name=request.company_name or request.name,
            ),
            subscription=subscription,
        )

        # Insert into database
        result = await db.clients.insert_one(client.to_dict())
        client.id = str(result.inserted_id)

        return client

    @staticmethod
    async def get_client_by_id(
        client_id: str,
        db: AsyncIOMotorDatabase
    ) -> Optional[Client]:
        """Get client by client_id"""
        doc = await db.clients.find_one({"client_id": client_id})
        if not doc:
            return None

        doc["_id"] = str(doc["_id"])
        return Client(**doc)

    @staticmethod
    async def get_client_by_object_id(
        object_id: str,
        db: AsyncIOMotorDatabase
    ) -> Optional[Client]:
        """Get client by MongoDB ObjectId"""
        doc = await db.clients.find_one({"_id": ObjectId(object_id)})
        if not doc:
            return None

        doc["_id"] = str(doc["_id"])
        return Client(**doc)

    @staticmethod
    async def get_clients_by_owner(
        owner_user_id: str,
        db: AsyncIOMotorDatabase
    ) -> List[Client]:
        """Get all clients owned by a user"""
        cursor = db.clients.find({"owner_user_id": owner_user_id, "status": {"$ne": "deleted"}})
        clients = []

        async for doc in cursor:
            doc["_id"] = str(doc["_id"])
            clients.append(Client(**doc))

        return clients

    @staticmethod
    async def update_client(
        client_id: str,
        request: UpdateClientRequest,
        db: AsyncIOMotorDatabase
    ) -> Optional[Client]:
        """
        Update client information

        Args:
            client_id: Client ID to update
            request: Update request with fields to change
            db: Database connection

        Returns:
            Updated Client object or None if not found
        """
        # Get existing client
        client = await ClientService.get_client_by_id(client_id, db)
        if not client:
            return None

        # Build update document
        update_doc = {"updated_at": datetime.utcnow()}

        if request.name:
            update_doc["name"] = request.name
            # Regenerate slug if name changed
            new_slug = Client.generate_slug(request.name)
            if new_slug != client.slug:
                # Check if new slug exists
                existing = await db.clients.find_one({"slug": new_slug, "client_id": {"$ne": client_id}})
                if not existing:
                    update_doc["slug"] = new_slug

        if request.description is not None:
            update_doc["description"] = request.description

        if request.billing_email:
            update_doc["billing_info.billing_email"] = request.billing_email

        if request.company_name:
            update_doc["billing_info.company_name"] = request.company_name

        if request.settings:
            update_doc["settings"] = request.settings

        if request.metadata:
            update_doc["metadata"] = request.metadata

        # Update in database
        await db.clients.update_one(
            {"client_id": client_id},
            {"$set": update_doc}
        )

        # Return updated client
        return await ClientService.get_client_by_id(client_id, db)

    @staticmethod
    async def deduct_credits(
        client_id: str,
        amount: int,
        operation: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """
        Deduct credits from client account

        Args:
            client_id: Client ID
            amount: Number of credits to deduct
            operation: Operation type (for tracking)
            db: Database connection

        Returns:
            True if successful, False if insufficient credits
        """
        client = await ClientService.get_client_by_id(client_id, db)
        if not client:
            return False

        if not client.has_credits(amount):
            return False

        # Deduct credits
        await db.clients.update_one(
            {"client_id": client_id},
            {
                "$inc": {"subscription.used_credits": amount},
                "$set": {"updated_at": datetime.utcnow()}
            }
        )

        # Track usage based on operation type
        usage_field = None
        if operation in ["content", "image", "post", "api"]:
            metric_map = {
                "content": ("usage_stats.total_content_generated", "usage_stats.content_generated_this_month"),
                "image": ("usage_stats.total_images_generated", "usage_stats.images_generated_this_month"),
                "post": ("usage_stats.total_posts_published", "usage_stats.posts_published_this_month"),
                "api": ("usage_stats.total_api_requests", "usage_stats.api_requests_this_month"),
            }

            if operation in metric_map:
                total_field, monthly_field = metric_map[operation]
                await db.clients.update_one(
                    {"client_id": client_id},
                    {
                        "$inc": {
                            total_field: 1,
                            monthly_field: 1
                        },
                        "$set": {
                            "usage_stats.last_activity_at": datetime.utcnow()
                        }
                    }
                )

        return True

    @staticmethod
    async def add_credits(
        client_id: str,
        amount: int,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """Add credits to client account"""
        result = await db.clients.update_one(
            {"client_id": client_id},
            {
                "$inc": {"subscription.total_credits": amount},
                "$set": {"updated_at": datetime.utcnow()}
            }
        )

        return result.modified_count > 0

    @staticmethod
    async def get_workspace_count(
        client_id: str,
        db: AsyncIOMotorDatabase
    ) -> int:
        """Get number of active workspaces for a client"""
        count = await db.workspaces.count_documents({
            "client_id": client_id,
            "status": {"$ne": "deleted"}
        })
        return count

    @staticmethod
    async def can_create_workspace(
        client_id: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """Check if client can create more workspaces"""
        client = await ClientService.get_client_by_id(client_id, db)
        if not client:
            return False

        current_count = await ClientService.get_workspace_count(client_id, db)
        return client.can_create_workspace(current_count)

    @staticmethod
    async def suspend_client(
        client_id: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """Suspend a client account"""
        result = await db.clients.update_one(
            {"client_id": client_id},
            {
                "$set": {
                    "status": "suspended",
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return result.modified_count > 0

    @staticmethod
    async def reactivate_client(
        client_id: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """Reactivate a suspended client account"""
        result = await db.clients.update_one(
            {"client_id": client_id},
            {
                "$set": {
                    "status": "active",
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return result.modified_count > 0

    @staticmethod
    async def delete_client(
        client_id: str,
        hard_delete: bool,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """
        Delete a client (soft or hard delete)

        Args:
            client_id: Client ID to delete
            hard_delete: If True, permanently remove from database
            db: Database connection

        Returns:
            True if successful
        """
        if hard_delete:
            # Permanently delete
            result = await db.clients.delete_one({"client_id": client_id})
            return result.deleted_count > 0
        else:
            # Soft delete
            result = await db.clients.update_one(
                {"client_id": client_id},
                {
                    "$set": {
                        "status": "deleted",
                        "deleted_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow()
                    }
                }
            )
            return result.modified_count > 0

    @staticmethod
    async def reset_monthly_usage(
        client_id: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """Reset monthly usage counters for billing cycle"""
        result = await db.clients.update_one(
            {"client_id": client_id},
            {
                "$set": {
                    "usage_stats.content_generated_this_month": 0,
                    "usage_stats.images_generated_this_month": 0,
                    "usage_stats.posts_published_this_month": 0,
                    "usage_stats.api_requests_this_month": 0,
                    "subscription.used_credits": 0,  # Reset credits on new billing cycle
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return result.modified_count > 0

    @staticmethod
    async def get_client_usage_summary(
        client_id: str,
        db: AsyncIOMotorDatabase
    ) -> Optional[Dict[str, Any]]:
        """Get usage summary for a client"""
        client = await ClientService.get_client_by_id(client_id, db)
        if not client:
            return None

        workspace_count = await ClientService.get_workspace_count(client_id, db)

        return {
            "client_id": client_id,
            "client_name": client.name,
            "subscription_tier": client.subscription.tier,
            "credits": {
                "total": client.subscription.total_credits,
                "used": client.subscription.used_credits,
                "remaining": client.subscription.total_credits - client.subscription.used_credits,
                "percentage_used": (client.subscription.used_credits / client.subscription.total_credits * 100) if client.subscription.total_credits > 0 else 0
            },
            "workspaces": {
                "current": workspace_count,
                "max": client.subscription.max_workspaces,
                "can_create_more": workspace_count < client.subscription.max_workspaces
            },
            "usage_this_month": {
                "content_generated": client.usage_stats.content_generated_this_month,
                "images_generated": client.usage_stats.images_generated_this_month,
                "posts_published": client.usage_stats.posts_published_this_month,
                "api_requests": client.usage_stats.api_requests_this_month,
            },
            "usage_all_time": {
                "content_generated": client.usage_stats.total_content_generated,
                "images_generated": client.usage_stats.total_images_generated,
                "posts_published": client.usage_stats.total_posts_published,
                "api_requests": client.usage_stats.total_api_requests,
            },
            "last_activity": client.usage_stats.last_activity_at.isoformat() if client.usage_stats.last_activity_at else None
        }

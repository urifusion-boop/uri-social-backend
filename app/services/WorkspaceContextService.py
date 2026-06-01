"""
Workspace Context Service

Provides utilities for workspace context management in multi-tenant operations.
Handles backward compatibility for single-tenant users.
"""

from typing import Optional, Dict, Any
from motor.motor_asyncio import AsyncIOMotorDatabase


class WorkspaceContextService:
    """Service for managing workspace context in requests"""

    @staticmethod
    async def get_user_default_workspace(
        user_id: str,
        db: AsyncIOMotorDatabase
    ) -> Optional[str]:
        """
        Get user's default workspace

        For multi-tenant users: Returns their default workspace
        For legacy single-tenant users: Returns None (backward compatibility)

        Args:
            user_id: User ID
            db: Database connection

        Returns:
            workspace_id or None
        """
        # Find user's workspace where they are a member
        member_doc = await db.workspace_members.find_one({
            "user_id": user_id,
            "status": "active"
        })

        if member_doc:
            return member_doc.get("workspace_id")

        return None

    @staticmethod
    async def get_workspace_from_request(
        user_id: str,
        workspace_id: Optional[str],
        db: AsyncIOMotorDatabase
    ) -> Optional[str]:
        """
        Resolve workspace_id for a request

        Priority:
        1. Explicit workspace_id from request (if provided and user has access)
        2. User's default workspace (if multi-tenant user)
        3. None (for legacy single-tenant users)

        Args:
            user_id: User making the request
            workspace_id: Optional workspace_id from request
            db: Database connection

        Returns:
            Resolved workspace_id or None
        """
        # If workspace_id provided, verify user has access
        if workspace_id:
            has_access = await WorkspaceContextService.verify_workspace_access(
                user_id, workspace_id, db
            )
            if has_access:
                return workspace_id
            else:
                # User doesn't have access, fall back to default
                pass

        # Get user's default workspace
        return await WorkspaceContextService.get_user_default_workspace(user_id, db)

    @staticmethod
    async def verify_workspace_access(
        user_id: str,
        workspace_id: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """
        Verify user has access to workspace

        Args:
            user_id: User ID
            workspace_id: Workspace ID
            db: Database connection

        Returns:
            True if user has access, False otherwise
        """
        member_doc = await db.workspace_members.find_one({
            "workspace_id": workspace_id,
            "user_id": user_id,
            "status": "active"
        })

        return member_doc is not None

    @staticmethod
    async def get_workspace_client_id(
        workspace_id: str,
        db: AsyncIOMotorDatabase
    ) -> Optional[str]:
        """
        Get client_id for a workspace

        Args:
            workspace_id: Workspace ID
            db: Database connection

        Returns:
            client_id or None
        """
        workspace_doc = await db.workspaces.find_one(
            {"workspace_id": workspace_id},
            {"client_id": 1}
        )

        if workspace_doc:
            return workspace_doc.get("client_id")

        return None

    @staticmethod
    def add_workspace_context_to_doc(
        doc: Dict[str, Any],
        workspace_id: Optional[str]
    ) -> Dict[str, Any]:
        """
        Add workspace_id to document if provided

        For backward compatibility, only adds workspace_id if it's not None

        Args:
            doc: Document to update
            workspace_id: Optional workspace_id

        Returns:
            Updated document
        """
        if workspace_id:
            doc["workspace_id"] = workspace_id

        return doc

    @staticmethod
    def build_query_with_workspace(
        base_query: Dict[str, Any],
        user_id: str,
        workspace_id: Optional[str]
    ) -> Dict[str, Any]:
        """
        Build MongoDB query with workspace context

        For multi-tenant users: Filters by workspace_id
        For legacy users: Filters by user_id only

        Args:
            base_query: Base query dict
            user_id: User ID
            workspace_id: Optional workspace_id

        Returns:
            Query with workspace context
        """
        query = base_query.copy()
        query["user_id"] = user_id

        if workspace_id:
            # Multi-tenant: Filter by workspace
            query["workspace_id"] = workspace_id
        else:
            # Legacy: Ensure no workspace_id (single-tenant data)
            query["$or"] = [
                {"workspace_id": {"$exists": False}},
                {"workspace_id": None}
            ]

        return query

    @staticmethod
    async def check_workspace_permission(
        user_id: str,
        workspace_id: str,
        permission: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """
        Check if user has specific permission in workspace

        Args:
            user_id: User ID
            workspace_id: Workspace ID
            permission: Permission to check (e.g., "can_create_content")
            db: Database connection

        Returns:
            True if user has permission, False otherwise
        """
        from app.services.WorkspaceService import WorkspaceService

        return await WorkspaceService.check_permission(
            workspace_id=workspace_id,
            user_id=user_id,
            permission=permission,
            db=db
        )

    @staticmethod
    async def deduct_workspace_credits(
        workspace_id: str,
        credits: int,
        operation: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """
        Deduct credits from workspace's client

        Args:
            workspace_id: Workspace ID
            credits: Number of credits to deduct
            operation: Operation type (e.g., "content_generation")
            db: Database connection

        Returns:
            True if credits deducted successfully, False if insufficient credits
        """
        from app.services.ClientService import ClientService

        # Get client_id for workspace
        client_id = await WorkspaceContextService.get_workspace_client_id(
            workspace_id, db
        )

        if not client_id:
            return False

        # Deduct credits from client
        return await ClientService.deduct_credits(
            client_id=client_id,
            amount=credits,
            operation=operation,
            db=db
        )

    @staticmethod
    async def increment_workspace_usage(
        workspace_id: str,
        metric: str,
        amount: int,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """
        Increment workspace usage statistics

        Args:
            workspace_id: Workspace ID
            metric: Metric to increment (e.g., "content", "image", "post")
            amount: Amount to increment
            db: Database connection

        Returns:
            True if incremented successfully
        """
        from app.services.WorkspaceService import WorkspaceService

        return await WorkspaceService.increment_workspace_usage(
            workspace_id=workspace_id,
            metric=metric,
            amount=amount,
            db=db
        )

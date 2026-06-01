"""
Workspace Service - Business logic for workspace management

Handles workspace creation, updates, member management, and access control.
"""

from datetime import datetime
from typing import Optional, List, Dict, Any
from motor.motor_asyncio import AsyncIOMotorDatabase
from bson import ObjectId

from app.models.workspace import (
    Workspace,
    CreateWorkspaceRequest,
    UpdateWorkspaceRequest,
)
from app.models.workspace_member import (
    WorkspaceMember,
    WorkspaceRole,
    WorkspacePermissions,
    InviteMemberRequest,
    UpdateMemberRoleRequest,
)
from app.services.ClientService import ClientService


class WorkspaceService:
    """Service for managing workspaces"""

    @staticmethod
    async def create_workspace(
        request: CreateWorkspaceRequest,
        client_id: str,
        creator_user_id: str,
        db: AsyncIOMotorDatabase
    ) -> Optional[Workspace]:
        """
        Create a new workspace

        Args:
            request: Workspace creation request
            client_id: Parent client ID
            creator_user_id: User creating the workspace
            db: Database connection

        Returns:
            Created Workspace object or None if client limit reached

        Raises:
            ValueError: If client doesn't exist or validation fails
        """
        # Check if client can create more workspaces
        can_create = await ClientService.can_create_workspace(client_id, db)
        if not can_create:
            return None

        # Generate unique workspace_id and slug
        workspace_id = Workspace.generate_workspace_id()

        # Get existing slugs for this client to avoid duplicates
        existing_workspaces = await db.workspaces.find(
            {"client_id": client_id},
            {"slug": 1}
        ).to_list(length=None)
        existing_slugs = [w["slug"] for w in existing_workspaces]

        slug = Workspace.generate_slug(request.name, existing_slugs)

        # Create workspace object
        workspace = Workspace(
            workspace_id=workspace_id,
            client_id=client_id,
            name=request.name,
            slug=slug,
            description=request.description,
            created_by_user_id=creator_user_id,
        )

        # Apply custom settings if provided
        if request.settings:
            for key, value in request.settings.items():
                if hasattr(workspace.settings, key):
                    setattr(workspace.settings, key, value)

        # Insert into database
        result = await db.workspaces.insert_one(workspace.to_dict())
        workspace.id = str(result.inserted_id)

        # Add creator as owner
        await WorkspaceService.add_member(
            workspace_id=workspace_id,
            user_id=creator_user_id,
            role=WorkspaceRole.OWNER,
            invited_by_user_id=None,  # Self-added
            db=db
        )

        return workspace

    @staticmethod
    async def get_workspace_by_id(
        workspace_id: str,
        db: AsyncIOMotorDatabase
    ) -> Optional[Workspace]:
        """Get workspace by workspace_id"""
        doc = await db.workspaces.find_one({"workspace_id": workspace_id})
        if not doc:
            return None

        doc["_id"] = str(doc["_id"])
        return Workspace(**doc)

    @staticmethod
    async def get_workspaces_by_client(
        client_id: str,
        db: AsyncIOMotorDatabase,
        include_archived: bool = False
    ) -> List[Workspace]:
        """Get all workspaces for a client"""
        query = {"client_id": client_id}
        if not include_archived:
            query["status"] = {"$in": ["active"]}

        cursor = db.workspaces.find(query)
        workspaces = []

        async for doc in cursor:
            doc["_id"] = str(doc["_id"])
            workspaces.append(Workspace(**doc))

        return workspaces

    @staticmethod
    async def get_workspaces_for_user(
        user_id: str,
        db: AsyncIOMotorDatabase
    ) -> List[Dict[str, Any]]:
        """
        Get all workspaces a user is a member of

        Returns workspace info with user's role
        """
        # Get all workspace memberships for user
        cursor = db.workspace_members.find({
            "user_id": user_id,
            "status": "active"
        })

        workspaces_with_role = []

        async for member_doc in cursor:
            workspace = await WorkspaceService.get_workspace_by_id(
                member_doc["workspace_id"],
                db
            )

            if workspace and workspace.status == "active":
                workspaces_with_role.append({
                    "workspace": workspace.to_public_dict(),
                    "role": member_doc["role"],
                    "permissions": member_doc.get("permissions", {})
                })

        return workspaces_with_role

    @staticmethod
    async def update_workspace(
        workspace_id: str,
        request: UpdateWorkspaceRequest,
        db: AsyncIOMotorDatabase
    ) -> Optional[Workspace]:
        """Update workspace information"""
        workspace = await WorkspaceService.get_workspace_by_id(workspace_id, db)
        if not workspace:
            return None

        # Build update document
        update_doc = {"updated_at": datetime.utcnow()}

        if request.name:
            update_doc["name"] = request.name
            # Regenerate slug if name changed
            existing_workspaces = await db.workspaces.find(
                {"client_id": workspace.client_id, "workspace_id": {"$ne": workspace_id}},
                {"slug": 1}
            ).to_list(length=None)
            existing_slugs = [w["slug"] for w in existing_workspaces]
            new_slug = Workspace.generate_slug(request.name, existing_slugs)
            update_doc["slug"] = new_slug

        if request.description is not None:
            update_doc["description"] = request.description

        if request.avatar_url is not None:
            update_doc["avatar_url"] = request.avatar_url

        if request.settings:
            for key, value in request.settings.items():
                update_doc[f"settings.{key}"] = value

        if request.metadata:
            update_doc["metadata"] = request.metadata

        # Update in database
        await db.workspaces.update_one(
            {"workspace_id": workspace_id},
            {"$set": update_doc}
        )

        return await WorkspaceService.get_workspace_by_id(workspace_id, db)

    @staticmethod
    async def archive_workspace(
        workspace_id: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """Archive a workspace"""
        result = await db.workspaces.update_one(
            {"workspace_id": workspace_id},
            {
                "$set": {
                    "status": "archived",
                    "archived_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return result.modified_count > 0

    @staticmethod
    async def unarchive_workspace(
        workspace_id: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """Unarchive a workspace"""
        result = await db.workspaces.update_one(
            {"workspace_id": workspace_id},
            {
                "$set": {
                    "status": "active",
                    "archived_at": None,
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return result.modified_count > 0

    @staticmethod
    async def delete_workspace(
        workspace_id: str,
        hard_delete: bool,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """Delete a workspace (soft or hard delete)"""
        if hard_delete:
            # Permanently delete workspace and all members
            await db.workspaces.delete_one({"workspace_id": workspace_id})
            await db.workspace_members.delete_many({"workspace_id": workspace_id})
            return True
        else:
            # Soft delete
            result = await db.workspaces.update_one(
                {"workspace_id": workspace_id},
                {
                    "$set": {
                        "status": "deleted",
                        "deleted_at": datetime.utcnow(),
                        "updated_at": datetime.utcnow()
                    }
                }
            )
            return result.modified_count > 0

    # ========================================
    # MEMBER MANAGEMENT
    # ========================================

    @staticmethod
    async def add_member(
        workspace_id: str,
        user_id: str,
        role: WorkspaceRole,
        invited_by_user_id: Optional[str],
        db: AsyncIOMotorDatabase
    ) -> WorkspaceMember:
        """Add a member to workspace"""
        # Check if already a member
        existing = await db.workspace_members.find_one({
            "workspace_id": workspace_id,
            "user_id": user_id
        })

        if existing:
            # Reactivate if previously removed
            if existing["status"] == "removed":
                await db.workspace_members.update_one(
                    {"_id": existing["_id"]},
                    {
                        "$set": {
                            "status": "active",
                            "role": role,
                            "permissions": WorkspacePermissions.for_role(role).model_dump(),
                            "updated_at": datetime.utcnow()
                        }
                    }
                )
            existing["_id"] = str(existing["_id"])
            return WorkspaceMember(**existing)

        # Create new member
        member = WorkspaceMember(
            workspace_id=workspace_id,
            user_id=user_id,
            role=role,
            permissions=WorkspacePermissions.for_role(role),
            invited_by_user_id=invited_by_user_id,
            status="active" if invited_by_user_id is None else "invited",
        )

        result = await db.workspace_members.insert_one(member.to_dict())
        member.id = str(result.inserted_id)

        return member

    @staticmethod
    async def get_member(
        workspace_id: str,
        user_id: str,
        db: AsyncIOMotorDatabase
    ) -> Optional[WorkspaceMember]:
        """Get a workspace member"""
        doc = await db.workspace_members.find_one({
            "workspace_id": workspace_id,
            "user_id": user_id
        })

        if not doc:
            return None

        doc["_id"] = str(doc["_id"])
        return WorkspaceMember(**doc)

    @staticmethod
    async def get_workspace_members(
        workspace_id: str,
        db: AsyncIOMotorDatabase,
        include_inactive: bool = False
    ) -> List[WorkspaceMember]:
        """Get all members of a workspace"""
        query = {"workspace_id": workspace_id}
        if not include_inactive:
            query["status"] = "active"

        cursor = db.workspace_members.find(query)
        members = []

        async for doc in cursor:
            doc["_id"] = str(doc["_id"])
            members.append(WorkspaceMember(**doc))

        return members

    @staticmethod
    async def update_member_role(
        workspace_id: str,
        user_id: str,
        new_role: WorkspaceRole,
        db: AsyncIOMotorDatabase
    ) -> Optional[WorkspaceMember]:
        """Update a member's role and permissions"""
        member = await WorkspaceService.get_member(workspace_id, user_id, db)
        if not member:
            return None

        # Update role and reset permissions to role defaults
        permissions = WorkspacePermissions.for_role(new_role)

        await db.workspace_members.update_one(
            {"workspace_id": workspace_id, "user_id": user_id},
            {
                "$set": {
                    "role": new_role,
                    "permissions": permissions.model_dump(),
                    "custom_permissions": False,
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return await WorkspaceService.get_member(workspace_id, user_id, db)

    @staticmethod
    async def update_member_permissions(
        workspace_id: str,
        user_id: str,
        permissions: WorkspacePermissions,
        db: AsyncIOMotorDatabase
    ) -> Optional[WorkspaceMember]:
        """Update custom permissions for a member"""
        member = await WorkspaceService.get_member(workspace_id, user_id, db)
        if not member:
            return None

        await db.workspace_members.update_one(
            {"workspace_id": workspace_id, "user_id": user_id},
            {
                "$set": {
                    "permissions": permissions.model_dump(),
                    "custom_permissions": True,
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return await WorkspaceService.get_member(workspace_id, user_id, db)

    @staticmethod
    async def suspend_member(
        workspace_id: str,
        user_id: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """Suspend a workspace member"""
        result = await db.workspace_members.update_one(
            {"workspace_id": workspace_id, "user_id": user_id},
            {
                "$set": {
                    "status": "suspended",
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return result.modified_count > 0

    @staticmethod
    async def reactivate_member(
        workspace_id: str,
        user_id: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """Reactivate a suspended member"""
        result = await db.workspace_members.update_one(
            {"workspace_id": workspace_id, "user_id": user_id},
            {
                "$set": {
                    "status": "active",
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return result.modified_count > 0

    @staticmethod
    async def transfer_ownership(
        workspace_id: str,
        current_owner_id: str,
        new_owner_id: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """Transfer workspace ownership"""
        # Demote current owner to admin
        await db.workspace_members.update_one(
            {"workspace_id": workspace_id, "user_id": current_owner_id},
            {
                "$set": {
                    "role": WorkspaceRole.ADMIN,
                    "permissions": WorkspacePermissions.for_role(WorkspaceRole.ADMIN).model_dump(),
                    "updated_at": datetime.utcnow()
                }
            }
        )

        # Promote new owner
        result = await db.workspace_members.update_one(
            {"workspace_id": workspace_id, "user_id": new_owner_id},
            {
                "$set": {
                    "role": WorkspaceRole.OWNER,
                    "permissions": WorkspacePermissions.for_role(WorkspaceRole.OWNER).model_dump(),
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return result.modified_count > 0

    @staticmethod
    async def remove_member(
        workspace_id: str,
        user_id: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """Remove a member from workspace"""
        result = await db.workspace_members.update_one(
            {"workspace_id": workspace_id, "user_id": user_id},
            {
                "$set": {
                    "status": "removed",
                    "removed_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return result.modified_count > 0

    @staticmethod
    async def check_permission(
        workspace_id: str,
        user_id: str,
        permission: str,
        db: AsyncIOMotorDatabase
    ) -> bool:
        """
        Check if a user has a specific permission in workspace

        Args:
            workspace_id: Workspace ID
            user_id: User ID
            permission: Permission to check (e.g., "can_edit_all_content")
            db: Database connection

        Returns:
            True if user has permission, False otherwise
        """
        member = await WorkspaceService.get_member(workspace_id, user_id, db)
        if not member or member.status != "active":
            return False

        return member.has_permission(permission)

    @staticmethod
    async def get_member_count(
        workspace_id: str,
        db: AsyncIOMotorDatabase
    ) -> int:
        """Get number of active members in workspace"""
        count = await db.workspace_members.count_documents({
            "workspace_id": workspace_id,
            "status": "active"
        })
        return count

    @staticmethod
    async def increment_workspace_usage(
        workspace_id: str,
        metric: str,
        db: AsyncIOMotorDatabase,
        amount: int = 1
    ) -> bool:
        """Increment workspace usage statistics"""
        metric_map = {
            "content": ("usage_stats.total_content_generated", "usage_stats.content_generated_this_month"),
            "image": ("usage_stats.total_images_generated", "usage_stats.images_generated_this_month"),
            "post": ("usage_stats.total_posts_published", "usage_stats.posts_published_this_month"),
            "draft": ("usage_stats.total_drafts_created", None),
        }

        if metric not in metric_map:
            return False

        total_field, monthly_field = metric_map[metric]
        update_doc = {
            "$inc": {total_field: amount},
            "$set": {
                "usage_stats.last_activity_at": datetime.utcnow(),
                "updated_at": datetime.utcnow()
            }
        }

        if monthly_field:
            update_doc["$inc"][monthly_field] = amount

        result = await db.workspaces.update_one(
            {"workspace_id": workspace_id},
            update_doc
        )

        return result.modified_count > 0

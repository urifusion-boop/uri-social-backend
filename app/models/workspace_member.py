"""
Workspace Member Model - User membership in a workspace

Represents a user's membership and role within a workspace.
Controls permissions and access levels for team collaboration.
"""

from datetime import datetime
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field, EmailStr
from bson import ObjectId
from enum import Enum


class WorkspaceRole(str, Enum):
    """Roles available in a workspace"""
    OWNER = "owner"  # Full control, can delete workspace
    ADMIN = "admin"  # Can manage members, settings, and all content
    MEMBER = "member"  # Can create and manage own content
    VIEWER = "viewer"  # Read-only access


class WorkspacePermissions(BaseModel):
    """Granular permissions for workspace members"""
    # Content permissions
    can_create_content: bool = True
    can_edit_own_content: bool = True
    can_edit_all_content: bool = False
    can_delete_own_content: bool = True
    can_delete_all_content: bool = False
    can_publish_content: bool = True
    can_schedule_content: bool = True

    # Member management
    can_invite_members: bool = False
    can_remove_members: bool = False
    can_change_member_roles: bool = False

    # Workspace settings
    can_edit_workspace_settings: bool = False
    can_manage_brand_profile: bool = False
    can_manage_social_connections: bool = False

    # Billing
    can_view_billing: bool = False
    can_manage_billing: bool = False

    @classmethod
    def for_role(cls, role: WorkspaceRole) -> "WorkspacePermissions":
        """Get default permissions for a role"""
        if role == WorkspaceRole.OWNER:
            return cls(
                can_create_content=True,
                can_edit_own_content=True,
                can_edit_all_content=True,
                can_delete_own_content=True,
                can_delete_all_content=True,
                can_publish_content=True,
                can_schedule_content=True,
                can_invite_members=True,
                can_remove_members=True,
                can_change_member_roles=True,
                can_edit_workspace_settings=True,
                can_manage_brand_profile=True,
                can_manage_social_connections=True,
                can_view_billing=True,
                can_manage_billing=True,
            )
        elif role == WorkspaceRole.ADMIN:
            return cls(
                can_create_content=True,
                can_edit_own_content=True,
                can_edit_all_content=True,
                can_delete_own_content=True,
                can_delete_all_content=True,
                can_publish_content=True,
                can_schedule_content=True,
                can_invite_members=True,
                can_remove_members=True,
                can_change_member_roles=False,  # Cannot change owner
                can_edit_workspace_settings=True,
                can_manage_brand_profile=True,
                can_manage_social_connections=True,
                can_view_billing=True,
                can_manage_billing=False,
            )
        elif role == WorkspaceRole.MEMBER:
            return cls(
                can_create_content=True,
                can_edit_own_content=True,
                can_edit_all_content=False,
                can_delete_own_content=True,
                can_delete_all_content=False,
                can_publish_content=True,
                can_schedule_content=True,
                can_invite_members=False,
                can_remove_members=False,
                can_change_member_roles=False,
                can_edit_workspace_settings=False,
                can_manage_brand_profile=False,
                can_manage_social_connections=False,
                can_view_billing=False,
                can_manage_billing=False,
            )
        else:  # VIEWER
            return cls(
                can_create_content=False,
                can_edit_own_content=False,
                can_edit_all_content=False,
                can_delete_own_content=False,
                can_delete_all_content=False,
                can_publish_content=False,
                can_schedule_content=False,
                can_invite_members=False,
                can_remove_members=False,
                can_change_member_roles=False,
                can_edit_workspace_settings=False,
                can_manage_brand_profile=False,
                can_manage_social_connections=False,
                can_view_billing=False,
                can_manage_billing=False,
            )


class WorkspaceMember(BaseModel):
    """
    Workspace Member Model - User's membership in a workspace

    Represents the relationship between a user and a workspace.
    Controls access, permissions, and role within the workspace.
    """

    id: Optional[str] = Field(default=None, alias="_id")
    workspace_id: str = Field(..., description="Workspace this membership belongs to")
    user_id: str = Field(..., description="User who is a member")

    # Role & Permissions
    role: WorkspaceRole = Field(default=WorkspaceRole.MEMBER)
    permissions: WorkspacePermissions = Field(default_factory=lambda: WorkspacePermissions.for_role(WorkspaceRole.MEMBER))
    custom_permissions: bool = Field(default=False, description="Whether permissions have been customized")

    # Invitation details
    invited_by_user_id: Optional[str] = Field(None, description="User who invited this member")
    invitation_accepted_at: Optional[datetime] = None

    # Status
    status: str = Field(default="active", pattern="^(active|invited|suspended|removed)$")

    # Activity tracking
    last_activity_at: Optional[datetime] = None
    content_created_count: int = Field(default=0, ge=0)
    content_published_count: int = Field(default=0, ge=0)

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    removed_at: Optional[datetime] = None

    class Config:
        populate_by_name = True
        json_encoders = {
            ObjectId: str,
            datetime: lambda v: v.isoformat()
        }
        use_enum_values = True

    def update_role(self, new_role: WorkspaceRole, custom_permissions: bool = False):
        """Update member's role and permissions"""
        self.role = new_role
        if not custom_permissions:
            self.permissions = WorkspacePermissions.for_role(new_role)
            self.custom_permissions = False
        else:
            self.custom_permissions = True
        self.updated_at = datetime.utcnow()

    def update_permissions(self, permissions: WorkspacePermissions):
        """Update member's custom permissions"""
        self.permissions = permissions
        self.custom_permissions = True
        self.updated_at = datetime.utcnow()

    def accept_invitation(self):
        """Mark invitation as accepted"""
        if self.status == "invited":
            self.status = "active"
            self.invitation_accepted_at = datetime.utcnow()
            self.updated_at = datetime.utcnow()

    def suspend(self):
        """Suspend member access"""
        self.status = "suspended"
        self.updated_at = datetime.utcnow()

    def remove(self):
        """Remove member from workspace"""
        self.status = "removed"
        self.removed_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()

    def reactivate(self):
        """Reactivate a suspended member"""
        self.status = "active"
        self.updated_at = datetime.utcnow()

    def increment_activity(self, activity_type: str):
        """Increment activity counters"""
        if activity_type == "content_created":
            self.content_created_count += 1
        elif activity_type == "content_published":
            self.content_published_count += 1

        self.last_activity_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()

    def has_permission(self, permission: str) -> bool:
        """Check if member has a specific permission"""
        return getattr(self.permissions, permission, False)

    def can_manage_member(self, target_member: "WorkspaceMember") -> bool:
        """Check if this member can manage another member"""
        # Owner can manage anyone
        if self.role == WorkspaceRole.OWNER:
            return True

        # Admin can manage members and viewers, but not owners or other admins
        if self.role == WorkspaceRole.ADMIN:
            return target_member.role in [WorkspaceRole.MEMBER, WorkspaceRole.VIEWER]

        return False

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for database storage"""
        data = self.model_dump(by_alias=True, exclude_none=True)
        if self.id:
            data["_id"] = ObjectId(self.id)
        return data

    def to_public_dict(self, include_user_details: bool = False) -> Dict[str, Any]:
        """Convert to public dictionary (safe for API responses)"""
        result = {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "role": self.role,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "last_activity_at": self.last_activity_at.isoformat() if self.last_activity_at else None,
        }

        if include_user_details:
            result["permissions"] = self.permissions.model_dump()
            result["custom_permissions"] = self.custom_permissions
            result["content_created_count"] = self.content_created_count
            result["content_published_count"] = self.content_published_count

        return result


class InviteMemberRequest(BaseModel):
    """Request model for inviting a member to workspace"""
    email: EmailStr = Field(..., description="Email of user to invite")
    role: WorkspaceRole = Field(default=WorkspaceRole.MEMBER)
    message: Optional[str] = Field(None, max_length=500, description="Optional invitation message")


class UpdateMemberRoleRequest(BaseModel):
    """Request model for updating a member's role"""
    role: WorkspaceRole
    custom_permissions: Optional[WorkspacePermissions] = None


class UpdateMemberPermissionsRequest(BaseModel):
    """Request model for updating custom permissions"""
    permissions: WorkspacePermissions


class WorkspaceMemberResponse(BaseModel):
    """Response model for workspace member"""
    id: str
    workspace_id: str
    user_id: str
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    role: str
    status: str
    joined_at: str
    last_activity_at: Optional[str] = None

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat()
        }


class WorkspaceMemberDetailResponse(WorkspaceMemberResponse):
    """Detailed response model for workspace member"""
    permissions: Dict[str, bool]
    custom_permissions: bool
    content_created_count: int
    content_published_count: int

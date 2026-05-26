"""
Workspace Member Management Router

REST API endpoints for managing team members within workspaces.
Provides invitation, role management, and permission control.
"""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from typing import List, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.dependencies import get_db_dependency
from app.core.auth_bearer import JWTBearer
from app.models.workspace_member import (
    WorkspaceMember,
    WorkspaceRole,
    WorkspacePermissions,
    InviteMemberRequest,
    UpdateMemberRoleRequest,
    UpdateMemberPermissionsRequest,
    WorkspaceMemberResponse,
)
from app.services.WorkspaceService import WorkspaceService
from app.domain.responses.uri_response import UriResponse

router = APIRouter(prefix="/social-media/workspaces", tags=["Workspace Members"])


@router.post("/{workspace_id}/members/invite", status_code=status.HTTP_201_CREATED)
async def invite_member(
    workspace_id: str,
    request: InviteMemberRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Invite a new member to the workspace

    **Required Authentication**: Bearer token

    **Permissions**: User must have "can_invite_members" permission (owner/admin)

    The invited user will be added with the specified role.
    If the user doesn't exist, you can optionally send an invitation email.
    """
    # Check if workspace exists
    workspace = await WorkspaceService.get_workspace_by_id(workspace_id, db)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found"
        )

    # Check if current user has permission to invite
    has_permission = await WorkspaceService.check_permission(
        workspace_id=workspace_id,
        user_id=token["userId"],
        permission="can_invite_members",
        db=db
    )

    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to invite members"
        )

    # Check if user to invite exists
    user_to_invite = await db.users.find_one({"email": request.email})
    if not user_to_invite:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User with email {request.email} not found"
        )

    user_id_to_invite = user_to_invite["userId"]

    # Check if user is already a member
    existing_member = await WorkspaceService.get_member(
        workspace_id=workspace_id,
        user_id=user_id_to_invite,
        db=db
    )

    if existing_member and existing_member.status == "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User is already a member of this workspace"
        )

    # Add member
    member = await WorkspaceService.add_member(
        workspace_id=workspace_id,
        user_id=user_id_to_invite,
        role=request.role,
        invited_by_user_id=token["userId"],
        db=db
    )

    if not member:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to add member"
        )

    # TODO: Send invitation email if configured

    return UriResponse.success(
        data={
            "member_id": member.id,
            "workspace_id": member.workspace_id,
            "user_id": member.user_id,
            "email": request.email,
            "role": member.role,
            "permissions": member.permissions.model_dump(),
            "status": member.status,
            "invited_at": member.invited_at.isoformat(),
        },
        message=f"User {request.email} invited as {request.role}"
    )


@router.get("/{workspace_id}/members/{user_id}")
async def get_member(
    workspace_id: str,
    user_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Get detailed information about a workspace member

    **Required Authentication**: Bearer token

    **Permissions**: User must be a member of the workspace
    """
    # Check if current user is a member
    current_member = await WorkspaceService.get_member(
        workspace_id=workspace_id,
        user_id=token["userId"],
        db=db
    )

    if not current_member or current_member.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this workspace"
        )

    # Get requested member
    member = await WorkspaceService.get_member(
        workspace_id=workspace_id,
        user_id=user_id,
        db=db
    )

    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found"
        )

    # Get user details
    user_doc = await db.users.find_one({"userId": user_id})

    member_data = member.to_public_dict()
    if user_doc:
        member_data["user_email"] = user_doc.get("email")
        member_data["user_name"] = f"{user_doc.get('first_name', '')} {user_doc.get('last_name', '')}".strip()

    return UriResponse.success(data=member_data)


@router.patch("/{workspace_id}/members/{user_id}/role")
async def update_member_role(
    workspace_id: str,
    user_id: str,
    request: UpdateMemberRoleRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Update a member's role

    **Required Authentication**: Bearer token

    **Permissions**: User must have "can_manage_members" permission (owner/admin)

    **Note**: Cannot change the role of the workspace owner.
    """
    # Check permissions
    has_permission = await WorkspaceService.check_permission(
        workspace_id=workspace_id,
        user_id=token["userId"],
        permission="can_manage_members",
        db=db
    )

    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage members"
        )

    # Get member to update
    member = await WorkspaceService.get_member(workspace_id, user_id, db)
    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found"
        )

    # Cannot change owner role
    if member.role == WorkspaceRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot change the role of the workspace owner"
        )

    # Update role
    updated_member = await WorkspaceService.update_member_role(
        workspace_id=workspace_id,
        user_id=user_id,
        new_role=request.new_role,
        db=db
    )

    if not updated_member:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update member role"
        )

    return UriResponse.success(
        data=updated_member.to_public_dict(),
        message=f"Member role updated to {request.new_role}"
    )


@router.patch("/{workspace_id}/members/{user_id}/permissions")
async def update_member_permissions(
    workspace_id: str,
    user_id: str,
    request: UpdateMemberPermissionsRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Update custom permissions for a member

    **Required Authentication**: Bearer token

    **Permissions**: User must have "can_manage_members" permission (owner/admin)

    Allows fine-grained control over member permissions beyond their role.
    """
    # Check permissions
    has_permission = await WorkspaceService.check_permission(
        workspace_id=workspace_id,
        user_id=token["userId"],
        permission="can_manage_members",
        db=db
    )

    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage member permissions"
        )

    # Get member to update
    member = await WorkspaceService.get_member(workspace_id, user_id, db)
    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found"
        )

    # Update permissions
    updated_member = await WorkspaceService.update_member_permissions(
        workspace_id=workspace_id,
        user_id=user_id,
        permissions=request.permissions,
        db=db
    )

    if not updated_member:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update member permissions"
        )

    return UriResponse.success(
        data=updated_member.to_public_dict(),
        message="Member permissions updated successfully"
    )


@router.post("/{workspace_id}/members/{user_id}/suspend")
async def suspend_member(
    workspace_id: str,
    user_id: str,
    reason: Optional[str] = Query(None, description="Reason for suspension"),
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Suspend a workspace member

    **Required Authentication**: Bearer token

    **Permissions**: User must have "can_manage_members" permission (owner/admin)

    Suspended members cannot access the workspace but remain in the member list.
    """
    # Check permissions
    has_permission = await WorkspaceService.check_permission(
        workspace_id=workspace_id,
        user_id=token["userId"],
        permission="can_manage_members",
        db=db
    )

    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage members"
        )

    # Cannot suspend yourself
    if user_id == token["userId"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot suspend yourself"
        )

    # Get member to suspend
    member = await WorkspaceService.get_member(workspace_id, user_id, db)
    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found"
        )

    # Cannot suspend owner
    if member.role == WorkspaceRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot suspend the workspace owner"
        )

    # Suspend member
    success = await WorkspaceService.suspend_member(workspace_id, user_id, db)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to suspend member"
        )

    return UriResponse.success(message="Member suspended successfully")


@router.post("/{workspace_id}/members/{user_id}/reactivate")
async def reactivate_member(
    workspace_id: str,
    user_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Reactivate a suspended workspace member

    **Required Authentication**: Bearer token

    **Permissions**: User must have "can_manage_members" permission (owner/admin)
    """
    # Check permissions
    has_permission = await WorkspaceService.check_permission(
        workspace_id=workspace_id,
        user_id=token["userId"],
        permission="can_manage_members",
        db=db
    )

    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage members"
        )

    # Get member
    member = await WorkspaceService.get_member(workspace_id, user_id, db)
    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found"
        )

    if member.status != "suspended":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Member is {member.status}, not suspended"
        )

    # Reactivate member
    success = await WorkspaceService.reactivate_member(workspace_id, user_id, db)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reactivate member"
        )

    return UriResponse.success(message="Member reactivated successfully")


@router.delete("/{workspace_id}/members/{user_id}")
async def remove_member(
    workspace_id: str,
    user_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Remove a member from the workspace

    **Required Authentication**: Bearer token

    **Permissions**: User must have "can_manage_members" permission, OR user can remove themselves

    **Note**: Cannot remove the workspace owner. Owner must transfer ownership first.
    """
    # Check if user is removing themselves
    is_self_removal = user_id == token["userId"]

    if not is_self_removal:
        # Check permissions for removing others
        has_permission = await WorkspaceService.check_permission(
            workspace_id=workspace_id,
            user_id=token["userId"],
            permission="can_manage_members",
            db=db
        )

        if not has_permission:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to remove members"
            )

    # Get member to remove
    member = await WorkspaceService.get_member(workspace_id, user_id, db)
    if not member:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Member not found"
        )

    # Cannot remove owner
    if member.role == WorkspaceRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot remove the workspace owner. Transfer ownership first."
        )

    # Remove member
    success = await WorkspaceService.remove_member(workspace_id, user_id, db)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to remove member"
        )

    message = "You have left the workspace" if is_self_removal else "Member removed successfully"
    return UriResponse.success(message=message)


@router.post("/{workspace_id}/members/{user_id}/transfer-ownership")
async def transfer_ownership(
    workspace_id: str,
    user_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Transfer workspace ownership to another member

    **Required Authentication**: Bearer token

    **Permissions**: User must be the current workspace owner

    The current owner will be demoted to admin role.
    """
    # Check if current user is the owner
    current_member = await WorkspaceService.get_member(
        workspace_id=workspace_id,
        user_id=token["userId"],
        db=db
    )

    if not current_member or current_member.role != WorkspaceRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the workspace owner can transfer ownership"
        )

    # Get new owner
    new_owner = await WorkspaceService.get_member(workspace_id, user_id, db)
    if not new_owner:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Target member not found"
        )

    if new_owner.status != "active":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot transfer ownership to inactive member"
        )

    # Transfer ownership
    success = await WorkspaceService.transfer_ownership(
        workspace_id=workspace_id,
        current_owner_id=token["userId"],
        new_owner_id=user_id,
        db=db
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to transfer ownership"
        )

    return UriResponse.success(message="Ownership transferred successfully")

"""
Workspace Management Router

REST API endpoints for managing workspaces within clients.
Provides CRUD operations, member management, and usage tracking.
"""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from typing import List, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.dependencies import get_db_dependency
from app.core.auth_bearer import JWTBearer
from app.models.workspace import (
    Workspace,
    CreateWorkspaceRequest,
    UpdateWorkspaceRequest,
    WorkspaceResponse,
    WorkspaceDetailResponse,
)
from app.models.workspace_member import WorkspaceRole
from app.services.WorkspaceService import WorkspaceService
from app.services.ClientService import ClientService
from app.domain.responses.uri_response import UriResponse

router = APIRouter(prefix="/social-media/workspaces", tags=["Workspaces"])


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_workspace(
    client_id: str = Query(..., description="Client ID to create workspace under"),
    request: CreateWorkspaceRequest = ...,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Create a new workspace within a client

    **Required Authentication**: Bearer token

    **Permissions**: User must be the client owner or have workspace creation permission

    **Query Parameters**:
    - client_id: The client ID to create the workspace under

    The creator will automatically be added as workspace owner.
    """
    # Verify client exists
    client = await ClientService.get_client_by_id(client_id, db)
    if not client:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Client not found"
        )

    # Check if user has permission to create workspace
    # For now, only client owner can create workspaces
    # TODO: Allow workspace admins to create workspaces if client allows
    if client.owner_user_id != token["userId"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the client owner can create workspaces"
        )

    # Check workspace limit
    can_create = await ClientService.can_create_workspace(client_id, db)
    if not can_create:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Workspace limit reached. Maximum {client.subscription.max_workspaces} workspaces allowed."
        )

    # Create workspace
    workspace = await WorkspaceService.create_workspace(
        request=request,
        client_id=client_id,
        creator_user_id=token["userId"],
        db=db
    )

    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create workspace"
        )

    # Get member count
    member_count = await WorkspaceService.get_member_count(workspace.workspace_id, db)

    return UriResponse.success(
        data={
            "id": workspace.id,
            "workspace_id": workspace.workspace_id,
            "client_id": workspace.client_id,
            "name": workspace.name,
            "slug": workspace.slug,
            "description": workspace.description,
            "status": workspace.status,
            "member_count": member_count,
            "created_at": workspace.created_at.isoformat(),
        },
        message="Workspace created successfully"
    )


@router.get("/{workspace_id}")
async def get_workspace(
    workspace_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Get workspace details by workspace_id

    **Required Authentication**: Bearer token

    **Permissions**: User must be a member of the workspace
    """
    workspace = await WorkspaceService.get_workspace_by_id(workspace_id, db)

    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found"
        )

    # Check if user is a member
    member = await WorkspaceService.get_member(
        workspace_id=workspace_id,
        user_id=token["userId"],
        db=db
    )

    if not member or member.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this workspace"
        )

    # Get member count
    member_count = await WorkspaceService.get_member_count(workspace_id, db)

    # Build detailed response
    response_data = workspace.to_public_dict()
    response_data["member_count"] = member_count
    response_data["your_role"] = member.role
    response_data["your_permissions"] = member.permissions.model_dump()

    return UriResponse.success(data=response_data)


@router.get("/")
async def list_workspaces(
    client_id: Optional[str] = Query(None, description="Filter by client ID"),
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    List workspaces

    **Required Authentication**: Bearer token

    **Query Parameters**:
    - client_id: If provided, list workspaces for that client (user must be client owner)
    - If not provided, list all workspaces the current user is a member of

    Returns workspaces with the user's role in each workspace.
    """
    if client_id:
        # List workspaces for a specific client
        client = await ClientService.get_client_by_id(client_id, db)
        if not client:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Client not found"
            )

        # Check if user is client owner
        if client.owner_user_id != token["userId"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have access to this client's workspaces"
            )

        workspaces = await WorkspaceService.get_workspaces_by_client(
            client_id=client_id,
            include_archived=False,
            db=db
        )

        workspaces_data = []
        for workspace in workspaces:
            member_count = await WorkspaceService.get_member_count(workspace.workspace_id, db)
            workspace_dict = workspace.to_public_dict()
            workspace_dict["member_count"] = member_count
            workspaces_data.append(workspace_dict)

    else:
        # List all workspaces user is a member of
        workspaces_with_role = await WorkspaceService.get_workspaces_for_user(
            user_id=token["userId"],
            db=db
        )

        workspaces_data = []
        for item in workspaces_with_role:
            workspace_dict = item["workspace"]
            workspace_dict["your_role"] = item["role"]
            member_count = await WorkspaceService.get_member_count(workspace_dict["workspace_id"], db)
            workspace_dict["member_count"] = member_count
            workspaces_data.append(workspace_dict)

    return UriResponse.success(
        data=workspaces_data,
        message=f"Found {len(workspaces_data)} workspace(s)"
    )


@router.patch("/{workspace_id}")
async def update_workspace(
    workspace_id: str,
    request: UpdateWorkspaceRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Update workspace information

    **Required Authentication**: Bearer token

    **Permissions**: User must have "can_edit_workspace_settings" permission (owner/admin)
    """
    # Check if workspace exists
    workspace = await WorkspaceService.get_workspace_by_id(workspace_id, db)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found"
        )

    # Check permissions
    has_permission = await WorkspaceService.check_permission(
        workspace_id=workspace_id,
        user_id=token["userId"],
        permission="can_edit_workspace_settings",
        db=db
    )

    if not has_permission:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to edit workspace settings"
        )

    # Update workspace
    updated_workspace = await WorkspaceService.update_workspace(
        workspace_id=workspace_id,
        request=request,
        db=db
    )

    if not updated_workspace:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update workspace"
        )

    return UriResponse.success(
        data=updated_workspace.to_public_dict(),
        message="Workspace updated successfully"
    )


@router.post("/{workspace_id}/archive")
async def archive_workspace(
    workspace_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Archive a workspace

    **Required Authentication**: Bearer token

    **Permissions**: User must be workspace owner or client owner

    Archived workspaces are hidden but can be restored.
    """
    workspace = await WorkspaceService.get_workspace_by_id(workspace_id, db)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found"
        )

    # Check if user is owner
    member = await WorkspaceService.get_member(workspace_id, token["userId"], db)
    if not member or member.role != WorkspaceRole.OWNER:
        # Also check if user is client owner
        client = await ClientService.get_client_by_id(workspace.client_id, db)
        if not client or client.owner_user_id != token["userId"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only workspace owner or client owner can archive workspace"
            )

    success = await WorkspaceService.archive_workspace(workspace_id, db)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to archive workspace"
        )

    return UriResponse.success(message="Workspace archived successfully")


@router.post("/{workspace_id}/unarchive")
async def unarchive_workspace(
    workspace_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Unarchive a workspace

    **Required Authentication**: Bearer token

    **Permissions**: User must be workspace owner or client owner
    """
    workspace = await WorkspaceService.get_workspace_by_id(workspace_id, db)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found"
        )

    # Check permissions (same as archive)
    member = await WorkspaceService.get_member(workspace_id, token["userId"], db)
    if not member or member.role != WorkspaceRole.OWNER:
        client = await ClientService.get_client_by_id(workspace.client_id, db)
        if not client or client.owner_user_id != token["userId"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only workspace owner or client owner can unarchive workspace"
            )

    success = await WorkspaceService.unarchive_workspace(workspace_id, db)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to unarchive workspace"
        )

    return UriResponse.success(message="Workspace unarchived successfully")


@router.delete("/{workspace_id}")
async def delete_workspace(
    workspace_id: str,
    hard_delete: bool = Query(False, description="Permanently delete (cannot be undone)"),
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Delete a workspace

    **Required Authentication**: Bearer token

    **Permissions**: User must be workspace owner or client owner

    **Query Parameters**:
    - hard_delete: If true, permanently removes data. If false, soft delete (default).

    **Warning**: Deleting a workspace will remove all associated data including:
    - Content drafts
    - Social connections
    - Team members
    """
    workspace = await WorkspaceService.get_workspace_by_id(workspace_id, db)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found"
        )

    # Check permissions
    member = await WorkspaceService.get_member(workspace_id, token["userId"], db)
    if not member or member.role != WorkspaceRole.OWNER:
        client = await ClientService.get_client_by_id(workspace.client_id, db)
        if not client or client.owner_user_id != token["userId"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only workspace owner or client owner can delete workspace"
            )

    success = await WorkspaceService.delete_workspace(workspace_id, hard_delete, db)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete workspace"
        )

    delete_type = "permanently deleted" if hard_delete else "deleted"
    return UriResponse.success(message=f"Workspace {delete_type} successfully")


@router.get("/{workspace_id}/members")
async def list_workspace_members(
    workspace_id: str,
    include_inactive: bool = Query(False, description="Include suspended/removed members"),
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    List all members of a workspace

    **Required Authentication**: Bearer token

    **Permissions**: User must be a member of the workspace

    Returns list of members with their roles and basic info.
    For detailed member info, use the workspace-members endpoints.
    """
    # Check if user is a member
    member = await WorkspaceService.get_member(workspace_id, token["userId"], db)
    if not member or member.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this workspace"
        )

    # Get all members
    members = await WorkspaceService.get_workspace_members(
        workspace_id=workspace_id,
        include_inactive=include_inactive,
        db=db
    )

    # Get user details for each member
    members_data = []
    for member in members:
        # Get user info
        user_doc = await db.users.find_one({"userId": member.user_id})

        member_dict = member.to_public_dict()
        if user_doc:
            member_dict["user_email"] = user_doc.get("email")
            member_dict["user_name"] = f"{user_doc.get('first_name', '')} {user_doc.get('last_name', '')}".strip()

        members_data.append(member_dict)

    return UriResponse.success(
        data=members_data,
        message=f"Found {len(members_data)} member(s)"
    )


@router.get("/{workspace_id}/usage")
async def get_workspace_usage(
    workspace_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Get workspace usage statistics

    **Required Authentication**: Bearer token

    **Permissions**: User must be a workspace member

    Returns usage stats for the workspace including:
    - Content generated
    - Images created
    - Posts published
    - Member activity
    """
    # Check if user is a member
    member = await WorkspaceService.get_member(workspace_id, token["userId"], db)
    if not member or member.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this workspace"
        )

    workspace = await WorkspaceService.get_workspace_by_id(workspace_id, db)
    if not workspace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Workspace not found"
        )

    member_count = await WorkspaceService.get_member_count(workspace_id, db)

    usage_data = {
        "workspace_id": workspace_id,
        "workspace_name": workspace.name,
        "member_count": member_count,
        "usage_this_month": {
            "content_generated": workspace.usage_stats.content_generated_this_month,
            "images_generated": workspace.usage_stats.images_generated_this_month,
            "posts_published": workspace.usage_stats.posts_published_this_month,
        },
        "usage_all_time": {
            "content_generated": workspace.usage_stats.total_content_generated,
            "images_generated": workspace.usage_stats.total_images_generated,
            "posts_published": workspace.usage_stats.total_posts_published,
            "drafts_created": workspace.usage_stats.total_drafts_created,
        },
        "last_activity": workspace.usage_stats.last_activity_at.isoformat() if workspace.usage_stats.last_activity_at else None,
    }

    return UriResponse.success(data=usage_data)

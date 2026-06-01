"""
Client Management Router

REST API endpoints for managing clients (organizations).
Provides CRUD operations, credit management, and usage analytics.
"""

from fastapi import APIRouter, Depends, HTTPException, status, Query
from typing import List, Optional
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.dependencies import get_db_dependency
from app.core.auth_bearer import JWTBearer
from app.models.client import (
    Client,
    CreateClientRequest,
    UpdateClientRequest,
    ClientResponse,
)
from app.services.ClientService import ClientService
from app.domain.responses.uri_response import UriResponse

router = APIRouter(prefix="/social-media/clients", tags=["Clients"])


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_client(
    request: CreateClientRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Create a new client (organization)

    Creates a new client account with the current user as owner.
    Also creates a default workspace for the client.

    **Required Authentication**: Bearer token

    **Permissions**: Any authenticated user can create a client
    """
    try:
        # Create client
        client = await ClientService.create_client(
            request=request,
            owner_user_id=token["userId"],
            db=db
        )

        # Format response
        response_data = {
            "id": client.id,
            "client_id": client.client_id,
            "name": client.name,
            "slug": client.slug,
            "description": client.description,
            "subscription_tier": client.subscription.tier,
            "subscription_status": client.subscription.status,
            "credits_remaining": client.subscription.total_credits - client.subscription.used_credits,
            "max_workspaces": client.subscription.max_workspaces,
            "status": client.status,
            "created_at": client.created_at.isoformat(),
        }

        return UriResponse.success(
            data=response_data,
            message="Client created successfully"
        )

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create client: {str(e)}"
        )


@router.get("/{client_id}")
async def get_client(
    client_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Get client details by client_id

    **Required Authentication**: Bearer token

    **Permissions**: User must be owner or member of a workspace in this client
    """
    client = await ClientService.get_client_by_id(client_id, db)

    if not client:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Client not found"
        )

    # TODO: Check if user has access to this client (owner or workspace member)
    # For now, only allow owner
    if client.owner_user_id != token["userId"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this client"
        )

    return UriResponse.success(data=client.to_public_dict())


@router.get("/")
async def list_my_clients(
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    List all clients owned by the current user

    **Required Authentication**: Bearer token

    Returns clients where the current user is the owner.
    """
    clients = await ClientService.get_clients_by_owner(
        owner_user_id=token["userId"],
        db=db
    )

    clients_data = [client.to_public_dict() for client in clients]

    return UriResponse.success(
        data=clients_data,
        message=f"Found {len(clients_data)} client(s)"
    )


@router.patch("/{client_id}")
async def update_client(
    client_id: str,
    request: UpdateClientRequest,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Update client information

    **Required Authentication**: Bearer token

    **Permissions**: User must be the client owner
    """
    client = await ClientService.get_client_by_id(client_id, db)

    if not client:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Client not found"
        )

    # Check ownership
    if client.owner_user_id != token["userId"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the client owner can update client information"
        )

    # Update client
    updated_client = await ClientService.update_client(
        client_id=client_id,
        request=request,
        db=db
    )

    if not updated_client:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update client"
        )

    return UriResponse.success(
        data=updated_client.to_public_dict(),
        message="Client updated successfully"
    )


@router.get("/{client_id}/usage")
async def get_client_usage(
    client_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Get client usage summary and statistics

    **Required Authentication**: Bearer token

    **Permissions**: User must be client owner or workspace admin

    Returns:
    - Credit usage
    - Workspace count
    - Monthly and all-time usage statistics
    """
    client = await ClientService.get_client_by_id(client_id, db)

    if not client:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Client not found"
        )

    # Check access
    if client.owner_user_id != token["userId"]:
        # TODO: Also allow workspace admins
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this client's usage data"
        )

    usage_summary = await ClientService.get_client_usage_summary(client_id, db)

    return UriResponse.success(data=usage_summary)


@router.post("/{client_id}/credits/add")
async def add_credits(
    client_id: str,
    amount: int = Query(..., ge=1, le=1000000, description="Number of credits to add"),
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Add credits to client account

    **Required Authentication**: Bearer token

    **Permissions**: User must be client owner

    **Note**: In production, this should be restricted to admin users
    or tied to payment processing.
    """
    client = await ClientService.get_client_by_id(client_id, db)

    if not client:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Client not found"
        )

    # Check ownership
    if client.owner_user_id != token["userId"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the client owner can add credits"
        )

    success = await ClientService.add_credits(client_id, amount, db)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to add credits"
        )

    # Get updated client
    updated_client = await ClientService.get_client_by_id(client_id, db)

    return UriResponse.success(
        data={
            "credits_total": updated_client.subscription.total_credits,
            "credits_used": updated_client.subscription.used_credits,
            "credits_remaining": updated_client.subscription.total_credits - updated_client.subscription.used_credits,
            "amount_added": amount,
        },
        message=f"Added {amount} credits successfully"
    )


@router.post("/{client_id}/suspend")
async def suspend_client(
    client_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Suspend a client account

    **Required Authentication**: Bearer token

    **Permissions**: Admin only (TODO: Add admin check)

    Suspends the client and all associated workspaces.
    """
    # TODO: Add admin check
    # For now, only allow owner
    client = await ClientService.get_client_by_id(client_id, db)

    if not client:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Client not found"
        )

    if client.owner_user_id != token["userId"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions"
        )

    success = await ClientService.suspend_client(client_id, db)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to suspend client"
        )

    return UriResponse.success(message="Client suspended successfully")


@router.post("/{client_id}/reactivate")
async def reactivate_client(
    client_id: str,
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Reactivate a suspended client account

    **Required Authentication**: Bearer token

    **Permissions**: Admin only (TODO: Add admin check)
    """
    # TODO: Add admin check
    client = await ClientService.get_client_by_id(client_id, db)

    if not client:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Client not found"
        )

    if client.owner_user_id != token["userId"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions"
        )

    success = await ClientService.reactivate_client(client_id, db)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to reactivate client"
        )

    return UriResponse.success(message="Client reactivated successfully")


@router.delete("/{client_id}")
async def delete_client(
    client_id: str,
    hard_delete: bool = Query(False, description="Permanently delete (cannot be undone)"),
    token: dict = Depends(JWTBearer()),
    db: AsyncIOMotorDatabase = Depends(get_db_dependency)
):
    """
    Delete a client account

    **Required Authentication**: Bearer token

    **Permissions**: User must be client owner

    **Query Parameters**:
    - hard_delete: If true, permanently removes data. If false, soft delete (default).

    **Warning**: Deleting a client will also delete all associated workspaces and data.
    """
    client = await ClientService.get_client_by_id(client_id, db)

    if not client:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Client not found"
        )

    # Check ownership
    if client.owner_user_id != token["userId"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the client owner can delete the client"
        )

    success = await ClientService.delete_client(client_id, hard_delete, db)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete client"
        )

    delete_type = "permanently deleted" if hard_delete else "deleted"
    return UriResponse.success(message=f"Client {delete_type} successfully")

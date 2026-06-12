"""
Canvas Editor API Endpoints

Provides endpoints for:
- Fetching layered documents for editing
- Updating individual layers
- Undo/redo operations
- Rendering documents to PNG
- Converting aspect ratios
"""

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional
from datetime import datetime

from app.dependencies import get_db_dependency
from app.core.auth_bearer import JWTBearer
from app.domain.responses.uri_response import UriResponse

from ..services.layered_document_service import LayeredDocumentService
from ..services.document_edit_service import DocumentEditService
from ..services.document_renderer_service import DocumentRendererService

router = APIRouter(tags=["Canvas Editor"], prefix="/canvas-editor")


def _get_user_id(token: dict) -> str | None:
    """Extract user_id from JWT payload"""
    if not isinstance(token, dict):
        return None

    for k in ("user_id", "userId", "id", "sub"):
        v = token.get(k)
        if v:
            return str(v)

    claims = token.get("claims") or {}
    if isinstance(claims, dict):
        for k in ("userId", "user_id", "id", "sub"):
            v = claims.get(k)
            if v:
                return str(v)

    return None


class UpdateLayerRequest(BaseModel):
    layer_id: str
    updates: Dict[str, Any]


class RenderDocumentRequest(BaseModel):
    aspect_ratio: Optional[str] = "1:1"
    output_format: Optional[str] = "png"
    quality: Optional[int] = 95


class ReorderLayersRequest(BaseModel):
    layer_order: List[str] = Field(..., min_items=1)


@router.get("/drafts/{draft_id}/document")
async def get_draft_document(
    draft_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Get the layered document for a draft.

    Returns the complete layered JSON document with all layers,
    ready for canvas editor rendering.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        # Fetch draft from database
        draft = await db["content_drafts"].find_one(
            {"id": draft_id, "user_id": user_id},
            {"_id": 0}
        )

        if not draft:
            return UriResponse.error_response(
                f"Draft {draft_id} not found or access denied",
                code=404
            )

        document = draft.get("document")
        if not document:
            return UriResponse.error_response(
                f"Draft {draft_id} does not have a layered document. Canvas editor is not available for this draft.",
                code=404
            )

        return UriResponse.get_single_data_response("canvas_document", {
            "draft_id": draft_id,
            "document": document,
            "document_version": draft.get("document_version", 1),
            "preview_url": draft.get("preview_url") or draft.get("image_url"),
            "platform": draft.get("platform"),
            "created_at": draft.get("created_at")
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/drafts/{draft_id}/layers/{layer_id}/update")
async def update_layer(
    draft_id: str,
    layer_id: str,
    request: UpdateLayerRequest,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Update a specific layer's properties.

    Supports updating:
    - Text content
    - Position (x, y)
    - Font properties (size, weight, color, family)
    - Visibility
    - Lock status
    - Size (width, height)

    Automatically tracks the edit for undo/redo.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        # Fetch current draft
        draft = await db["content_drafts"].find_one(
            {"id": draft_id, "user_id": user_id},
            {"_id": 0}
        )

        if not draft:
            return UriResponse.error_response(
                f"Draft {draft_id} not found or access denied",
                code=404
            )

        document = draft.get("document")
        if not document:
            return UriResponse.error_response(
                f"Draft {draft_id} does not have a layered document",
                code=404
            )

        # Get current layer state (for undo tracking)
        current_layer = LayeredDocumentService.get_layer_by_id(document, layer_id)
        if not current_layer:
            return UriResponse.error_response(
                f"Layer {layer_id} not found in document",
                code=404
            )

        # Check if layer is locked
        if current_layer.get("locked", False) and "locked" not in request.updates:
            return UriResponse.error_response(
                f"Layer {layer_id} is locked and cannot be edited",
                code=403
            )

        # Capture before state (only fields being updated)
        before_state = {k: current_layer.get(k) for k in request.updates.keys()}

        # Apply updates
        document = LayeredDocumentService.update_layer(
            document,
            layer_id,
            request.updates
        )

        # Increment document version
        document = LayeredDocumentService.increment_version(document)

        # Record edit for undo/redo
        edit_type = "text_change" if "content" in request.updates else \
                   "move" if "x" in request.updates or "y" in request.updates else \
                   "resize" if "width" in request.updates or "height" in request.updates else \
                   "color" if "color" in request.updates else \
                   "visibility" if "visible" in request.updates else \
                   "property_change"

        await DocumentEditService.record_edit(
            draft_id=draft_id,
            user_id=user_id,
            edit_type=edit_type,
            layer_id=layer_id,
            before_state=before_state,
            after_state=request.updates,
            db=db
        )

        # Save updated document
        await db["content_drafts"].update_one(
            {"id": draft_id},
            {
                "$set": {
                    "document": document,
                    "document_version": document["version"],
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return UriResponse.get_single_data_response("layer_updated", {
            "draft_id": draft_id,
            "layer_id": layer_id,
            "document": document,
            "document_version": document["version"]
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/drafts/{draft_id}/undo")
async def undo_edit(
    draft_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Undo the last edit operation.

    Reverts the document to the state before the last edit
    and marks the edit as undone (enabling redo).
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        # Fetch current draft
        draft = await db["content_drafts"].find_one(
            {"id": draft_id, "user_id": user_id},
            {"_id": 0}
        )

        if not draft:
            return UriResponse.error_response(
                f"Draft {draft_id} not found or access denied",
                code=404
            )

        document = draft.get("document")
        if not document:
            return UriResponse.error_response(
                f"Draft {draft_id} does not have a layered document",
                code=404
            )

        # Apply undo
        updated_document = await DocumentEditService.apply_undo(
            draft_id=draft_id,
            document=document,
            db=db
        )

        if not updated_document:
            return UriResponse.error_response(
                "Nothing to undo",
                code=400
            )

        # Increment version
        updated_document = LayeredDocumentService.increment_version(updated_document)

        # Save updated document
        await db["content_drafts"].update_one(
            {"id": draft_id},
            {
                "$set": {
                    "document": updated_document,
                    "document_version": updated_document["version"],
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return UriResponse.get_single_data_response("undo_applied", {
            "draft_id": draft_id,
            "document": updated_document,
            "document_version": updated_document["version"]
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/drafts/{draft_id}/redo")
async def redo_edit(
    draft_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Redo the last undone edit operation.

    Reapplies the edit that was previously undone.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        # Fetch current draft
        draft = await db["content_drafts"].find_one(
            {"id": draft_id, "user_id": user_id},
            {"_id": 0}
        )

        if not draft:
            return UriResponse.error_response(
                f"Draft {draft_id} not found or access denied",
                code=404
            )

        document = draft.get("document")
        if not document:
            return UriResponse.error_response(
                f"Draft {draft_id} does not have a layered document",
                code=404
            )

        # Apply redo
        updated_document = await DocumentEditService.apply_redo(
            draft_id=draft_id,
            document=document,
            db=db
        )

        if not updated_document:
            return UriResponse.error_response(
                "Nothing to redo",
                code=400
            )

        # Increment version
        updated_document = LayeredDocumentService.increment_version(updated_document)

        # Save updated document
        await db["content_drafts"].update_one(
            {"id": draft_id},
            {
                "$set": {
                    "document": updated_document,
                    "document_version": updated_document["version"],
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return UriResponse.get_single_data_response("redo_applied", {
            "draft_id": draft_id,
            "document": updated_document,
            "document_version": updated_document["version"]
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/drafts/{draft_id}/render")
async def render_document(
    draft_id: str,
    request: RenderDocumentRequest,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Render the layered document to a final PNG/JPG image.

    Supports different aspect ratios for multi-format export:
    - 1:1 (Instagram square)
    - 9:16 (Instagram Story)
    - 4:5 (Instagram Portrait)
    - 16:9 (YouTube, LinkedIn)

    Returns base64 encoded image ready for download/upload.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        # Fetch current draft
        draft = await db["content_drafts"].find_one(
            {"id": draft_id, "user_id": user_id},
            {"_id": 0}
        )

        if not draft:
            return UriResponse.error_response(
                f"Draft {draft_id} not found or access denied",
                code=404
            )

        document = draft.get("document")
        if not document:
            return UriResponse.error_response(
                f"Draft {draft_id} does not have a layered document",
                code=404
            )

        # Adjust canvas for aspect ratio if needed
        if request.aspect_ratio and request.aspect_ratio != "1:1":
            # Clone document and adjust dimensions
            import copy
            document = copy.deepcopy(document)

            aspect_ratios = {
                "9:16": {"width": 1080, "height": 1920},
                "4:5": {"width": 1080, "height": 1350},
                "16:9": {"width": 1920, "height": 1080},
                "1:1": {"width": 1080, "height": 1080},
            }

            dims = aspect_ratios.get(request.aspect_ratio)
            if dims:
                document["canvas"]["width"] = dims["width"]
                document["canvas"]["height"] = dims["height"]
                document["canvas"]["aspect_ratio"] = request.aspect_ratio

        # Render to PNG bytes
        image_bytes = await DocumentRendererService.render_to_png(
            document=document,
            output_format=request.output_format,
            quality=request.quality
        )

        # Convert to base64 for response
        import base64
        image_base64 = base64.b64encode(image_bytes).decode('utf-8')
        data_url = f"data:image/{request.output_format};base64,{image_base64}"

        # Optionally upload to Cloudinary for permanent storage
        from app.utils.cloudinary_upload import upload_base64
        permanent_url = await upload_base64(data_url, folder="uri-social/canvas-renders")

        return UriResponse.get_single_data_response("document_rendered", {
            "draft_id": draft_id,
            "aspect_ratio": request.aspect_ratio,
            "output_format": request.output_format,
            "image_url": permanent_url,
            "image_base64": data_url,
            "size_bytes": len(image_bytes)
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/drafts/{draft_id}/layers/reorder")
async def reorder_layers(
    draft_id: str,
    request: ReorderLayersRequest,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Reorder layers by providing new layer order.

    Useful for bringing layers to front/back.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        # Fetch current draft
        draft = await db["content_drafts"].find_one(
            {"id": draft_id, "user_id": user_id},
            {"_id": 0}
        )

        if not draft:
            return UriResponse.error_response(
                f"Draft {draft_id} not found or access denied",
                code=404
            )

        document = draft.get("document")
        if not document:
            return UriResponse.error_response(
                f"Draft {draft_id} does not have a layered document",
                code=404
            )

        # Apply reordering
        document = LayeredDocumentService.reorder_layers(
            document,
            request.layer_order
        )

        # Increment version
        document = LayeredDocumentService.increment_version(document)

        # Save updated document
        await db["content_drafts"].update_one(
            {"id": draft_id},
            {
                "$set": {
                    "document": document,
                    "document_version": document["version"],
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return UriResponse.get_single_data_response("layers_reordered", {
            "draft_id": draft_id,
            "document": document,
            "document_version": document["version"]
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/drafts/{draft_id}/layers/{layer_id}")
async def delete_layer(
    draft_id: str,
    layer_id: str,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Delete a layer from the document.

    Note: Background layers (locked) cannot be deleted.
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        # Fetch current draft
        draft = await db["content_drafts"].find_one(
            {"id": draft_id, "user_id": user_id},
            {"_id": 0}
        )

        if not draft:
            return UriResponse.error_response(
                f"Draft {draft_id} not found or access denied",
                code=404
            )

        document = draft.get("document")
        if not document:
            return UriResponse.error_response(
                f"Draft {draft_id} does not have a layered document",
                code=404
            )

        # Check if layer exists and is not locked
        layer = LayeredDocumentService.get_layer_by_id(document, layer_id)
        if not layer:
            return UriResponse.error_response(
                f"Layer {layer_id} not found",
                code=404
            )

        if layer.get("locked", False):
            return UriResponse.error_response(
                f"Layer {layer_id} is locked and cannot be deleted",
                code=403
            )

        # Delete layer
        document = LayeredDocumentService.delete_layer(document, layer_id)

        # Increment version
        document = LayeredDocumentService.increment_version(document)

        # Save updated document
        await db["content_drafts"].update_one(
            {"id": draft_id},
            {
                "$set": {
                    "document": document,
                    "document_version": document["version"],
                    "updated_at": datetime.utcnow()
                }
            }
        )

        return UriResponse.get_single_data_response("layer_deleted", {
            "draft_id": draft_id,
            "layer_id": layer_id,
            "document": document,
            "document_version": document["version"]
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/drafts/{draft_id}/edit-history")
async def get_edit_history(
    draft_id: str,
    limit: int = 50,
    db: AsyncIOMotorDatabase = Depends(get_db_dependency),
    token: dict = Depends(JWTBearer())
):
    """
    Get edit history for a draft.

    Returns list of all edits (for debugging/analytics).
    """
    user_id = _get_user_id(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="User ID not found in token")

    try:
        # Verify draft ownership
        draft = await db["content_drafts"].find_one(
            {"id": draft_id, "user_id": user_id},
            {"_id": 0, "id": 1}
        )

        if not draft:
            return UriResponse.error_response(
                f"Draft {draft_id} not found or access denied",
                code=404
            )

        # Get edit history
        edits = await DocumentEditService.get_edit_history(
            draft_id=draft_id,
            db=db,
            limit=limit
        )

        return UriResponse.get_single_data_response("edit_history", {
            "draft_id": draft_id,
            "total_edits": len(edits),
            "edits": edits
        })

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# app/routers/ai_marketing_image_router.py

from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional, Dict, List, Any
from pydantic import BaseModel, Field

from app.services.AIMarketingImageService import AIMarketingImageService
from app.dependencies import get_current_workspace_context
from app.domain.responses.uri_response import UriResponse


router = APIRouter(prefix="/ai-marketing-images", tags=["AI Marketing Images"])


# Request/Response Models
class GenerateImageRequest(BaseModel):
    """Request model for generating an AI marketing image"""
    template_id: str = Field(..., description="Template ID to use")
    variables: Dict[str, str] = Field(..., description="Variables to replace in template")
    aspect_ratio: Optional[str] = Field(None, description="Override aspect ratio (1:1, 4:5, 16:9, 9:16)")

    class Config:
        json_schema_extra = {
            "example": {
                "template_id": "modern-doodle-collage",
                "variables": {
                    "PRODUCT_NAME": "URISocial SDK",
                    "BRAND": "URISocial"
                },
                "aspect_ratio": "4:5"
            }
        }


class TemplateResponse(BaseModel):
    """Response model for template data"""
    id: str
    template_id: str
    name: str
    description: str
    category: str
    default_aspect_ratio: str
    default_size: str
    variables: List[str]
    example_images: List[str]
    is_premium: bool
    usage_count: int


# Endpoints
@router.get("/templates")
async def list_templates(
    category: Optional[str] = Query(None, description="Filter by category"),
    current_context: dict = Depends(get_current_workspace_context)
) -> Dict[str, Any]:
    """
    List all available AI marketing image templates

    Returns templates filtered by category (optional) and user's workspace.
    Includes both global templates and workspace-specific templates.
    """
    user_id = current_context.get("user_id")
    workspace_id = current_context.get("workspace_id")

    templates = await AIMarketingImageService.list_templates(
        category=category,
        workspace_id=workspace_id
    )

    return UriResponse.get_list_data_response(
        entity_name="AI Marketing Template",
        data=templates
    )


@router.get("/templates/{template_id}")
async def get_template(
    template_id: str,
    current_context: dict = Depends(get_current_workspace_context)
) -> Dict[str, Any]:
    """
    Get details of a specific template

    Returns complete template structure including all sections and variables.
    """
    template = await AIMarketingImageService.get_template(template_id)

    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")

    return UriResponse.get_single_data_response(
        entity_name="AI Marketing Template",
        data=template
    )


@router.post("/generate")
async def generate_image(
    request: GenerateImageRequest,
    current_context: dict = Depends(get_current_workspace_context)
) -> Dict[str, Any]:
    """
    Generate an AI marketing image using a template

    Takes a template ID and variable replacements, generates a complete prompt,
    and creates the image using DALL-E 3.

    Returns the generated image URL and metadata.
    """
    user_id = current_context.get("user_id")
    workspace_id = current_context.get("workspace_id")

    result = await AIMarketingImageService.generate_image(
        user_id=user_id,
        template_id=request.template_id,
        variables=request.variables,
        workspace_id=workspace_id,
        aspect_ratio=request.aspect_ratio
    )

    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error", "Image generation failed")
        )

    generation_data = {
        "generation_id": result["generation_id"],
        "image_url": result["image_url"],
        "dalle_url": result.get("dalle_url"),
        "prompt": result["prompt"],
        "template_name": result["template_name"],
        "size": result["size"],
        "aspect_ratio": result["aspect_ratio"],
        "status": result.get("status", "completed"),
    }

    return UriResponse.create_response(
        entity_name="AI Marketing Image",
        data=generation_data,
        message="Image generated successfully"
    )


@router.get("/generations")
async def list_generations(
    limit: int = Query(50, ge=1, le=100, description="Number of generations to return"),
    current_context: dict = Depends(get_current_workspace_context)
):
    """
    Get user's generation history

    Returns a list of previously generated images with metadata.
    """
    user_id = current_context.get("user_id")
    workspace_id = current_context.get("workspace_id")

    generations = await AIMarketingImageService.get_generation_history(
        user_id=user_id,
        workspace_id=workspace_id,
        limit=limit
    )

    return {
        "generations": generations,
        "count": len(generations)
    }


@router.get("/stats")
async def get_stats(
    current_context: dict = Depends(get_current_workspace_context)
):
    """
    Get user's AI image generation statistics

    Returns total generations, success rate, and cost information.
    """
    user_id = current_context.get("user_id")

    stats = await AIMarketingImageService.get_generation_stats(user_id)

    return stats


@router.get("/categories")
async def list_categories(
    current_context: dict = Depends(get_current_workspace_context)
):
    """
    Get all available template categories

    Returns a list of unique categories for filtering templates.
    """
    from app.database import get_db

    db = get_db()
    collection = db["ai_prompt_templates"]

    # Get distinct categories
    pipeline = [
        {"$match": {"is_active": True}},
        {"$group": {"_id": "$category", "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}}
    ]

    cursor = collection.aggregate(pipeline)
    results = await cursor.to_list(length=None)

    categories = [
        {"category": r["_id"], "template_count": r["count"]}
        for r in results
    ]

    return {"categories": categories}

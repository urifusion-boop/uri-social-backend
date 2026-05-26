# app/services/AIMarketingImageService.py

import re
from typing import Dict, List, Any, Optional
from datetime import datetime
from app.models.ai_prompt_template import PromptTemplate, AIImageGeneration, PromptSection
from app.agents.social_media_manager.services.image_content_service import ImageContentService


class AIMarketingImageService:
    """
    Service for AI Marketing Image Generation using structured prompt templates.

    Integrates with existing ImageContentService for DALL-E generation while
    adding structured template management for marketing content.
    """

    @staticmethod
    async def list_templates(
        category: Optional[str] = None,
        workspace_id: Optional[str] = None,
        is_premium: Optional[bool] = None,
    ) -> List[Dict[str, Any]]:
        """
        List available prompt templates with optional filters

        Args:
            category: Filter by category (fashion, product, food, etc.)
            workspace_id: Filter by workspace (for custom templates)
            is_premium: Filter by premium status

        Returns:
            List of template dictionaries
        """
        query = {"is_active": True}

        if category:
            query["category"] = category

        if workspace_id:
            query["$or"] = [
                {"workspace_id": None},  # Global templates
                {"workspace_id": workspace_id},  # Workspace templates
            ]

        if is_premium is not None:
            query["is_premium"] = is_premium

        templates = await PromptTemplate.find(query).to_list()
        return [template.to_dict() for template in templates]

    @staticmethod
    async def get_template(template_id: str) -> Optional[PromptTemplate]:
        """Get a specific template by ID"""
        return await PromptTemplate.find_one({"template_id": template_id, "is_active": True})

    @staticmethod
    def build_prompt_from_template(
        template: PromptTemplate,
        variables: Dict[str, str]
    ) -> str:
        """
        Build complete AI prompt from template and variable replacements

        Args:
            template: PromptTemplate document
            variables: Dict of variable replacements
                      e.g., {"PRODUCT_NAME": "URISocial SDK", "BRAND": "URISocial"}

        Returns:
            Complete formatted prompt ready for DALL-E
        """
        prompt_parts = []

        # Build prompt from sections
        for section in template.sections:
            # Add section header
            prompt_parts.append(f"{section.name}:")

            # Add section content
            for line in section.content:
                prompt_parts.append(line)

            # Add spacing between sections
            prompt_parts.append("")

        # Join all parts
        full_prompt = "\n".join(prompt_parts)

        # Replace variables (case-insensitive)
        for var_name, var_value in variables.items():
            # Try with brackets first [VARIABLE]
            pattern1 = re.compile(re.escape(f"[{var_name}]"), re.IGNORECASE)
            full_prompt = pattern1.sub(var_value, full_prompt)

            # Try without brackets for flexibility
            pattern2 = re.compile(re.escape(var_name), re.IGNORECASE)
            full_prompt = pattern2.sub(var_value, full_prompt)

        return full_prompt

    @staticmethod
    def convert_aspect_ratio_to_size(aspect_ratio: str) -> str:
        """
        Convert aspect ratio to DALL-E compatible size

        DALL-E 3 supports: 1024x1024, 1024x1792, 1792x1024
        """
        aspect_map = {
            "1:1": "1024x1024",     # Square
            "square": "1024x1024",
            "4:5": "1024x1792",     # Portrait (Instagram)
            "portrait": "1024x1792",
            "9:16": "1024x1792",    # Story format
            "story": "1024x1792",
            "5:4": "1792x1024",     # Landscape
            "landscape": "1792x1024",
            "16:9": "1792x1024",
        }

        return aspect_map.get(aspect_ratio.lower(), "1024x1024")

    @staticmethod
    async def generate_image(
        user_id: str,
        template_id: str,
        variables: Dict[str, str],
        workspace_id: Optional[str] = None,
        aspect_ratio: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generate AI marketing image using template

        Args:
            user_id: User generating the image
            template_id: Template to use
            variables: Variable replacements for the template
            workspace_id: Optional workspace context
            aspect_ratio: Override default aspect ratio

        Returns:
            Dict with generation result:
            {
                "success": bool,
                "generation_id": str,
                "image_url": str,
                "prompt": str,
                "error": str (if failed)
            }
        """
        start_time = datetime.utcnow()

        # Get template
        template = await AIMarketingImageService.get_template(template_id)
        if not template:
            return {
                "success": False,
                "error": f"Template '{template_id}' not found"
            }

        # Build prompt
        prompt = AIMarketingImageService.build_prompt_from_template(template, variables)

        # Determine size
        final_aspect_ratio = aspect_ratio or template.default_aspect_ratio
        size = AIMarketingImageService.convert_aspect_ratio_to_size(final_aspect_ratio)

        # Create generation record
        generation = AIImageGeneration(
            user_id=user_id,
            workspace_id=workspace_id,
            template_id=template_id,
            template_name=template.name,
            prompt=prompt,
            variables=variables,
            size=size,
            aspect_ratio=final_aspect_ratio,
            status="pending",
            provider="dall-e-3",
            model="dall-e-3",
        )
        await generation.save()

        try:
            # Call existing DALL-E service (reuses existing integration)
            result = await ImageContentService._call_dalle_api(
                prompt=prompt,
                size=size,
                image_model="dall-e-3"
            )

            if result.get("success"):
                # Update generation record with success
                generation.status = "completed"
                generation.dalle_url = result.get("url")
                generation.image_url = result.get("url")  # Can be uploaded to Cloudinary later

                # Calculate generation time
                end_time = datetime.utcnow()
                generation.generation_time_ms = int((end_time - start_time).total_seconds() * 1000)

                # Track cost (DALL-E 3 standard pricing)
                if size == "1024x1024":
                    generation.cost_usd = 0.04
                else:  # 1024x1792 or 1792x1024
                    generation.cost_usd = 0.08

                # Update template usage count
                await PromptTemplate.find_one({"template_id": template_id}).update(
                    {"$inc": {"usage_count": 1}}
                )

                await generation.save()

                return {
                    "success": True,
                    "generation_id": str(generation.id),
                    "image_url": generation.image_url,
                    "prompt": prompt,
                    "template_name": template.name,
                    "size": size,
                    "aspect_ratio": final_aspect_ratio,
                }
            else:
                # Update generation record with failure
                generation.status = "failed"
                generation.error_message = result.get("error", "Unknown error")
                await generation.save()

                return {
                    "success": False,
                    "error": result.get("error", "Image generation failed"),
                    "generation_id": str(generation.id),
                }

        except Exception as e:
            # Update generation record with error
            generation.status = "failed"
            generation.error_message = str(e)
            await generation.save()

            return {
                "success": False,
                "error": f"Generation error: {str(e)}",
                "generation_id": str(generation.id),
            }

    @staticmethod
    async def get_generation_history(
        user_id: str,
        workspace_id: Optional[str] = None,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get user's generation history"""
        query = {"user_id": user_id}
        if workspace_id:
            query["workspace_id"] = workspace_id

        generations = await AIImageGeneration.find(query) \
            .sort([("created_at", -1)]) \
            .limit(limit) \
            .to_list()

        return [gen.to_dict() for gen in generations]

    @staticmethod
    async def get_generation_stats(user_id: str) -> Dict[str, Any]:
        """Get user's generation statistics"""
        pipeline = [
            {"$match": {"user_id": user_id}},
            {"$group": {
                "_id": None,
                "total_generations": {"$sum": 1},
                "successful_generations": {
                    "$sum": {"$cond": [{"$eq": ["$status", "completed"]}, 1, 0]}
                },
                "total_cost": {"$sum": "$cost_usd"},
            }}
        ]

        result = await AIImageGeneration.aggregate(pipeline).to_list()

        if result:
            stats = result[0]
            return {
                "total_generations": stats.get("total_generations", 0),
                "successful_generations": stats.get("successful_generations", 0),
                "total_cost_usd": round(stats.get("total_cost", 0), 2),
            }

        return {
            "total_generations": 0,
            "successful_generations": 0,
            "total_cost_usd": 0.0,
        }

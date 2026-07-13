"""
Vendor Configuration for Template Rendering
Orshot (primary) and Placid (fallback)
"""

import os
from typing import Dict, Optional


# ============================================================================
# VENDOR CONFIGURATION
# ============================================================================

class VendorConfig:
    """Configuration for template rendering vendors"""

    # Orshot (Primary Vendor)
    ORSHOT_ENABLED = os.getenv("ORSHOT_ENABLED", "false").lower() == "true"
    ORSHOT_API_KEY = os.getenv("ORSHOT_API_KEY", "")
    ORSHOT_API_URL = "https://api.orshot.com/v1"
    ORSHOT_COST_PER_RENDER = 0.026  # $0.026 per render

    # Placid (Fallback Vendor)
    PLACID_ENABLED = os.getenv("PLACID_ENABLED", "false").lower() == "true"
    PLACID_API_KEY = os.getenv("PLACID_API_KEY", "")
    PLACID_API_URL = "https://api.placid.app/api/rest"
    PLACID_COST_PER_RENDER = 0.05  # Higher cost, no carousel support

    # Background Removal
    removebg_api_key = os.getenv("REMOVE_BG_API_KEY", "")

    # Feature flags
    VISUAL_ENGINE_V2_ENABLED = os.getenv("VISUAL_ENGINE_V2_ENABLED", "true").lower() == "true"

    @classmethod
    def is_orshot_available(cls) -> bool:
        """Check if Orshot is configured and enabled"""
        return cls.ORSHOT_ENABLED and bool(cls.ORSHOT_API_KEY)

    @classmethod
    def is_placid_available(cls) -> bool:
        """Check if Placid is configured and enabled"""
        return cls.PLACID_ENABLED and bool(cls.PLACID_API_KEY)

    @classmethod
    def get_primary_vendor(cls) -> str:
        """Get primary rendering vendor"""
        if cls.is_orshot_available():
            return "orshot"
        elif cls.is_placid_available():
            return "placid"
        else:
            return "none"

    @classmethod
    def get_render_cost(cls, vendor: str = "auto") -> float:
        """Get per-render cost for vendor"""
        if vendor == "auto":
            vendor = cls.get_primary_vendor()

        if vendor == "orshot":
            return cls.ORSHOT_COST_PER_RENDER
        elif vendor == "placid":
            return cls.PLACID_COST_PER_RENDER
        else:
            return 0.0


# ============================================================================
# COST MODEL (from PRD Section 11)
# ============================================================================

class CostModel:
    """Per-unit costs as per PRD"""

    def __init__(self):
        # AI content generation
        self.content_generation_cost = 0.03  # Claude/GPT text generation
        self.carousel_content_cost = 0.03  # Same - one call plans all slides

        # Image generation (Path A)
        self.image_generation_cost = 0.04  # GPT Image 2 generation

        # Image cleanup (Path B)
        self.background_removal_cost = 0.01  # Background removal API
        self.reframe_cost = 0.005  # Smart crop/reframe
        self.ai_recomposite_cost = 0.10  # Premium AI editing (opt-in)

        # Template rendering
        self.template_render_cost = VendorConfig.ORSHOT_COST_PER_RENDER

    # Class-level constants for backward compatibility
    AI_CONTENT_COST_PER_POST = 0.03
    AI_CONTENT_COST_PER_CAROUSEL = 0.03
    GPT_IMAGE_2_COST_PER_IMAGE = 0.04
    BACKGROUND_REMOVAL_COST = 0.01
    REFRAME_COST = 0.005
    AI_RECOMPOSITE_COST = 0.10
    TEMPLATE_RENDER_COST = VendorConfig.ORSHOT_COST_PER_RENDER

    @classmethod
    def calculate_post_cost(
        cls,
        image_path: str = "path_a",  # "path_a" or "path_b"
        cleanup_level: str = "background_removal",
        num_formats: int = 1,
        is_carousel: bool = False,
        num_slides: int = 1
    ) -> Dict[str, float]:
        """Calculate per-post cost breakdown"""

        costs = {
            "ai_content": cls.AI_CONTENT_COST_PER_CAROUSEL if is_carousel else cls.AI_CONTENT_COST_PER_POST,
            "image_generation": 0.0,
            "image_cleanup": 0.0,
            "template_render": 0.0
        }

        # Image costs (per slide if carousel)
        if image_path == "path_a":
            costs["image_generation"] = cls.GPT_IMAGE_2_COST_PER_IMAGE * num_slides
        elif image_path == "path_b":
            if cleanup_level == "background_removal":
                costs["image_cleanup"] = cls.BACKGROUND_REMOVAL_COST * num_slides
            elif cleanup_level == "reframe":
                costs["image_cleanup"] = cls.REFRAME_COST * num_slides
            elif cleanup_level == "ai_recomposite":
                costs["image_cleanup"] = cls.AI_RECOMPOSITE_COST * num_slides

        # Rendering costs (per slide × per format)
        total_renders = num_slides * num_formats
        costs["template_render"] = cls.TEMPLATE_RENDER_COST * total_renders

        costs["total"] = sum(costs.values())

        return costs


# VendorConfig and CostModel were always meant to be used together
# (every call site does `self.vendor_config.cost_model.xxx`), but nothing
# ever actually attached one to the other — VendorConfig().cost_model raised
# AttributeError in production the moment any render path was actually
# exercised live. Wired here, after both classes exist, rather than at
# each of the 4 call sites that assumed it worked.
VendorConfig.cost_model = CostModel()


# ============================================================================
# FEATURE FLAGS
# ============================================================================

class FeatureFlags:
    """V2 feature toggles"""

    # Beta user whitelist (empty = all users if enabled)
    BETA_USERS: list = []  # Add user IDs to restrict access

    # Quality gate settings
    MIN_CONFIDENCE_AUTO_PUBLISH = 0.85
    REQUIRE_REVIEW_FIRST_N_POSTS = 3  # First 3 posts per new customer

    # Feature toggles
    CAROUSEL_ENABLED = True
    MULTI_FORMAT_ENABLED = True
    AI_RECOMPOSITE_ENABLED = False  # Premium, opt-in only

    # Cost caps
    MAX_COST_PER_RENDER_USD = 0.50
    MAX_COST_PER_CAROUSEL_USD = 1.50

    @classmethod
    def is_v2_enabled_for_user(cls, user_id: str) -> bool:
        """Check if V2 is enabled for this user"""
        if not VendorConfig.VISUAL_ENGINE_V2_ENABLED:
            return False

        # If beta user list is empty, allow all users
        if not cls.BETA_USERS:
            return True

        return user_id in cls.BETA_USERS

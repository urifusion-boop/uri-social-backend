# app/agents/social_media_manager/services/cta_recommender_service.py

from typing import List, Optional


class CTARecommenderService:
    """
    Recommend call-to-actions based on content type, business goal, and platform.

    PRD Requirement: Weekly Calendar Output - "Recommended CTA"
    "Each recommendation should include a recommended CTA that matches the
    content type and business objective."
    """

    # CTA templates by content type
    CTA_BY_CONTENT_TYPE = {
        "educational": [
            "Learn more in our guide",
            "Download our free resource",
            "Read the full article",
            "Save this for later",
            "Share with someone who needs this",
        ],
        "promotional": [
            "Shop now",
            "Get yours today",
            "Limited time offer - grab it",
            "Check out our collection",
            "Don't miss out",
        ],
        "engagement": [
            "Tell us in the comments",
            "Tag someone who relates",
            "What's your take on this?",
            "Drop your thoughts below",
            "Vote in our poll",
        ],
        "relatable": [
            "Double tap if you agree",
            "Tag a friend who needs to see this",
            "Share if you relate",
            "Comment your experience",
            "Who can relate?",
        ],
        "behind_the_scenes": [
            "Follow for more behind the scenes",
            "Want to see more? Let us know",
            "Stay tuned for updates",
            "Drop a ❤️ if you enjoyed this",
            "Save to follow our journey",
        ],
    }

    # CTA templates by business goal
    CTA_BY_GOAL = {
        "lead_generation": [
            "Download our free guide 📥",
            "Sign up for our newsletter",
            "Get your free consultation",
            "Register for our webinar",
            "Claim your free template",
        ],
        "sales": [
            "Shop now 🛍️",
            "Get 20% off today",
            "Add to cart",
            "Grab yours before it's gone",
            "Order now",
        ],
        "engagement": [
            "Comment below 👇",
            "Tell us your thoughts",
            "Tag a friend",
            "Share your story",
            "Join the conversation",
        ],
        "awareness": [
            "Follow for more",
            "Share this with your network",
            "Tag someone who needs this",
            "Spread the word",
            "Help us reach more people",
        ],
        "traffic": [
            "Read more on our blog",
            "Visit our website",
            "Link in bio 🔗",
            "Swipe up to learn more",
            "Check out the full article",
        ],
        "followers": [
            "Follow us for daily tips",
            "Hit that follow button",
            "Join our community",
            "Don't miss future posts",
            "Follow for more content like this",
        ],
    }

    # Platform-specific CTA nuances
    PLATFORM_CTA_STYLES = {
        "instagram": {
            "use_emojis": True,
            "mention_bio": True,  # "Link in bio"
            "mention_stories": True,  # "Swipe up" (for accounts with 10K+)
        },
        "facebook": {
            "use_emojis": True,
            "can_use_links": True,
            "mention_bio": False,
        },
        "linkedin": {
            "use_emojis": False,  # More professional
            "can_use_links": True,
            "professional_tone": True,
        },
        "twitter": {
            "use_emojis": True,
            "short_ctas": True,  # Keep brief
            "can_use_links": True,
        },
    }

    @staticmethod
    def recommend_cta(
        content_type: str,
        primary_goal: str,
        platform: str = "instagram",
        brand_cta_styles: Optional[List[str]] = None
    ) -> str:
        """
        Returns appropriate CTA for this post.

        Args:
            content_type: educational | promotional | engagement | relatable | behind_the_scenes
            primary_goal: lead_generation | sales | engagement | awareness | traffic | followers
            platform: instagram | facebook | linkedin | twitter
            brand_cta_styles: User's preferred CTA styles from brand playbook

        Returns:
            Recommended CTA string

        Example:
            >>> recommend_cta("educational", "lead_generation", "instagram")
            "Download our free guide 📥"
        """
        # Prioritize brand's custom CTAs if provided
        if brand_cta_styles and len(brand_cta_styles) > 0:
            # Pick first matching CTA from brand preferences
            return brand_cta_styles[0]

        # Normalize inputs
        content_type = content_type.lower().replace(" ", "_")
        goal_key = CTARecommenderService._normalize_goal(primary_goal)
        platform = platform.lower()

        # Get platform style preferences
        platform_style = CTARecommenderService.PLATFORM_CTA_STYLES.get(
            platform,
            {"use_emojis": True, "can_use_links": False, "mention_bio": False}
        )

        # Strategy: Match goal first, then content type
        cta = None

        # 1. Try goal-based CTA
        if goal_key in CTARecommenderService.CTA_BY_GOAL:
            goal_ctas = CTARecommenderService.CTA_BY_GOAL[goal_key]
            cta = goal_ctas[0]  # Pick first option

        # 2. Fallback to content-type CTA
        if not cta and content_type in CTARecommenderService.CTA_BY_CONTENT_TYPE:
            type_ctas = CTARecommenderService.CTA_BY_CONTENT_TYPE[content_type]
            cta = type_ctas[0]

        # 3. Ultimate fallback
        if not cta:
            cta = "Learn more"

        # Apply platform-specific adjustments
        cta = CTARecommenderService._apply_platform_style(cta, platform, platform_style)

        return cta

    @staticmethod
    def _normalize_goal(goal: str) -> str:
        """
        Normalize goal string to match CTA_BY_GOAL keys.
        """
        goal_lower = goal.lower()

        if "lead" in goal_lower or "generate" in goal_lower:
            return "lead_generation"
        if "sale" in goal_lower or "revenue" in goal_lower or "conversion" in goal_lower:
            return "sales"
        if "engagement" in goal_lower or "interact" in goal_lower:
            return "engagement"
        if "awareness" in goal_lower or "reach" in goal_lower or "visibility" in goal_lower:
            return "awareness"
        if "traffic" in goal_lower or "website" in goal_lower or "visit" in goal_lower:
            return "traffic"
        if "follower" in goal_lower or "audience" in goal_lower or "grow" in goal_lower:
            return "followers"

        return "engagement"  # Default

    @staticmethod
    def _apply_platform_style(cta: str, platform: str, style: dict) -> str:
        """
        Apply platform-specific formatting to CTA.
        """
        # LinkedIn: Remove emojis for professionalism
        if platform == "linkedin" and style.get("professional_tone"):
            # Remove common emojis
            emojis = ["📥", "🛍️", "👇", "🔗", "💼", "✨", "🎯", "💡", "🔥", "📊", "❤️"]
            for emoji in emojis:
                cta = cta.replace(emoji, "").strip()

        # Instagram: Add "Link in bio" for traffic-related CTAs
        if platform == "instagram" and style.get("mention_bio"):
            if "website" in cta.lower() or "learn more" in cta.lower() or "read more" in cta.lower():
                if "bio" not in cta.lower():
                    cta = cta + " (link in bio)"

        return cta

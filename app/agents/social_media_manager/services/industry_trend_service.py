# app/agents/social_media_manager/services/industry_trend_service.py

from typing import Any, Dict, List, Optional


class IndustryTrendService:
    """
    Provide industry-specific content trends and best practices.

    PRD Requirement: Section 6 - Industry Trends
    "For tech brands: 'AI agents' might be trending this month, so the calendar
    could suggest a post like 'How AI is transforming customer service.'"

    This service provides industry-specific trending topics, content formats,
    and best practices based on the brand's industry vertical.
    """

    # Industry-specific content best practices
    INDUSTRY_BEST_PRACTICES = {
        "technology": {
            "top_performing_types": ["educational", "thought_leadership", "behind_the_scenes"],
            "optimal_posting_days": ["Tuesday", "Wednesday", "Thursday"],
            "avoid_days": ["Saturday", "Sunday"],
            "content_formats": {
                "educational": 0.40,  # 40% of content
                "thought_leadership": 0.25,
                "promotional": 0.15,
                "engagement": 0.10,
                "behind_the_scenes": 0.10,
            },
            "insights": [
                "Educational content drives 2.3x more engagement in tech",
                "Thought leadership positions your brand as an industry expert",
                "Tuesday-Thursday sees peak professional audience engagement",
                "Data-driven content (charts, stats) performs exceptionally well",
            ],
            "trending_formats": ["Carousels with step-by-step tutorials", "Tech explainer videos", "Case studies"],
        },
        "fashion": {
            "top_performing_types": ["behind_the_scenes", "promotional", "relatable"],
            "optimal_posting_days": ["Wednesday", "Thursday", "Friday", "Sunday"],
            "avoid_days": [],
            "content_formats": {
                "promotional": 0.35,
                "behind_the_scenes": 0.25,
                "relatable": 0.20,
                "engagement": 0.15,
                "educational": 0.05,
            },
            "insights": [
                "Visual storytelling is critical — high-quality imagery is non-negotiable",
                "Behind-the-scenes content humanizes your brand and builds connection",
                "Friday-Sunday audiences are in shopping mode",
                "User-generated content drives 4x more engagement than brand photos",
            ],
            "trending_formats": ["Lookbook carousels", "Styling tips videos", "Try-on hauls"],
        },
        "food": {
            "top_performing_types": ["relatable", "behind_the_scenes", "educational"],
            "optimal_posting_days": ["Tuesday", "Wednesday", "Thursday", "Sunday"],
            "avoid_days": [],
            "content_formats": {
                "relatable": 0.30,
                "educational": 0.25,
                "behind_the_scenes": 0.20,
                "promotional": 0.15,
                "engagement": 0.10,
            },
            "insights": [
                "Recipe content and cooking tips drive massive saves and shares",
                "Behind-the-scenes kitchen content builds authenticity",
                "Lunch hours (11am-1pm) and dinner time (6-8pm) see peak engagement",
                "Food photography quality directly correlates with engagement",
            ],
            "trending_formats": ["Recipe carousels", "Cooking process videos", "Food styling tips"],
        },
        "health": {
            "top_performing_types": ["educational", "relatable", "engagement"],
            "optimal_posting_days": ["Monday", "Tuesday", "Wednesday", "Sunday"],
            "avoid_days": [],
            "content_formats": {
                "educational": 0.40,
                "relatable": 0.25,
                "engagement": 0.15,
                "promotional": 0.10,
                "behind_the_scenes": 0.10,
            },
            "insights": [
                "Educational wellness content positions you as a trusted authority",
                "Monday motivation content performs exceptionally well",
                "Mental health and mindfulness topics see high engagement",
                "Personal transformation stories drive emotional connection",
            ],
            "trending_formats": ["Wellness tip carousels", "Exercise demo videos", "Myth-busting posts"],
        },
        "finance": {
            "top_performing_types": ["educational", "thought_leadership", "relatable"],
            "optimal_posting_days": ["Monday", "Tuesday", "Wednesday"],
            "avoid_days": ["Saturday", "Sunday"],
            "content_formats": {
                "educational": 0.45,
                "thought_leadership": 0.25,
                "relatable": 0.15,
                "engagement": 0.10,
                "promotional": 0.05,
            },
            "insights": [
                "Simplifying complex financial topics builds trust and authority",
                "Data visualization and infographics drive high engagement",
                "Monday financial planning content aligns with audience mindset",
                "Relatable money struggles resonate strongly with audiences",
            ],
            "trending_formats": ["Financial tip carousels", "Budgeting guides", "Market analysis"],
        },
        "e-commerce": {
            "top_performing_types": ["promotional", "relatable", "engagement"],
            "optimal_posting_days": ["Thursday", "Friday", "Saturday", "Sunday"],
            "avoid_days": [],
            "content_formats": {
                "promotional": 0.40,
                "relatable": 0.25,
                "engagement": 0.15,
                "behind_the_scenes": 0.10,
                "educational": 0.10,
            },
            "insights": [
                "Thursday-Sunday is peak online shopping window",
                "User-generated content and reviews build trust and drive conversions",
                "Limited-time offers and urgency tactics significantly boost sales",
                "Product carousels with multiple angles outperform single images",
            ],
            "trending_formats": ["Product showcase carousels", "Unboxing videos", "Customer testimonials"],
        },
        "real estate": {
            "top_performing_types": ["educational", "behind_the_scenes", "promotional"],
            "optimal_posting_days": ["Tuesday", "Wednesday", "Thursday", "Saturday", "Sunday"],
            "avoid_days": [],
            "content_formats": {
                "educational": 0.35,
                "promotional": 0.30,
                "behind_the_scenes": 0.20,
                "relatable": 0.10,
                "engagement": 0.05,
            },
            "insights": [
                "Educational content (market insights, buying tips) builds authority",
                "Property showcase content performs best on weekends",
                "Behind-the-scenes of property tours humanizes your service",
                "Local market data and neighborhood guides drive high engagement",
            ],
            "trending_formats": ["Property tour videos", "Home buying tip carousels", "Market update posts"],
        },
    }

    # Industry-specific trending topics (would be dynamic in production)
    INDUSTRY_TRENDING_TOPICS = {
        "technology": {
            "2026": [
                "AI automation",
                "machine learning models",
                "cybersecurity best practices",
                "cloud migration strategies",
                "remote work productivity tools",
                "developer productivity",
                "API integrations",
                "no-code platforms",
            ]
        },
        "fashion": {
            "2026": [
                "sustainable fashion",
                "vintage style revival",
                "minimalist wardrobe",
                "gender-neutral fashion",
                "slow fashion movement",
                "capsule wardrobe",
                "thrift flips",
                "fashion rental services",
            ]
        },
        "food": {
            "2026": [
                "plant-based recipes",
                "meal prep strategies",
                "air fryer recipes",
                "gut health foods",
                "zero-waste cooking",
                "batch cooking",
                "protein-packed meals",
                "comfort food makeovers",
            ]
        },
        "health": {
            "2026": [
                "mental health awareness",
                "mindfulness practices",
                "strength training for women",
                "sleep optimization",
                "stress management",
                "holistic wellness",
                "fitness challenges",
                "nutrition science",
            ]
        },
        "finance": {
            "2026": [
                "passive income strategies",
                "cryptocurrency investing",
                "retirement planning",
                "debt payoff strategies",
                "financial independence",
                "investment diversification",
                "tax optimization",
                "emergency fund building",
            ]
        },
        "e-commerce": {
            "2026": [
                "personalized shopping experiences",
                "social commerce",
                "fast shipping expectations",
                "customer reviews impact",
                "influencer collaborations",
                "subscription box models",
                "live shopping events",
                "AI product recommendations",
            ]
        },
        "real estate": {
            "2026": [
                "first-time homebuyer tips",
                "real estate investment strategies",
                "mortgage rate trends",
                "remote work impact on housing",
                "smart home technology",
                "property staging tips",
                "neighborhood guides",
                "real estate market predictions",
            ]
        },
    }

    @staticmethod
    def get_industry_best_practices(industry: str) -> Dict[str, Any]:
        """
        Returns content best practices for the specified industry.

        Args:
            industry: User's industry category

        Returns:
            Dict with top_performing_types, optimal_posting_days, content_formats, insights

        Example:
            {
                "top_performing_types": ["educational", "thought_leadership"],
                "optimal_posting_days": ["Tuesday", "Wednesday", "Thursday"],
                "content_formats": {"educational": 0.40, "promotional": 0.15, ...},
                "insights": ["Educational content drives 2.3x more engagement..."],
                "trending_formats": ["Carousels with tutorials", "Case studies"]
            }
        """
        industry_lower = industry.lower().replace(" ", "_") if industry else ""

        practices = IndustryTrendService.INDUSTRY_BEST_PRACTICES.get(
            industry_lower,
            IndustryTrendService.INDUSTRY_BEST_PRACTICES["technology"]  # Default
        )

        return practices

    @staticmethod
    def get_trending_topics(industry: str, year: Optional[int] = None) -> List[str]:
        """
        Returns industry-specific trending topics.

        Args:
            industry: User's industry category
            year: Year for trends (defaults to current year)

        Returns:
            List of trending topic strings

        Example:
            ["AI automation", "machine learning models", "cybersecurity best practices"]
        """
        if not year:
            year = 2026  # Current year from context

        industry_lower = industry.lower().replace(" ", "_") if industry else ""

        # Get industry trends for the year
        industry_trends = IndustryTrendService.INDUSTRY_TRENDING_TOPICS.get(
            industry_lower,
            IndustryTrendService.INDUSTRY_TRENDING_TOPICS.get("technology", {})
        )

        year_str = str(year)
        trends = industry_trends.get(year_str, industry_trends.get("2026", []))

        return trends[:8]  # Return top 8

    @staticmethod
    def recommend_content_type(
        industry: str,
        current_distribution: Optional[Dict[str, int]] = None,
    ) -> str:
        """
        Recommend next content type based on industry best practices and current distribution.

        Args:
            industry: User's industry category
            current_distribution: Current content type counts {"educational": 2, "promotional": 1}

        Returns:
            Recommended content type string

        Example:
            "educational" (for tech brand needing more educational content)
        """
        practices = IndustryTrendService.get_industry_best_practices(industry)
        ideal_formats = practices["content_formats"]

        if not current_distribution:
            # No history - return most recommended type
            return max(ideal_formats, key=ideal_formats.get)

        # Calculate current percentages
        total_posts = sum(current_distribution.values())
        if total_posts == 0:
            return max(ideal_formats, key=ideal_formats.get)

        current_percentages = {
            ctype: (count / total_posts) for ctype, count in current_distribution.items()
        }

        # Find type that's most under-represented compared to ideal
        max_deficit = -1
        recommended_type = "educational"

        for ctype, ideal_pct in ideal_formats.items():
            current_pct = current_percentages.get(ctype, 0)
            deficit = ideal_pct - current_pct

            if deficit > max_deficit:
                max_deficit = deficit
                recommended_type = ctype

        return recommended_type

    @staticmethod
    def should_post_on_day(industry: str, day_of_week: str) -> Dict[str, Any]:
        """
        Check if this day is optimal for posting based on industry best practices.

        Args:
            industry: User's industry category
            day_of_week: Day name (Monday, Tuesday, etc.)

        Returns:
            Dict with is_optimal, reason

        Example:
            {
                "is_optimal": True,
                "reason": "Tuesday sees peak professional engagement in tech industry",
                "priority": "high"
            }
        """
        practices = IndustryTrendService.get_industry_best_practices(industry)
        optimal_days = practices["optimal_posting_days"]
        avoid_days = practices.get("avoid_days", [])

        if day_of_week in avoid_days:
            return {
                "is_optimal": False,
                "reason": f"{day_of_week} typically sees lower engagement in {industry}",
                "priority": "low",
            }

        if day_of_week in optimal_days:
            return {
                "is_optimal": True,
                "reason": f"{day_of_week} is optimal for {industry} audience engagement",
                "priority": "high",
            }

        return {
            "is_optimal": True,
            "reason": f"{day_of_week} provides consistent reach for your audience",
            "priority": "medium",
        }

    @staticmethod
    def get_content_format_insight(industry: str, content_type: str) -> Optional[str]:
        """
        Get specific insight about why this content type works for this industry.

        Args:
            industry: User's industry category
            content_type: Content type (educational, promotional, etc.)

        Returns:
            Insight string or None

        Example:
            "Educational content drives 2.3x more engagement in tech"
        """
        practices = IndustryTrendService.get_industry_best_practices(industry)
        insights = practices.get("insights", [])

        # Find insight mentioning this content type
        content_lower = content_type.lower()
        for insight in insights:
            if content_lower in insight.lower():
                return insight

        # Return general insight
        if insights:
            return insights[0]

        return None

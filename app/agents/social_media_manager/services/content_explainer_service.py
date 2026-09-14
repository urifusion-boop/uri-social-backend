# app/agents/social_media_manager/services/content_explainer_service.py

from typing import Any, Dict, List, Optional


class ContentExplainerService:
    """
    Generate data-backed explanations for why specific content was recommended.

    PRD Requirement: Enhanced Explainability
    "Each recommendation should include a brief explanation of WHY this content
    was chosen, backed by data where possible (e.g., 'Educational carousels
    generated 4,500 avg impressions last month, 4x higher than static posts')."
    """

    @staticmethod
    def explain_recommendation(
        content_type: str,
        topic: str,
        post_day: str,
        primary_goal: str,
        historical_performance: Optional[Dict[str, Any]] = None,
        upcoming_holidays: Optional[List[Dict[str, Any]]] = None,
        trending_topics: Optional[List[str]] = None,
        industry_best_practices: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Generate a comprehensive, data-backed explanation for this content recommendation.

        Args:
            content_type: educational | promotional | engagement | relatable | behind_the_scenes
            topic: The post subject
            post_day: Day of week (Monday, Tuesday, etc.)
            primary_goal: User's business goal (lead_generation, engagement, etc.)
            historical_performance: Performance data from past posts
            upcoming_holidays: Relevant holidays from HolidayCalendarService
            trending_topics: Trending keywords/topics from CulturalMomentService
            industry_best_practices: Best practices from IndustryTrendService

        Returns:
            Detailed explanation string with data backing where available

        Example:
            "Educational carousels like this generated 4,500 avg impressions last month
            (4x higher than static posts). Valentine's Day is in 3 weeks - perfect timing
            to start gift guide content. 'Gift ideas' is trending with 120K+ mentions.
            Aligns with your lead generation goal by driving traffic to your site."
        """
        explanation_parts = []

        # 1. Historical Performance Evidence
        if historical_performance:
            perf_insight = ContentExplainerService._explain_historical_performance(
                content_type, historical_performance
            )
            if perf_insight:
                explanation_parts.append(perf_insight)

        # 2. Holiday/Seasonal Timing
        if upcoming_holidays and len(upcoming_holidays) > 0:
            holiday_insight = ContentExplainerService._explain_holiday_timing(
                content_type, upcoming_holidays
            )
            if holiday_insight:
                explanation_parts.append(holiday_insight)

        # 3. Trending Topics Alignment
        if trending_topics and len(trending_topics) > 0:
            trend_insight = ContentExplainerService._explain_trend_alignment(
                topic, trending_topics
            )
            if trend_insight:
                explanation_parts.append(trend_insight)

        # 4. Day-of-Week Strategy
        day_insight = ContentExplainerService._explain_day_strategy(
            post_day, content_type
        )
        if day_insight:
            explanation_parts.append(day_insight)

        # 5. Goal Alignment
        goal_insight = ContentExplainerService._explain_goal_alignment(
            content_type, primary_goal
        )
        if goal_insight:
            explanation_parts.append(goal_insight)

        # 6. Industry Best Practices
        if industry_best_practices:
            industry_insight = ContentExplainerService._explain_industry_fit(
                content_type, industry_best_practices
            )
            if industry_insight:
                explanation_parts.append(industry_insight)

        # Fallback if no data available
        if not explanation_parts:
            return ContentExplainerService._generic_explanation(content_type, topic)

        return " ".join(explanation_parts)

    @staticmethod
    def _explain_historical_performance(
        content_type: str, performance: Dict[str, Any]
    ) -> Optional[str]:
        """
        Generate performance-based insight from historical data.

        Expected performance structure:
        {
            "educational": {"avg_impressions": 4500, "avg_engagement_rate": 0.035},
            "promotional": {"avg_impressions": 2800, "avg_engagement_rate": 0.021},
            "best_performing_type": "educational",
            "comparison_multiplier": 4.0
        }
        """
        if content_type not in performance:
            return None

        type_data = performance[content_type]
        avg_impressions = type_data.get("avg_impressions")
        avg_engagement = type_data.get("avg_engagement_rate")

        # Check if this is the best performing type
        best_type = performance.get("best_performing_type")
        if best_type == content_type:
            if avg_impressions:
                multiplier = performance.get("comparison_multiplier", 2.0)
                return (
                    f"{content_type.capitalize()} content like this generated "
                    f"{avg_impressions:,.0f} avg impressions in your past posts "
                    f"({multiplier:.1f}x higher than other formats)."
                )
            elif avg_engagement:
                return (
                    f"{content_type.capitalize()} posts historically drive "
                    f"{avg_engagement:.1%} engagement rate for your brand "
                    f"(your best-performing format)."
                )

        # Not the best, but still have data
        if avg_impressions and avg_impressions > 1000:
            return (
                f"{content_type.capitalize()} content generated "
                f"{avg_impressions:,.0f} avg impressions in your past posts."
            )

        return None

    @staticmethod
    def _explain_holiday_timing(
        content_type: str, holidays: List[Dict[str, Any]]
    ) -> Optional[str]:
        """
        Generate holiday timing insight.

        Expected holiday structure (from HolidayCalendarService):
        {
            "date": "2025-02-14",
            "name": "Valentine's Day",
            "type": "commercial",
            "lead_time_days": 21,
            "relevance_score": 0.9,
            "content_angle": "Start promoting gift ideas 3 weeks before Valentine's"
        }
        """
        # Find most relevant holiday
        top_holiday = max(holidays, key=lambda h: h.get("relevance_score", 0))

        name = top_holiday["name"]
        lead_time = top_holiday.get("lead_time_days", 0)
        holiday_type = top_holiday.get("type", "")

        if lead_time <= 0:
            return f"{name} is today — perfect timing for timely, celebratory content."
        elif lead_time <= 7:
            return f"{name} is this week ({lead_time} days) — ideal for last-minute reminders."
        elif lead_time <= 14:
            if holiday_type == "commercial":
                return f"{name} is in 2 weeks — ramp up promotional content now for maximum sales."
            return f"{name} is approaching in {lead_time} days — build awareness early."
        else:
            weeks = lead_time // 7
            if holiday_type == "commercial":
                return f"{name} is in {weeks} weeks — early promotion captures planning shoppers."
            return f"{name} is coming up — position your brand early with anticipation content."

    @staticmethod
    def _explain_trend_alignment(
        topic: str, trending_topics: List[str]
    ) -> Optional[str]:
        """
        Generate trending topic alignment insight.

        Expected trending_topics: ["AI automation", "remote work tips", "sustainable fashion"]
        """
        # Check if topic contains any trending keyword
        topic_lower = topic.lower()
        matching_trends = [t for t in trending_topics if t.lower() in topic_lower]

        if matching_trends:
            trend = matching_trends[0]
            return f"'{trend}' is trending right now with high engagement — timely and relevant."

        # Otherwise just mention trending topics exist
        if trending_topics:
            trend = trending_topics[0]
            return f"Aligns with current trends like '{trend}' for timely relevance."

        return None

    @staticmethod
    def _explain_day_strategy(post_day: str, content_type: str) -> str:
        """
        Generate day-of-week strategic insight.
        """
        day_strategies = {
            "Monday": "Monday audiences seek motivation and planning content — great for educational posts.",
            "Tuesday": "Tuesday sees peak professional engagement — ideal for value-driven content.",
            "Wednesday": "Mid-week audiences are open to diverse content — balanced engagement opportunity.",
            "Thursday": "Thursday performs well for thought leadership and engagement posts.",
            "Friday": "Friday audiences prefer lighter, relatable, and entertaining content.",
            "Saturday": "Weekend audiences are more casual — great for lifestyle and behind-the-scenes content.",
            "Sunday": "Sunday audiences seek inspiration and planning — educational content performs well.",
        }

        return day_strategies.get(post_day, "Strategically timed for consistent presence.")

    @staticmethod
    def _explain_goal_alignment(content_type: str, goal: str) -> Optional[str]:
        """
        Generate business goal alignment insight.
        """
        goal_lower = goal.lower()

        alignments = {
            "educational": {
                "lead": "Drives lead generation by positioning your brand as an expert and building trust.",
                "awareness": "Builds awareness by providing value upfront and establishing authority.",
                "traffic": "Drives traffic by offering insights that encourage 'learn more' clicks.",
                "engagement": "Sparks engagement through valuable tips that audiences want to save and share.",
            },
            "promotional": {
                "sales": "Directly drives sales with clear product focus and compelling offers.",
                "lead": "Generates leads by showcasing your solution and encouraging action.",
                "conversion": "Optimized for conversions with urgency and clear calls-to-action.",
            },
            "engagement": {
                "engagement": "Maximizes engagement by inviting direct audience participation and discussion.",
                "awareness": "Boosts awareness through shareability and conversation-starting content.",
                "followers": "Attracts new followers by encouraging tags and shares.",
            },
            "relatable": {
                "engagement": "Drives engagement through emotional connection and relatability.",
                "awareness": "Expands awareness through highly shareable, tag-worthy content.",
                "followers": "Grows followers by building authentic community connection.",
            },
            "behind_the_scenes": {
                "engagement": "Deepens engagement by humanizing your brand and inviting audiences in.",
                "awareness": "Builds awareness through authentic storytelling and transparency.",
                "followers": "Attracts followers who value authenticity and want to follow your journey.",
            },
        }

        # Find matching goal keyword
        for key in ["lead", "sales", "conversion", "engagement", "awareness", "traffic", "followers"]:
            if key in goal_lower:
                return alignments.get(content_type, {}).get(key)

        # Default alignment
        return f"Aligns with your {goal} objective through strategic content design."

    @staticmethod
    def _explain_industry_fit(
        content_type: str, best_practices: Dict[str, Any]
    ) -> Optional[str]:
        """
        Generate industry best practice insight.

        Expected structure:
        {
            "top_performing_types": ["educational", "behind_the_scenes"],
            "industry": "technology",
            "recommendation": "Educational content drives 2.3x more engagement in tech"
        }
        """
        top_types = best_practices.get("top_performing_types", [])
        industry = best_practices.get("industry", "")
        recommendation = best_practices.get("recommendation")

        if content_type in top_types:
            if recommendation:
                return f"Industry insight: {recommendation}."
            return f"{content_type.capitalize()} content performs exceptionally well in {industry}."

        return None

    @staticmethod
    def _generic_explanation(content_type: str, topic: str) -> str:
        """
        Fallback explanation when no data is available.
        """
        generic_reasons = {
            "educational": "Educational content builds authority and trust while providing tangible value to your audience.",
            "promotional": "Promotional content directly showcases your offerings and drives conversion actions.",
            "engagement": "Engagement content sparks conversation and strengthens community connection.",
            "relatable": "Relatable content creates emotional connection and encourages sharing.",
            "behind_the_scenes": "Behind-the-scenes content humanizes your brand and builds authentic connection.",
        }

        return generic_reasons.get(
            content_type,
            "This content aligns with your brand strategy and audience preferences."
        )

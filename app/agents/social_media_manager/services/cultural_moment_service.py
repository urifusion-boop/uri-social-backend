# app/agents/social_media_manager/services/cultural_moment_service.py

from datetime import datetime
from typing import Any, Dict, List, Optional


class CulturalMomentService:
    """
    Detect global trends, viral moments, and cultural events for timely content.

    PRD Requirement: Section 7 - Global Trends & Cultural Moments
    "The system should be aware of major trending topics, viral moments, and
    cultural events happening globally or regionally (e.g., World Cup, Oscars,
    viral memes, breaking news)."

    Note: This is a foundation service with placeholder logic. In production,
    this would integrate with:
    - Twitter/X Trending API
    - Google Trends API
    - News APIs (NewsAPI, Event Registry)
    - Reddit trending subreddits
    - TikTok trending sounds/hashtags
    """

    # Placeholder: Major recurring annual events
    ANNUAL_CULTURAL_MOMENTS = {
        "01-01": {"name": "New Year", "type": "cultural", "global": True},
        "02-14": {"name": "Valentine's Day", "type": "commercial_cultural", "global": True},
        "03-08": {"name": "International Women's Day", "type": "awareness", "global": True},
        "04-22": {"name": "Earth Day", "type": "awareness", "global": True},
        "06-21": {"name": "World Music Day", "type": "cultural", "global": True},
        "10-31": {"name": "Halloween", "type": "cultural", "global": False, "regions": ["US", "UK"]},
        "11-25": {"name": "Black Friday", "type": "commercial", "global": True},
        "12-25": {"name": "Christmas", "type": "cultural", "global": True},
    }

    # Placeholder: Simulated trending topics by industry
    # In production, this would be fetched from live APIs
    SIMULATED_TRENDS = {
        "technology": [
            "AI automation",
            "remote work tools",
            "cybersecurity threats",
            "cloud migration",
            "machine learning",
        ],
        "fashion": [
            "sustainable fashion",
            "vintage style",
            "minimalist wardrobe",
            "color of the year",
            "fashion week",
        ],
        "food": [
            "meal prep ideas",
            "healthy recipes",
            "food delivery",
            "plant-based diet",
            "comfort food",
        ],
        "health": [
            "mental health awareness",
            "fitness challenges",
            "mindfulness",
            "nutrition tips",
            "wellness routines",
        ],
        "finance": [
            "investment strategies",
            "saving tips",
            "cryptocurrency",
            "financial planning",
            "passive income",
        ],
        "e-commerce": [
            "online shopping trends",
            "customer experience",
            "fast shipping",
            "product reviews",
            "sales events",
        ],
        "real estate": [
            "home buying tips",
            "real estate market",
            "property investment",
            "interior design",
            "mortgage rates",
        ],
    }

    @staticmethod
    def get_trending_topics(
        industry: str = "",
        region: str = "",
        week_start: Optional[str] = None,
    ) -> List[str]:
        """
        Returns currently trending topics relevant to the brand's industry and region.

        Args:
            industry: User's industry category
            region: User's region (e.g., "Nigeria", "West Africa")
            week_start: ISO date string "YYYY-MM-DD" (optional)

        Returns:
            List of trending topic strings

        Example:
            ["AI automation", "remote work tools", "cybersecurity threats"]

        Note: This is a placeholder implementation. In production, integrate:
        - Google Trends API for real-time trending searches
        - Twitter/X API for trending hashtags
        - News APIs for breaking topics
        """
        industry_lower = industry.lower().replace(" ", "_") if industry else ""

        # Get industry-specific trends
        trending = CulturalMomentService.SIMULATED_TRENDS.get(
            industry_lower,
            CulturalMomentService.SIMULATED_TRENDS.get("technology", [])
        )

        # TODO: In production, fetch live trends from APIs
        # Example:
        # from pytrends.request import TrendReq
        # pytrends = TrendReq()
        # trending = pytrends.trending_searches(pn='nigeria')

        return trending[:5]  # Return top 5

    @staticmethod
    def get_cultural_moments(
        week_start: str,
        region: str = "",
    ) -> List[Dict[str, Any]]:
        """
        Returns major cultural moments happening during or near this week.

        Args:
            week_start: ISO date string "YYYY-MM-DD"
            region: User's region

        Returns:
            List of cultural moment dicts with name, date, type, description

        Example:
            [
                {
                    "name": "World Cup Final",
                    "date": "2026-07-19",
                    "type": "sports",
                    "description": "Global sporting event with massive social media engagement",
                    "content_angle": "Tie your brand message to the excitement and celebration"
                }
            ]

        Note: This is a placeholder. In production, integrate:
        - Sports event APIs (Olympics, World Cup, Super Bowl)
        - Awards show schedules (Oscars, Grammys, Emmys)
        - News APIs for breaking viral moments
        """
        try:
            week_start_dt = datetime.strptime(week_start, "%Y-%m-%d")
        except ValueError:
            return []

        year = week_start_dt.year
        moments = []

        # Check annual cultural moments
        for date_str, moment_data in CulturalMomentService.ANNUAL_CULTURAL_MOMENTS.items():
            try:
                month, day = map(int, date_str.split("-"))
                moment_dt = datetime(year, month, day)
            except ValueError:
                continue

            # Include moments within 2 weeks
            delta = (moment_dt - week_start_dt).days
            if -7 <= delta <= 14:
                # Check region relevance
                if not moment_data.get("global", False):
                    allowed_regions = moment_data.get("regions", [])
                    if region and not any(r.lower() in region.lower() for r in allowed_regions):
                        continue

                content_angle = CulturalMomentService._generate_moment_angle(
                    moment_data["name"],
                    moment_data["type"],
                    delta
                )

                moments.append({
                    "name": moment_data["name"],
                    "date": moment_dt.strftime("%Y-%m-%d"),
                    "type": moment_data["type"],
                    "description": f"Global cultural moment: {moment_data['name']}",
                    "content_angle": content_angle,
                })

        # TODO: In production, add live events from APIs
        # Example major events to integrate:
        # - Sporting events (World Cup, Olympics, Super Bowl)
        # - Awards shows (Oscars, Grammys, Emmys)
        # - Breaking viral moments (memes, challenges, news)

        return moments

    @staticmethod
    def _generate_moment_angle(name: str, moment_type: str, days_until: int) -> str:
        """
        Generate content angle for cultural moment based on timing.
        """
        if days_until <= 0:
            return f"{name} is happening now — create timely, celebratory content to ride the wave."

        if days_until <= 7:
            return f"{name} is this week — build anticipation or last-minute reminders."

        # More than 1 week out
        if moment_type == "commercial":
            return f"{name} is coming up — start promotional content early to maximize reach."
        elif moment_type == "awareness":
            return f"{name} is approaching — create educational or advocacy content to participate."
        else:
            return f"{name} is soon — build anticipation with related content."

    @staticmethod
    def detect_trending_keywords(
        topic: str,
        trending_topics: List[str],
    ) -> List[str]:
        """
        Check if a topic contains trending keywords.

        Args:
            topic: Post topic/title
            trending_topics: List of trending keywords

        Returns:
            List of matching trending keywords found in topic

        Example:
            >>> detect_trending_keywords(
                "How AI automation is changing remote work",
                ["AI automation", "remote work tools", "cybersecurity"]
            )
            ["AI automation", "remote work tools"]
        """
        topic_lower = topic.lower()
        matches = []

        for trend in trending_topics:
            # Check for full phrase match or partial keyword match
            if trend.lower() in topic_lower:
                matches.append(trend)
            else:
                # Check individual words
                trend_words = trend.lower().split()
                if any(word in topic_lower for word in trend_words if len(word) > 3):
                    matches.append(trend)

        return matches

    @staticmethod
    def get_content_recommendation(
        trending_topics: List[str],
        cultural_moments: List[Dict[str, Any]],
        industry: str,
    ) -> Optional[Dict[str, Any]]:
        """
        Recommend a timely content opportunity based on trends and moments.

        Returns:
            Dict with recommended topic, angle, and reason, or None

        Example:
            {
                "recommended_topic": "AI automation in customer service",
                "angle": "Educational - How AI chatbots are transforming support",
                "reason": "'AI automation' is trending with high engagement + aligns with tech industry"
            }
        """
        if not trending_topics and not cultural_moments:
            return None

        # Prioritize cultural moments if very close
        if cultural_moments:
            closest = min(cultural_moments, key=lambda m: abs(
                (datetime.strptime(m["date"], "%Y-%m-%d") - datetime.now()).days
            ))
            days_until = (datetime.strptime(closest["date"], "%Y-%m-%d") - datetime.now()).days

            if days_until <= 7:
                return {
                    "recommended_topic": f"Content for {closest['name']}",
                    "angle": closest.get("content_angle", ""),
                    "reason": f"{closest['name']} is {days_until} days away — timely content opportunity",
                }

        # Otherwise recommend based on trending topic
        if trending_topics:
            top_trend = trending_topics[0]
            return {
                "recommended_topic": f"Content about {top_trend}",
                "angle": f"Educational or thought leadership on {top_trend}",
                "reason": f"'{top_trend}' is trending in {industry} — timely and relevant",
            }

        return None

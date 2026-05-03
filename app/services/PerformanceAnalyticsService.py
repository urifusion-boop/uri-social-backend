"""
Performance Analytics Service — Data-Driven Content Calendar (Phase 1)
Aggregates a user's historical post engagement to inform content scoring.
"""
from collections import defaultdict
from typing import Any, Dict, List

from motor.motor_asyncio import AsyncIOMotorDatabase


# Topic keyword → canonical topic label
_TOPIC_KEYWORDS: Dict[str, List[str]] = {
    "finance":    ["money", "finance", "investment", "savings", "profit", "revenue", "cost", "budget"],
    "education":  ["learn", "guide", "tips", "how to", "beginner", "mistakes", "advice", "tutorial"],
    "motivation": ["success", "achieve", "goals", "mindset", "growth", "challenge", "inspire"],
    "marketing":  ["marketing", "brand", "audience", "content", "social media", "engagement", "campaign"],
    "business":   ["business", "startup", "entrepreneur", "client", "customer", "sales", "revenue"],
    "technology": ["tech", "software", "app", "digital", "automation", "AI", "platform", "tool"],
    "health":     ["health", "wellness", "fitness", "mental", "wellbeing", "nutrition"],
    "real estate":["property", "real estate", "house", "land", "rent", "buy", "mortgage"],
    "fashion":    ["fashion", "style", "outfit", "clothing", "wear", "look", "trend"],
    "food":       ["food", "meal", "recipe", "cook", "eat", "restaurant", "catering"],
    "offer":      ["discount", "promo", "offer", "sale", "deal", "combo", "price", "off"],
    "story":      ["behind", "story", "journey", "our team", "process", "how we"],
}


class PerformanceAnalyticsService:

    @staticmethod
    async def get_user_performance(user_id: str, db: AsyncIOMotorDatabase) -> Dict[str, Any]:
        """
        Return aggregated performance metrics:
        {
            avg_engagement_by_format: {image: 3.2, text: 1.8},
            avg_engagement_by_topic:  {finance: 4.5, education: 3.1},
            best_posting_hour: 18,
            top_formats: [image, text],
            top_topics:  [finance, education],
            post_count: 12,
            has_data: bool,
        }
        """
        try:
            # Fetch published drafts (last 90 days worth, capped at 200)
            drafts_cursor = db["content_drafts"].find(
                {"user_id": user_id, "status": "published"},
                {"id": 1, "platform": 1, "content": 1, "has_image": 1, "published_date": 1},
            )
            drafts = await drafts_cursor.to_list(length=200)

            if not drafts:
                return PerformanceAnalyticsService._empty()

            draft_ids = [d["id"] for d in drafts if d.get("id")]

            # Fetch analytics
            analytics_cursor = db["content_analytics"].find(
                {"draft_id": {"$in": draft_ids}},
                {"draft_id": 1, "likes": 1, "comments": 1, "shares": 1, "impressions": 1},
            )
            analytics_list = await analytics_cursor.to_list(length=200)
            analytics_map = {a["draft_id"]: a for a in analytics_list}

            format_eng: Dict[str, List[float]] = defaultdict(list)
            topic_eng: Dict[str, List[float]] = defaultdict(list)
            hour_eng: Dict[int, List[float]] = defaultdict(list)

            for draft in drafts:
                draft_id = draft.get("id", "")
                ana = analytics_map.get(draft_id, {})

                likes      = float(ana.get("likes", 0) or 0)
                comments   = float(ana.get("comments", 0) or 0)
                shares     = float(ana.get("shares", 0) or 0)
                impressions = float(ana.get("impressions", 0) or 0)

                eng_rate = ((likes + comments + shares) / impressions * 100) if impressions > 0 else 0.0

                fmt = PerformanceAnalyticsService._classify_format(draft)
                format_eng[fmt].append(eng_rate)

                for topic in PerformanceAnalyticsService._extract_topics(draft.get("content", "")):
                    topic_eng[topic].append(eng_rate)

                pub_date = draft.get("published_date")
                if pub_date:
                    try:
                        from datetime import datetime
                        dt = (
                            datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
                            if isinstance(pub_date, str)
                            else pub_date
                        )
                        hour_eng[dt.hour].append(eng_rate)
                    except Exception:
                        pass

            avg_by_format = {
                fmt: round(sum(v) / len(v), 2)
                for fmt, v in format_eng.items() if v
            }
            avg_by_topic = {
                topic: round(sum(v) / len(v), 2)
                for topic, v in topic_eng.items() if v
            }
            best_hour = (
                max(hour_eng.items(), key=lambda x: sum(x[1]) / len(x[1]))[0]
                if hour_eng else 18
            )

            top_formats = [f for f, _ in sorted(avg_by_format.items(), key=lambda x: x[1], reverse=True)][:3]
            top_topics  = [t for t, _ in sorted(avg_by_topic.items(),  key=lambda x: x[1], reverse=True)][:5]

            return {
                "avg_engagement_by_format": avg_by_format,
                "avg_engagement_by_topic":  avg_by_topic,
                "best_posting_hour": best_hour,
                "top_formats": top_formats,
                "top_topics":  top_topics,
                "post_count": len(drafts),
                "analytics_count": len(analytics_list),
                "has_data": bool(avg_by_format or avg_by_topic),
            }

        except Exception as exc:
            print(f"[PerformanceAnalytics] error: {exc}")
            return PerformanceAnalyticsService._empty()

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _classify_format(draft: Dict[str, Any]) -> str:
        if draft.get("has_image"):
            return "image"
        content_len = len(draft.get("content", ""))
        return "long_form" if content_len > 500 else "text"

    @staticmethod
    def _extract_topics(content: str) -> List[str]:
        text = content.lower()
        return [
            topic for topic, keywords in _TOPIC_KEYWORDS.items()
            if any(k in text for k in keywords)
        ][:3]

    @staticmethod
    def _empty() -> Dict[str, Any]:
        return {
            "avg_engagement_by_format": {},
            "avg_engagement_by_topic":  {},
            "best_posting_hour": 18,
            "top_formats": [],
            "top_topics":  [],
            "post_count": 0,
            "analytics_count": 0,
            "has_data": False,
        }

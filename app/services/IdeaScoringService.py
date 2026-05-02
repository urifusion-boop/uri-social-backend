"""
Idea Scoring Service — Data-Driven Content Calendar (Phase 1)

Pipeline:
  1. generate_ideas()  — combine trend keywords × templates → raw ideas
  2. score_ideas()     — PRD formula: 0.4×trend + 0.4×performance + 0.2×format
  3. select_for_calendar() — pick top 7 with educational/trend/promotional mix
"""
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional


# Content idea templates — {topic} is replaced by the trend keyword
_TEMPLATES = [
    "5 mistakes {audience} make with {topic}",
    "How to {action} using {topic}",
    "Beginner's guide to {topic}",
    "What nobody tells you about {topic}",
    "The truth about {topic} in {year}",
    "5 things every {audience} should know about {topic}",
    "Why {topic} matters for your business",
    "5 ways to improve your {topic} strategy",
    "The biggest {topic} trends this year",
    "Is {topic} worth the investment? Here's what we found",
    "{topic}: common myths debunked",
    "How {topic} is changing the {industry} industry",
]

_AUDIENCE_BY_INDUSTRY: Dict[str, str] = {
    "real estate": "first-time homebuyer",
    "fashion":     "fashion entrepreneur",
    "food":        "food business owner",
    "finance":     "entrepreneur",
    "technology":  "startup founder",
    "health":      "health-conscious professional",
    "marketing":   "brand owner",
    "ecommerce":   "online store owner",
    "law":         "business owner",
    "education":   "student or professional",
    "beauty":      "beauty entrepreneur",
    "logistics":   "logistics manager",
}

# Maps idea title patterns → content_type label
_EDUCATIONAL_MARKERS = ["guide", "how to", "mistakes", "tips", "things", "truth", "myths", "know", "what nobody"]
_PROMOTIONAL_MARKERS = ["worth", "investment", "improve", "strategy", "changing"]


class IdeaScoringService:

    @staticmethod
    def generate_ideas(
        trend_keywords: List[Dict[str, Any]],
        industry: str,
        performance_data: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Generate raw ideas by combining trend keywords with templates."""
        audience  = _AUDIENCE_BY_INDUSTRY.get(industry.lower(), "business owner")
        top_fmts  = performance_data.get("top_formats", ["image"])
        best_fmt  = top_fmts[0] if top_fmts else "image"
        year      = str(datetime.utcnow().year)

        ideas = []
        for kw_data in trend_keywords[:8]:
            keyword = kw_data["keyword"]
            for template in _TEMPLATES:
                title = (
                    template
                    .replace("{topic}",    keyword)
                    .replace("{audience}", audience)
                    .replace("{action}",   f"grow your {industry} business")
                    .replace("{industry}", industry)
                    .replace("{year}",     year)
                )
                ideas.append({
                    "idea_id":          str(uuid.uuid4())[:8],
                    "title":            title,
                    "keyword":          keyword,
                    "format":           best_fmt,
                    "trend_data":       kw_data,
                    "trend_score":      0.0,
                    "performance_score":0.0,
                    "format_score":     0.0,
                    "final_score":      0.0,
                    "reason":           "",
                })

        return ideas

    @staticmethod
    def score_ideas(
        ideas: List[Dict[str, Any]],
        performance_data: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """
        Score each idea using PRD formula:
        final_score = (0.4 × trend_score) + (0.4 × performance_score) + (0.2 × format_score)
        """
        avg_by_format = performance_data.get("avg_engagement_by_format", {})
        avg_by_topic  = performance_data.get("avg_engagement_by_topic",  {})
        top_topics    = performance_data.get("top_topics", [])

        max_fmt_eng   = max(avg_by_format.values(), default=1.0) or 1.0
        max_topic_eng = max(avg_by_topic.values(),  default=1.0) or 1.0

        scored = []
        for idea in ideas:
            kw_data = idea.get("trend_data", {})
            keyword = idea["keyword"].lower()

            # ── Trend score (40%) ─────────────────────────────────────────────
            trend_score = min(100.0, float(kw_data.get("trend_score", 30)))
            if kw_data.get("type") == "rising":
                trend_score = min(100.0, trend_score * 1.35)  # rising boost

            # ── Performance score (40%) ──────────────────────────────────────
            perf_score = 20.0  # base when no data
            for topic, avg_eng in avg_by_topic.items():
                if topic.lower() in keyword or keyword in topic.lower():
                    perf_score = (avg_eng / max_topic_eng) * 100
                    break
            if any(t.lower() in keyword for t in top_topics):
                perf_score = min(100.0, perf_score * 1.25)  # top-topic boost

            # ── Format score (20%) ───────────────────────────────────────────
            fmt = idea.get("format", "image")
            if avg_by_format:
                fmt_eng = avg_by_format.get(fmt, 0.0)
                format_score = (fmt_eng / max_fmt_eng) * 100
            else:
                format_score = 50.0  # neutral when no format data

            # ── Final score ───────────────────────────────────────────────────
            final_score = (0.4 * trend_score) + (0.4 * perf_score) + (0.2 * format_score)

            # ── Build reason string (PRD Section 7) ───────────────────────────
            parts = []
            if kw_data.get("type") == "rising":
                growth = kw_data.get("growth_rate", 0)
                parts.append(f'"{idea["keyword"]}" is trending (+{growth:.0f}% growth on Google)')
            elif trend_score >= 50:
                parts.append(f'"{idea["keyword"]}" has strong current search interest')

            topic_match = next((t for t in top_topics if t.lower() in keyword), None)
            if topic_match and avg_by_topic.get(topic_match, 0) > 0:
                boost_pct = round(avg_by_topic[topic_match], 1)
                parts.append(f"Your {topic_match} posts perform {boost_pct}% above average")

            fmt_pct = avg_by_format.get(fmt, 0)
            if fmt_pct > 0:
                parts.append(f"{fmt.replace('_', ' ').title()} posts have your best engagement ({fmt_pct:.1f}%)")

            reason = " · ".join(parts) if parts else "Aligned with current industry trends"

            scored.append({
                **idea,
                "trend_score":      round(trend_score,  1),
                "performance_score":round(perf_score,   1),
                "format_score":     round(format_score, 1),
                "final_score":      round(final_score,  1),
                "reason":           reason,
            })

        # Sort descending, deduplicate by exact title only (allow multiple templates per keyword)
        scored.sort(key=lambda x: x["final_score"], reverse=True)
        seen_titles: set = set()
        unique: List[Dict] = []
        for idea in scored:
            title_key = idea["title"].lower().strip()
            if title_key not in seen_titles:
                seen_titles.add(title_key)
                unique.append(idea)

        return unique

    @staticmethod
    def select_for_calendar(
        scored_ideas: List[Dict[str, Any]],
        n: int = 7,
    ) -> List[Dict[str, Any]]:
        """
        Pick top n ideas with a content-type mix:
        Mon educational, Tue trend_based, Wed educational, Thu promotional,
        Fri educational, Sat trend_based, Sun promotional
        """
        day_types = [
            "educational", "trend_based", "educational", "promotional",
            "educational", "trend_based", "promotional",
        ]

        def _is_educational(idea: Dict) -> bool:
            t = idea["title"].lower()
            return any(m in t for m in _EDUCATIONAL_MARKERS)

        def _is_promotional(idea: Dict) -> bool:
            t = idea["title"].lower()
            return any(m in t for m in _PROMOTIONAL_MARKERS)

        educational = [i for i in scored_ideas if _is_educational(i)]
        promotional = [i for i in scored_ideas if _is_promotional(i) and not _is_educational(i)]
        trend_based = [i for i in scored_ideas if not _is_educational(i) and not _is_promotional(i)]

        pools = {
            "educational": educational or scored_ideas,
            "promotional": promotional or scored_ideas,
            "trend_based": trend_based  or scored_ideas,
        }

        selected: List[Dict] = []
        used_ids: set = set()
        keyword_count: Dict[str, int] = {}
        max_per_keyword = 2  # enforce diversity across 7 days

        def _pick(pool: List[Dict]) -> Optional[Dict]:
            for idea in pool:
                if idea["idea_id"] in used_ids:
                    continue
                kw = idea["keyword"].lower()
                if keyword_count.get(kw, 0) < max_per_keyword:
                    return idea
            # All keywords saturated — relax limit and take any unused
            for idea in pool:
                if idea["idea_id"] not in used_ids:
                    return idea
            return None

        for ct in day_types[:n]:
            pick = _pick(pools[ct]) or _pick(scored_ideas)
            if pick:
                kw = pick["keyword"].lower()
                selected.append({**pick, "content_type": ct})
                used_ids.add(pick["idea_id"])
                keyword_count[kw] = keyword_count.get(kw, 0) + 1

        return selected[:n]

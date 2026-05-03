"""
Trend Data Service — Data-Driven Content Calendar (Phase 1)
Fetches Google Trends rising/top keywords for an industry using pytrends.
Falls back to curated seed keywords when the API is unavailable.
"""
import asyncio
from typing import Any, Dict, List


# Industry → seed terms to query Google Trends
_INDUSTRY_SEEDS: Dict[str, List[str]] = {
    "real estate":  ["real estate", "buy property", "first time home buyer", "land investment"],
    "fashion":      ["fashion trends", "clothing brand", "style tips", "outfit ideas"],
    "food":         ["food business", "restaurant", "catering", "food delivery", "meal prep"],
    "finance":      ["personal finance", "investment tips", "savings", "fintech", "money management"],
    "technology":   ["tech startup", "SaaS", "software", "digital transformation", "AI tools"],
    "health":       ["wellness", "fitness tips", "mental health", "healthcare", "nutrition"],
    "law":          ["legal advice", "law firm", "contracts", "legal services", "lawyer"],
    "education":    ["online learning", "skill development", "tutoring", "professional development"],
    "marketing":    ["digital marketing", "social media strategy", "content marketing", "SEO", "branding"],
    "ecommerce":    ["online store", "dropshipping", "ecommerce", "product sourcing", "online business"],
    "beauty":       ["skincare", "beauty tips", "makeup", "hair care", "cosmetics"],
    "logistics":    ["logistics", "supply chain", "delivery", "shipping", "freight"],
}


class TrendDataService:

    @staticmethod
    async def get_trending_keywords(
        industry: str,
        geo: str = "NG",
        timeframe: str = "today 1-m",
    ) -> List[Dict[str, Any]]:
        """
        Return up to 10 trending keyword dicts for the industry:
        {keyword, trend_score (0-100), growth_rate, source, type}
        """
        try:
            loop = asyncio.get_running_loop()
            results = await loop.run_in_executor(
                None,
                lambda: TrendDataService._fetch_pytrends(industry, geo, timeframe),
            )
            return results
        except Exception as exc:
            print(f"[TrendData] pytrends fetch failed: {exc} — using fallback keywords")
            return TrendDataService._fallback_keywords(industry)

    # ── Internal sync method (runs in thread executor) ────────────────────────

    @staticmethod
    def _fetch_pytrends(industry: str, geo: str, timeframe: str) -> List[Dict[str, Any]]:
        from pytrends.request import TrendReq

        seeds = _INDUSTRY_SEEDS.get(
            industry.lower(),
            [industry, f"{industry} tips", f"{industry} mistakes", f"{industry} guide"],
        )

        pytrend = TrendReq(hl="en-NG", tz=60, timeout=(10, 25), retries=1, backoff_factor=0.5)
        found: List[Dict[str, Any]] = []

        for seed in seeds[:2]:  # max 2 seeds to avoid rate-limit
            try:
                pytrend.build_payload([seed], cat=0, timeframe=timeframe, geo=geo)
                related = pytrend.related_queries()
                seed_data = related.get(seed, {})

                rising_df = seed_data.get("rising")
                top_df = seed_data.get("top")

                if rising_df is not None and not rising_df.empty:
                    for _, row in rising_df.head(5).iterrows():
                        found.append({
                            "keyword": str(row["query"]).strip(),
                            "trend_score": min(100.0, float(row.get("value", 50))),
                            "growth_rate": float(row.get("value", 50)),
                            "source": "google_trends",
                            "type": "rising",
                        })

                if top_df is not None and not top_df.empty:
                    for _, row in top_df.head(3).iterrows():
                        found.append({
                            "keyword": str(row["query"]).strip(),
                            "trend_score": min(80.0, float(row.get("value", 40))),
                            "growth_rate": float(row.get("value", 40)) * 0.5,
                            "source": "google_trends",
                            "type": "top",
                        })
            except Exception as exc:
                print(f"[TrendData] seed '{seed}' failed: {exc}")
                continue

        # Deduplicate by keyword text, keep highest score per keyword
        deduped: Dict[str, Dict] = {}
        for kw in found:
            key = kw["keyword"].lower()
            if key not in deduped or kw["trend_score"] > deduped[key]["trend_score"]:
                deduped[key] = kw

        ranked = sorted(deduped.values(), key=lambda x: x["trend_score"], reverse=True)
        return ranked[:10] if ranked else TrendDataService._fallback_keywords(industry)

    @staticmethod
    def _fallback_keywords(industry: str) -> List[Dict[str, Any]]:
        seeds = _INDUSTRY_SEEDS.get(
            industry.lower(),
            [f"{industry} tips", f"{industry} mistakes", f"how to {industry}", f"{industry} guide"],
        )
        return [
            {
                "keyword": kw,
                "trend_score": 35.0,
                "growth_rate": 10.0,
                "source": "fallback",
                "type": "seed",
            }
            for kw in seeds[:6]
        ]

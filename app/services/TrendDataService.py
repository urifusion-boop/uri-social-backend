"""
Trend Data Service — Data-Driven Content Calendar (Phase 1)
Fetches Google Trends rising/top keywords for an industry using pytrends.
Falls back to curated seed keywords when the API is unavailable.
Results are cached in MongoDB for 6 hours to avoid Google rate-limits.
"""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional


# Region name → Google Trends geo code
_REGION_GEO: Dict[str, str] = {
    "nigeria": "NG", "lagos": "NG", "abuja": "NG", "port harcourt": "NG",
    "ghana": "GH", "accra": "GH", "kumasi": "GH",
    "kenya": "KE", "nairobi": "KE", "mombasa": "KE",
    "south africa": "ZA", "johannesburg": "ZA", "cape town": "ZA", "durban": "ZA",
    "united kingdom": "GB", "uk": "GB", "england": "GB", "london": "GB",
    "united states": "US", "usa": "US", "america": "US",
    "canada": "CA", "australia": "AU",
    "egypt": "EG", "cairo": "EG",
    "ethiopia": "ET", "senegal": "SN", "tanzania": "TZ", "uganda": "UG",
    "rwanda": "RW", "cameroon": "CM", "ivory coast": "CI", "cote d'ivoire": "CI",
}


def _region_to_geo(region: str) -> str:
    """Map a free-text region/country name to a Google Trends geo code."""
    if not region:
        return "NG"
    return _REGION_GEO.get(region.lower().strip(), "NG")


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
    "social media & marketing technology": ["social media marketing", "content creation", "AI content tools", "social media automation", "digital branding"],
    "social media":  ["social media marketing", "content creation", "AI tools", "social media automation", "digital branding"],
    "ecommerce":    ["online store", "dropshipping", "ecommerce", "product sourcing", "online business"],
    "beauty":       ["skincare", "beauty tips", "makeup", "hair care", "cosmetics"],
    "logistics":    ["logistics", "supply chain", "delivery", "shipping", "freight"],
}


class TrendDataService:

    CACHE_TTL_HOURS_REAL     = 24  # Cache real Google Trends data for 24h
    CACHE_TTL_HOURS_FALLBACK = 1   # Cache fallback data for 1h, then retry Google

    @staticmethod
    async def get_trending_keywords(
        industry: str,
        region: str = "",
        geo: str = "",
        timeframe: str = "today 1-m",
        brand_seeds: List[str] = None,
        db=None,  # Optional AsyncIOMotorDatabase for caching
    ) -> List[Dict[str, Any]]:
        """
        Return up to 10 trending keyword dicts for the industry:
        {keyword, trend_score (0-100), growth_rate, source, type}
        Caches real Trends data for 24h and fallback data for 1h to avoid
        hammering Google's rate limits on every page load.
        brand_seeds: additional topic seeds from the brand's content pillars
        and key products, used alongside industry seeds.
        """
        resolved_geo = geo or _region_to_geo(region)
        brand_seeds = [s for s in (brand_seeds or []) if s and len(s.strip()) > 2]
        # v2 prefix in cache key forces a miss when upgrading from pre-brand-seed code
        cache_key = f"v2:{industry.lower()}:{resolved_geo}:{timeframe}:{','.join(sorted(brand_seeds))}"

        # Try to read from cache first
        if db is not None:
            try:
                cached = await db["trends_cache"].find_one({"_id": cache_key})
                if cached:
                    is_fallback = cached.get("is_fallback", False)
                    ttl = TrendDataService.CACHE_TTL_HOURS_FALLBACK if is_fallback else TrendDataService.CACHE_TTL_HOURS_REAL
                    age = datetime.now(timezone.utc) - cached["cached_at"].replace(tzinfo=timezone.utc)
                    if age < timedelta(hours=ttl):
                        print(f"[TrendData] cache hit for '{industry}' (age: {int(age.total_seconds()/60)}m, fallback={is_fallback})")
                        return cached["keywords"]
            except Exception as e:
                print(f"[TrendData] cache read error: {e}")

        # Fetch from Google Trends
        try:
            loop = asyncio.get_running_loop()
            results = await loop.run_in_executor(
                None,
                lambda: TrendDataService._fetch_pytrends(industry, resolved_geo, timeframe, brand_seeds),
            )
        except Exception as exc:
            print(f"[TrendData] pytrends fetch failed: {exc} — using fallback keywords")
            results = TrendDataService._fallback_keywords(industry, brand_seeds)

        # Cache all results — real data for 24h, fallback for 1h
        if db is not None:
            is_fallback = not any(r.get("source") == "google_trends" for r in results)
            try:
                await db["trends_cache"].replace_one(
                    {"_id": cache_key},
                    {
                        "_id": cache_key,
                        "keywords": results,
                        "cached_at": datetime.now(timezone.utc),
                        "is_fallback": is_fallback,
                    },
                    upsert=True,
                )
                print(f"[TrendData] cached {len(results)} keywords for '{industry}' geo={resolved_geo} (fallback={is_fallback})")
            except Exception as e:
                print(f"[TrendData] cache write error: {e}")

        return results

    # ── Internal sync method (runs in thread executor) ────────────────────────

    @staticmethod
    def _fetch_pytrends(industry: str, geo: str, timeframe: str, brand_seeds: List[str] = None) -> List[Dict[str, Any]]:
        from pytrends.request import TrendReq

        industry_seeds = _INDUSTRY_SEEDS.get(
            industry.lower(),
            [industry, f"{industry} tips", f"{industry} mistakes", f"{industry} guide"],
        )
        # Merge brand-specific seeds first so they get priority query slots
        extra = [s.strip() for s in (brand_seeds or []) if s and len(s.strip()) > 2]
        seeds = extra[:2] + industry_seeds  # brand seeds get first 2 slots

        hl = "en-NG" if geo == "NG" else "en-US"
        pytrend = TrendReq(hl=hl, tz=60, timeout=(10, 25), retries=1, backoff_factor=0.5)
        found: List[Dict[str, Any]] = []

        for seed in seeds[:3]:  # max 3 seeds (2 brand + 1 industry)
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
        # Also strip query-type phrases that produce bad template titles
        _QUERY_PREFIXES = (
            "how to ", "what is ", "what are ", "why is ", "why are ",
            "can i ", "should i ", "where to ", "when to ", "is it ",
            "does ", "will ", "can ", "are there ", "which ", "who ",
        )
        deduped: Dict[str, Dict] = {}
        for kw in found:
            key = kw["keyword"].lower()
            if any(key.startswith(p) for p in _QUERY_PREFIXES):
                continue  # skip question-type queries — they make bad topic substitutions
            if len(key.split()) > 5:
                continue  # skip overly long phrases
            if key not in deduped or kw["trend_score"] > deduped[key]["trend_score"]:
                deduped[key] = kw

        ranked = sorted(deduped.values(), key=lambda x: x["trend_score"], reverse=True)
        return ranked[:10] if ranked else TrendDataService._fallback_keywords(industry, brand_seeds)

    @staticmethod
    def _fallback_keywords(industry: str, brand_seeds: List[str] = None) -> List[Dict[str, Any]]:
        industry_seeds = _INDUSTRY_SEEDS.get(
            industry.lower(),
            [f"{industry} tips", f"{industry} growth", f"{industry} strategy", f"{industry} trends"],
        )
        extra = [s.strip() for s in (brand_seeds or []) if s and len(s.strip()) > 2]
        # Brand-specific seeds first, then industry defaults
        seeds = extra + [s for s in industry_seeds if s not in extra]
        return [
            {
                "keyword": kw,
                "trend_score": 35.0,
                "growth_rate": 10.0,
                "source": "fallback",
                "type": "seed",
            }
            for kw in seeds[:8]
        ]

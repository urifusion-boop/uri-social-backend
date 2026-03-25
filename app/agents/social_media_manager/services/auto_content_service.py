# app/agents/social_media_manager/services/auto_content_service.py

from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.AIService import AIService
from .content_generation_service import ContentGenerationService
from .image_content_service import ImageContentService
from .brand_profile_service import BrandProfileService


class AutoContentService:
    DEFAULT_PLATFORMS = ["facebook", "instagram"]
    FALLBACK_SEED = (
        "Nigerian SME growth strategies and fintech solutions for Lagos businesses"
    )

    # -------------------------------------------------------------------------
    # Settings helpers
    # -------------------------------------------------------------------------

    @staticmethod
    async def get_or_create_settings(user_id: str, db: AsyncIOMotorDatabase) -> dict:
        doc = await db["auto_content_settings"].find_one({"user_id": user_id})
        if not doc:
            now = datetime.utcnow()
            doc = {
                "user_id": user_id,
                "enabled": False,
                "platforms": AutoContentService.DEFAULT_PLATFORMS,
                "frequency": "daily",
                "include_images": False,
                "brand_context": None,
                "next_run_at": now + timedelta(days=1),
                "last_run_at": None,
                "last_run_draft_count": 0,
                "created_at": now,
                "updated_at": now,
            }
            await db["auto_content_settings"].insert_one({**doc})

        if "_id" in doc:
            del doc["_id"]

        # Append connected analytics context summary
        ctx_docs = await db["account_analytics_context"].find(
            {"user_id": user_id}
        ).sort("saved_at", -1).limit(10).to_list(length=10)

        connected_platforms = list({d["platform"] for d in ctx_docs if d.get("platform")})
        industries = list({
            (d.get("industry_classification") or {}).get("industry_name") or
            (d.get("industry_classification") or {}).get("name") or ""
            for d in ctx_docs
        } - {""})

        doc["analytics_context"] = {
            "connected": bool(ctx_docs),
            "connected_platforms": connected_platforms,
            "accounts_analysed": len(ctx_docs),
            "industries_detected": industries[:3],
            "last_synced_at": ctx_docs[0]["saved_at"].isoformat() if ctx_docs else None,
        }

        return doc

    @staticmethod
    async def update_settings(
        user_id: str, payload: dict, db: AsyncIOMotorDatabase
    ) -> dict:
        now = datetime.utcnow()
        frequency = payload.get("frequency", "daily")
        delta = timedelta(days=1) if frequency == "daily" else timedelta(weeks=1)
        next_run_at = now + delta

        update_fields = {
            "enabled": payload.get("enabled", False),
            "platforms": payload.get("platforms", AutoContentService.DEFAULT_PLATFORMS),
            "frequency": frequency,
            "include_images": payload.get("include_images", False),
            "brand_context": payload.get("brand_context"),
            "next_run_at": next_run_at,
            "updated_at": now,
        }

        await db["auto_content_settings"].update_one(
            {"user_id": user_id},
            {"$set": update_fields, "$setOnInsert": {"user_id": user_id, "created_at": now, "last_run_at": None, "last_run_draft_count": 0}},
            upsert=True,
        )

        doc = await db["auto_content_settings"].find_one({"user_id": user_id})
        if doc and "_id" in doc:
            del doc["_id"]
        return doc or {}

    # -------------------------------------------------------------------------
    # Account analytics context (from "Analyse" button)
    # -------------------------------------------------------------------------

    @staticmethod
    async def save_analytics_context(
        user_id: str,
        influencer_id: str,
        platform: str,
        social_user_id: Optional[str],
        insights: dict,
        db: AsyncIOMotorDatabase,
    ) -> None:
        """
        Persist the AiMediaReportDto from account tracking to a durable collection.
        Called each time the frontend fetches a fresh AI media report for an account.
        """
        now = datetime.utcnow()
        doc = {
            "user_id": user_id,
            "influencer_id": influencer_id,
            "platform": platform.lower(),
            "social_user_id": social_user_id,
            # --- rich structured fields ---
            "industry_classification": insights.get("industry_classification"),
            "content_themes": insights.get("content_themes", []),
            "key_trends": insights.get("key_trends", []),
            "engagement_drivers": insights.get("engagement_drivers", []),
            "engagement_opportunities": insights.get("engagement_opportunities", []),
            "activity_breakdown": insights.get("activity_breakdown"),
            "weekly_campaign_calendar": insights.get("weekly_campaign_calendar", []),
            "hashtag_mention_frequency": insights.get("hashtag_mention_frequency", []),
            "summary_and_achievements": insights.get("summary_and_achievements"),
            "saved_at": now,
        }
        await db["account_analytics_context"].replace_one(
            {"user_id": user_id, "influencer_id": influencer_id, "platform": platform.lower()},
            doc,
            upsert=True,
        )

    # -------------------------------------------------------------------------
    # Insights gathering
    # -------------------------------------------------------------------------

    @staticmethod
    async def gather_account_tracking_insights(
        user_id: str, db: AsyncIOMotorDatabase
    ) -> dict:
        """
        Pull context from three sources, ordered by richness:
        1. account_analytics_context — AI media reports from the "Analyse" button
        2. influencers collection     — resolve tracked social_user_ids
        3. embeddings                 — top-performing raw posts (ACCOUNT_TRACKING_EMBEDDING)
        """
        # ── 1. Rich analytics context (from Analyse button) ──────────────────
        analytics_docs = await db["account_analytics_context"].find(
            {"user_id": user_id}
        ).sort("saved_at", -1).limit(5).to_list(length=5)

        # ── 2. Tracked account social_user_ids (influencers collection) ───────
        influencer_docs = await db["influencers"].find(
            {"user_id": user_id}
        ).to_list(length=50)

        tracked_social_ids = [
            str(d.get("social_user_id"))
            for d in influencer_docs
            if d.get("social_user_id")
        ]

        # Also include social_connections (own posting accounts)
        connections = await db["social_connections"].find(
            {"user_id": user_id}
        ).to_list(length=20)
        for conn in connections:
            sid = conn.get("social_user_id") or conn.get("page_id")
            if sid and str(sid) not in tracked_social_ids:
                tracked_social_ids.append(str(sid))

        # ── 3. Embedding top posts ────────────────────────────────────────────
        top_posts: List[str] = []
        top_hashtags: List[str] = []
        avg_engagement: float = 0.0

        if tracked_social_ids:
            cursor = (
                db["embeddings"]
                .find(
                    {
                        "embedding_type": "ACCOUNT_TRACKING_EMBEDDING",
                        "social_user_id": {"$in": tracked_social_ids},
                    }
                )
                .sort("engagement_count", -1)
                .limit(30)
            )
            embed_docs = await cursor.to_list(length=30)

            for doc in embed_docs[:5]:
                text = doc.get("content") or doc.get("caption") or doc.get("text") or ""
                if text:
                    top_posts.append(text[:300])

            hashtag_counts: Dict[str, int] = {}
            for doc in embed_docs:
                for tag in (doc.get("hashtags") or []):
                    hashtag_counts[tag] = hashtag_counts.get(tag, 0) + 1
            top_hashtags = sorted(hashtag_counts, key=hashtag_counts.get, reverse=True)[:10]

            engagements = [d.get("engagement_count", 0) for d in embed_docs if d.get("engagement_count")]
            avg_engagement = round(sum(engagements) / len(engagements), 1) if engagements else 0.0

        # ── Compile rich analytics from account_analytics_context ─────────────
        industry: Optional[dict] = None
        content_themes: List[dict] = []
        key_trends: List[str] = []
        engagement_drivers: List[str] = []
        engagement_opportunities: List[str] = []
        activity_breakdown: Optional[dict] = None
        calendar_entries: List[dict] = []
        hashtag_freq: List[dict] = []
        summaries: List[str] = []

        for adoc in analytics_docs:
            if not industry and adoc.get("industry_classification"):
                industry = adoc["industry_classification"]
            content_themes.extend(adoc.get("content_themes") or [])
            key_trends.extend(adoc.get("key_trends") or [])
            engagement_drivers.extend(adoc.get("engagement_drivers") or [])
            engagement_opportunities.extend(adoc.get("engagement_opportunities") or [])
            if not activity_breakdown and adoc.get("activity_breakdown"):
                activity_breakdown = adoc["activity_breakdown"]
            calendar_entries.extend(adoc.get("weekly_campaign_calendar") or [])
            hashtag_freq.extend(adoc.get("hashtag_mention_frequency") or [])
            sa = adoc.get("summary_and_achievements")
            if sa and sa.get("summary"):
                summaries.append(sa["summary"])

        # Deduplicate key_trends & engagement_drivers
        key_trends = list(dict.fromkeys(key_trends))[:8]
        engagement_drivers = list(dict.fromkeys(engagement_drivers))[:6]
        engagement_opportunities = list(dict.fromkeys(engagement_opportunities))[:4]

        # Merge hashtags from frequency list (if no embedding hashtags)
        if not top_hashtags and hashtag_freq:
            sorted_hf = sorted(hashtag_freq, key=lambda x: x.get("count", 0), reverse=True)
            top_hashtags = [h["hashtag"] for h in sorted_hf[:10] if h.get("hashtag")]

        has_data = bool(top_posts or analytics_docs)

        return {
            "top_posts": top_posts,
            "top_hashtags": top_hashtags,
            "avg_engagement": avg_engagement,
            "has_data": has_data,
            # rich analytics context ↓
            "industry": industry,
            "content_themes": content_themes[:10],
            "key_trends": key_trends,
            "engagement_drivers": engagement_drivers,
            "engagement_opportunities": engagement_opportunities,
            "activity_breakdown": activity_breakdown,
            "calendar_entries": calendar_entries[:7],   # up to 7 past calendar drafts
            "account_summaries": summaries[:3],
            "has_analytics_context": bool(analytics_docs),
        }

    # -------------------------------------------------------------------------
    # GPT-4o brief synthesis
    # -------------------------------------------------------------------------

    @staticmethod
    async def synthesize_content_brief(insights: dict, platforms: List[str]) -> str:
        """
        Ask GPT-4o to synthesise a 200-400 char seed_content from the account's
        own performance data.

        Context priority (richest first):
        1. account_analytics_context (AI media report from Analyse button):
           - industry classification, content themes, engagement drivers,
             weekly campaign calendar topics, activity breakdown
        2. embedding top posts + hashtags
        3. Fallback seed if no data at all
        """
        if not insights.get("has_data"):
            return AutoContentService.FALLBACK_SEED

        # ── Build context sections ────────────────────────────────────────────
        sections: List[str] = []

        # Industry
        industry = insights.get("industry")
        if industry:
            name = industry.get("industry_name") or industry.get("name") or ""
            overview = industry.get("overview") or industry.get("text") or ""
            if name:
                sections.append(f"Industry: {name}" + (f" — {overview[:200]}" if overview else ""))

        # Account summary
        for s in insights.get("account_summaries", [])[:1]:
            sections.append(f"Account summary: {s[:300]}")

        # Top-performing posts (from embeddings)
        if insights.get("top_posts"):
            posts_text = "\n".join(f"  • {p}" for p in insights["top_posts"])
            sections.append(f"Top-performing posts:\n{posts_text}")

        # Content themes from AI analysis
        if insights.get("content_themes"):
            themes = ", ".join(
                t.get("theme", "") for t in insights["content_themes"][:6] if t.get("theme")
            )
            if themes:
                sections.append(f"Content themes that resonate: {themes}")

        # Engagement drivers
        if insights.get("engagement_drivers"):
            sections.append(
                "Engagement drivers: " + "; ".join(insights["engagement_drivers"][:5])
            )

        # Key trends
        if insights.get("key_trends"):
            sections.append(
                "Key trends to leverage: " + "; ".join(insights["key_trends"][:5])
            )

        # Activity breakdown
        ab = insights.get("activity_breakdown")
        if ab:
            parts = []
            if ab.get("top_performing_media_type"):
                parts.append(f"best media type: {ab['top_performing_media_type']}")
            if ab.get("peak_posting_time"):
                parts.append(f"peak time: {ab['peak_posting_time']}")
            if ab.get("engagement_trend"):
                parts.append(f"trend: {ab['engagement_trend']}")
            if parts:
                sections.append("Activity breakdown — " + ", ".join(parts))

        # Weekly calendar topics (to inspire, not copy verbatim)
        if insights.get("calendar_entries"):
            topics = [
                e.get("topic") or e.get("title") or ""
                for e in insights["calendar_entries"]
                if e.get("topic") or e.get("title")
            ]
            if topics:
                sections.append(
                    "Proven campaign topics from past analysis: "
                    + " | ".join(topics[:5])
                )

        # Top hashtags
        hashtags_text = ", ".join(insights.get("top_hashtags", []))
        if hashtags_text:
            sections.append(f"Top hashtags: {hashtags_text}")

        # Average engagement
        if insights.get("avg_engagement"):
            sections.append(f"Average engagement per post: {insights['avg_engagement']}")

        context_block = "\n".join(sections) if sections else "(no prior data)"
        has_rich = insights.get("has_analytics_context", False)

        system_prompt = (
            "You are a social media strategist with deep knowledge of this business's "
            "own account performance. "
            + ("You have access to AI-analysed insights from their account tracking reports, "
               "including industry classification, proven content themes, engagement drivers, "
               "and past campaign calendar topics. " if has_rich else "")
            + "Based on this context, write a concise content seed (200-400 characters) "
            "that captures what resonates with this specific audience. "
            "Output ONLY the seed text — no preamble, no quotes, no hashtags."
        )

        user_prompt = (
            f"Business context:\n{context_block}\n\n"
            f"Target platforms: {', '.join(platforms)}\n\n"
            "Write a focused content seed that will inspire platform-native posts "
            "tailored to this account's proven themes and audience preferences."
        )

        try:
            ai_request = AIService.build_ai_model(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model="gpt-5.4",
                temperature=0.6,
            )
            ai_response = await AIService.chat_completion(ai_request)
            seed = ai_response.choices[0].message.content.strip()
            return seed[:400] if seed else AutoContentService.FALLBACK_SEED
        except Exception as exc:
            print(f"[AutoContentService] GPT-4o brief synthesis failed: {exc}")
            return AutoContentService.FALLBACK_SEED

    # -------------------------------------------------------------------------
    # Full per-user pipeline
    # -------------------------------------------------------------------------

    @staticmethod
    async def generate_for_user(user_id: str, db: AsyncIOMotorDatabase) -> dict:
        """
        Full pipeline:
        1. Load settings
        2. Gather insights
        3. Synthesise brief
        4. Generate platform drafts (tagging auto_generated=True)
        5. Update settings with run stats
        Returns {"drafts_created": N, "platforms": [...], "seed_used": str}
        """
        settings_doc = await AutoContentService.get_or_create_settings(user_id, db)
        platforms = settings_doc.get("platforms", AutoContentService.DEFAULT_PLATFORMS)
        include_images = settings_doc.get("include_images", False)
        settings_brand_context = settings_doc.get("brand_context") or {}

        # Load the rich brand profile from onboarding (source of truth)
        profile_result = await BrandProfileService.get(user_id, db)
        profile_data = (profile_result.get("responseData") or {}) if profile_result.get("status") else {}
        profile_brand_context = BrandProfileService.to_brand_context(profile_data) if profile_data else {}

        # Gather analytics insights
        insights = await AutoContentService.gather_account_tracking_insights(user_id, db)

        # Synthesise seed — prefer brand profile industry if available
        seed = await AutoContentService.synthesize_content_brief(insights, platforms)

        # Build auto context from analytics insights
        industry_raw = insights.get("industry") or {}
        auto_brand_context = {
            "industry": (
                industry_raw.get("industry_name")
                or industry_raw.get("name")
                or "business"
            ),
            "industry_overview": (
                industry_raw.get("overview")
                or industry_raw.get("text")
                or ""
            ),
            "content_themes": insights.get("content_themes", [])[:5],
            "engagement_drivers": insights.get("engagement_drivers", [])[:4],
        }
        # Priority: brand profile > settings brand_context > auto-derived context
        merged_brand_context = {**auto_brand_context, **profile_brand_context, **settings_brand_context}

        # Generate content
        if include_images:
            result = await ImageContentService.generate_content_with_images(
                user_id=user_id,
                seed_content=seed,
                platforms=platforms,
                include_images=True,
                brand_context=merged_brand_context,
                db=db,
            )
        else:
            result = await ContentGenerationService.generate_multi_platform_content(
                user_id=user_id,
                seed_content=seed,
                platforms=platforms,
                seed_type="auto_generated",
                brand_context=merged_brand_context,
                db=db,
            )

        # Count drafts created
        drafts_created = 0
        if result and result.get("status"):
            rd = result.get("responseData", {})
            drafts = rd.get("drafts", [])
            drafts_created = len(drafts)

            # Tag all drafts as auto_generated
            if drafts_created > 0:
                draft_ids = [d.get("id") or d.get("draft_id") for d in drafts if d.get("id") or d.get("draft_id")]
                if draft_ids:
                    await db["content_drafts"].update_many(
                        {"id": {"$in": draft_ids}},
                        {"$set": {"auto_generated": True}},
                    )

        # Update settings
        now = datetime.utcnow()
        frequency = settings_doc.get("frequency", "daily")
        delta = timedelta(days=1) if frequency == "daily" else timedelta(weeks=1)
        await db["auto_content_settings"].update_one(
            {"user_id": user_id},
            {
                "$set": {
                    "last_run_at": now,
                    "last_run_draft_count": drafts_created,
                    "next_run_at": now + delta,
                    "updated_at": now,
                }
            },
        )

        return {
            "drafts_created": drafts_created,
            "platforms": platforms,
            "seed_used": seed,
        }

    # -------------------------------------------------------------------------
    # Scheduler entry-point
    # -------------------------------------------------------------------------

    @staticmethod
    async def run_scheduled_auto_generation(db: AsyncIOMotorDatabase):
        """
        Cron job: iterate all users with enabled=True and next_run_at <= now.
        """
        now = datetime.utcnow()
        print(f"[{now}] Starting auto_content_generation job...")

        try:
            due_users = await db["auto_content_settings"].find(
                {"enabled": True, "next_run_at": {"$lte": now}}
            ).to_list(length=None)

            print(f"[AutoContentService] {len(due_users)} user(s) due for auto generation")

            for doc in due_users:
                user_id = doc["user_id"]
                try:
                    result = await AutoContentService.generate_for_user(user_id, db)
                    print(
                        f"  ✓ user={user_id} drafts={result['drafts_created']} "
                        f"platforms={result['platforms']}"
                    )
                except Exception as user_err:
                    print(f"  ✗ user={user_id} error={user_err}")

        except Exception as exc:
            print(f"[{datetime.utcnow()}] ERROR in auto_content_generation job: {exc}")

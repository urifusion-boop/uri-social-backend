from datetime import datetime
from typing import Dict, Any, Optional, List
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.domain.responses.uri_response import UriResponse


class BrandProfileService:
    COLLECTION = "brand_profiles"

    @staticmethod
    async def save(
        user_id: str,
        data: Dict[str, Any],
        db: AsyncIOMotorDatabase,
    ) -> Dict[str, Any]:
        now = datetime.utcnow()

        doc = {
            "user_id": user_id,
            "brand_name": data.get("brand_name", ""),
            "industry": data.get("industry", ""),
            "website": data.get("website", ""),
            "tagline": data.get("tagline", ""),
            "product_description": data.get("product_description", ""),
            "key_products_services": data.get("key_products_services", []),
            "logo_url": data.get("logo_url"),
            "sample_template_urls": data.get("sample_template_urls", []),
            "brand_colors": data.get("brand_colors", []),
            "personality_quiz": data.get("personality_quiz", {}),
            "derived_voice": data.get("derived_voice", ""),
            "voice_sample": data.get("voice_sample", ""),
            "platform_tones": data.get("platform_tones", {}),
            "same_tone_everywhere": data.get("same_tone_everywhere", True),
            "content_pillars": data.get("content_pillars", []),
            "preferred_formats": data.get("preferred_formats", []),
            "guardrails": data.get("guardrails", {}),
            "cta_styles": data.get("cta_styles", []),
            "default_link": data.get("default_link", ""),
            "audience_age_range": data.get("audience_age_range", ""),
            "target_platforms": data.get("target_platforms", []),
            "primary_goal": data.get("primary_goal", ""),
            "competitor_handles": data.get("competitor_handles", []),
            "key_dates": data.get("key_dates", []),
            "posting_cadence": data.get("posting_cadence", ""),
            "posting_time_mode": data.get("posting_time_mode", ""),
            "posting_time_prefs": data.get("posting_time_prefs", {}),
            "approval_workflow": data.get("approval_workflow", ""),
            "approval_channels": data.get("approval_channels", []),
            "notification_events": data.get("notification_events", []),
            "notification_channel": data.get("notification_channel", ""),
            "team_members": data.get("team_members", []),
            "languages": data.get("languages", []),
            "region": data.get("region", ""),
            "onboarding_completed": data.get("onboarding_completed", False),
            "updated_at": now,
        }
        if "style_selections" in data:
            doc["style_selections"] = data["style_selections"]
        if "style_prompt_fragments" in data:
            doc["style_prompt_fragments"] = data["style_prompt_fragments"]
        if "font_style" in data:
            doc["font_style"] = data["font_style"]
        if "font_style_prompt" in data:
            doc["font_style_prompt"] = data["font_style_prompt"]

        # OPTION 2: ONBOARDING VALIDATION - Enforce required fields
        # When user tries to mark onboarding as complete, validate critical fields
        if doc.get("onboarding_completed"):
            required_for_completion = {
                "brand_name": doc.get("brand_name"),
                "industry": doc.get("industry"),
                "brand_colors": doc.get("brand_colors") and len(doc.get("brand_colors", [])) > 0,
            }

            missing = [field for field, value in required_for_completion.items() if not value]

            if missing:
                # Cannot complete onboarding without required fields
                doc["onboarding_completed"] = False
                return UriResponse.error_response(
                    f"Cannot complete onboarding. Please provide: {', '.join(missing)}",
                    code=400
                )

        existing = await db[BrandProfileService.COLLECTION].find_one({"user_id": user_id})
        if existing:
            # Once onboarding_completed is True, never allow it to be reset to False
            if existing.get("onboarding_completed") and not doc.get("onboarding_completed"):
                doc["onboarding_completed"] = True
            await db[BrandProfileService.COLLECTION].update_one(
                {"user_id": user_id}, {"$set": doc}
            )
        else:
            doc["created_at"] = now
            await db[BrandProfileService.COLLECTION].insert_one(doc)

        result = await db[BrandProfileService.COLLECTION].find_one({"user_id": user_id})
        if result:
            result.pop("_id", None)
        return UriResponse.get_single_data_response("brand_profile", result)

    @staticmethod
    async def get(user_id: str, db: AsyncIOMotorDatabase) -> Dict[str, Any]:
        profile = await db[BrandProfileService.COLLECTION].find_one({"user_id": user_id})
        if not profile:
            return UriResponse.get_single_data_response("brand_profile", None)
        profile.pop("_id", None)
        return UriResponse.get_single_data_response("brand_profile", profile)

    @staticmethod
    def to_brand_context(profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not profile:
            return {}

        voice_parts: List[str] = []
        if profile.get("derived_voice"):
            voice_parts.append(profile["derived_voice"])

        quiz = profile.get("personality_quiz") or {}
        if quiz.get("formality"):
            voice_parts.append("formal" if quiz["formality"] == "formal" else "casual")
        if quiz.get("energy"):
            voice_parts.append("bold and energetic" if quiz["energy"] == "bold" else "calm and reassuring")
        if quiz.get("humor"):
            voice_parts.append("witty and playful" if quiz["humor"] == "witty" else "direct and to the point")
        if quiz.get("approach"):
            voice_parts.append("educational" if quiz["approach"] == "educational" else "inspirational")

        brand_voice = ", ".join(voice_parts) if voice_parts else ""

        audience_parts: List[str] = []
        if profile.get("audience_age_range"):
            audience_parts.append(f"age {profile['audience_age_range']}")
        if profile.get("primary_goal"):
            audience_parts.append(f"goal: {profile['primary_goal']}")
        if profile.get("region"):
            audience_parts.append(f"market: {profile['region']}")
        target_audience = " | ".join(audience_parts) if audience_parts else ""

        key_dates_str = ""
        key_dates = profile.get("key_dates") or []
        if key_dates:
            date_items = [
                f"{d.get('label', '')} ({d.get('date', '')})" if isinstance(d, dict) else str(d)
                for d in key_dates[:5]
            ]
            key_dates_str = ", ".join(d for d in date_items if d.strip())

        return {
            "brand_name":           profile.get("brand_name", ""),
            "industry":             profile.get("industry", ""),
            "website":              profile.get("website", ""),
            "tagline":              profile.get("tagline", ""),
            "business_description": profile.get("product_description", ""),
            "key_products_services": [p for p in (profile.get("key_products_services") or []) if p],
            "logo_url":             profile.get("logo_url"),
            "sample_template_urls": [u for u in (profile.get("sample_template_urls") or []) if u],
            "brand_colors":         profile.get("brand_colors") or [],
            "brand_voice":          brand_voice,
            "voice_sample":         profile.get("voice_sample", ""),
            "platform_tones":       profile.get("platform_tones") or {},
            "same_tone_everywhere": profile.get("same_tone_everywhere", True),
            "target_audience":      target_audience,
            "audience_age_range":   profile.get("audience_age_range", ""),
            "primary_goal":         profile.get("primary_goal", ""),
            "target_platforms":     profile.get("target_platforms") or [],
            "region":               profile.get("region", ""),
            "languages":            profile.get("languages") or [],
            "content_pillars":      profile.get("content_pillars") or [],
            "preferred_formats":    profile.get("preferred_formats") or [],
            "guardrails":           profile.get("guardrails") or {},
            "cta_styles":           profile.get("cta_styles") or [],
            "default_link":         profile.get("default_link", ""),
            "competitor_handles":   [h for h in (profile.get("competitor_handles") or []) if h],
            "key_dates":            key_dates_str,
            "posting_cadence":      profile.get("posting_cadence", ""),
            "style_selections":     profile.get("style_selections") or [],
            "style_rotation_index": int(profile.get("style_rotation_index") or 0),
            "font_style":           profile.get("font_style", ""),
            "font_style_prompt":    profile.get("font_style_prompt", ""),
        }

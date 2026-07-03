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
        brand_id: Optional[str] = None,
        sdk_client_id: Optional[str] = None,
        end_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        now = datetime.utcnow()

        doc = {
            "user_id": user_id,
            "brand_id": brand_id,
            # Multi-tenant fields (optional, for SDK end-users)
            "sdk_client_id": sdk_client_id,
            "end_user_id": end_user_id,
            "brand_name": data.get("brand_name", ""),
            "industry": data.get("industry", ""),
            "website": data.get("website", ""),
            "tagline": data.get("tagline", ""),
            "product_description": data.get("product_description", ""),
            "key_products_services": data.get("key_products_services", []),
            "logo_url": data.get("logo_url"),
            "logo_position": data.get("logo_position", "bottom_right"),
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
            # Caption Voice System fields (PRD Section 3.1)
            "voice_profile": {
                "formality": data.get("voice_profile", {}).get("formality", "casual"),
                "sentence_style": data.get("voice_profile", {}).get("sentence_style", "mixed_rhythm"),
                "emoji_usage": data.get("voice_profile", {}).get("emoji_usage", "light"),
                "emoji_placement": data.get("voice_profile", {}).get("emoji_placement", "end_of_lines"),
                "slang_level": data.get("voice_profile", {}).get("slang_level", "pure_english"),
                "cta_style": data.get("voice_profile", {}).get("cta_style", "direct"),
                "caption_length": data.get("voice_profile", {}).get("caption_length", "short"),
                "hook_style": data.get("voice_profile", {}).get("hook_style", "bold_statement"),
                "hashtag_style": data.get("voice_profile", {}).get("hashtag_style", "minimal"),
                "hashtag_placement": data.get("voice_profile", {}).get("hashtag_placement", "end"),
                "humor_level": data.get("voice_profile", {}).get("humor_level", "none"),
                "nigerian_expressions": data.get("voice_profile", {}).get("nigerian_expressions", []),
                "banned_words": data.get("voice_profile", {}).get("banned_words", []),
                "sample_captions": data.get("voice_profile", {}).get("sample_captions", []),
                "platform_overrides": data.get("voice_profile", {}).get("platform_overrides", {}),
            },
            # Voice sample analysis (PRD Section 6.1)
            "voice_sample_analysis": data.get("voice_sample_analysis", {}),
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

        # Custom font fields (Typography System)
        if "custom_font_enabled" in data:
            doc["custom_font_enabled"] = data["custom_font_enabled"]
        if "custom_font_files" in data:
            doc["custom_font_files"] = data["custom_font_files"]
        if "custom_font_analysis" in data:
            doc["custom_font_analysis"] = data["custom_font_analysis"]
        if "custom_font_directive" in data:
            doc["custom_font_directive"] = data["custom_font_directive"]

        # Canvas Editor feature flag
        if "canvas_editor_enabled" in data:
            print(f"🎨 Canvas Editor: saving canvas_editor_enabled={data['canvas_editor_enabled']}")
            doc["canvas_editor_enabled"] = data["canvas_editor_enabled"]
        else:
            print(f"🎨 Canvas Editor: canvas_editor_enabled NOT in data. Keys: {list(data.keys())[:20]}")

        # V3 Prompts feature flag
        if "use_v3_prompts" in data:
            doc["use_v3_prompts"] = data["use_v3_prompts"]

        # Custom Visual Guides selections
        if "selected_custom_guides" in data:
            doc["selected_custom_guides"] = data["selected_custom_guides"]
        if "selected_custom_guides_v2" in data:
            doc["selected_custom_guides_v2"] = data["selected_custom_guides_v2"]
        if "style_rotation_index" in data:
            doc["style_rotation_index"] = data["style_rotation_index"]

        # Multi-tenant isolation: end_user_id is the primary isolation boundary
        # Priority: end_user_id > brand_id > user_id (for backward compatibility)
        if end_user_id:
            # Multi-tenant mode: query by end_user_id (SDK end-user's profile)
            scope = {"end_user_id": end_user_id}
            existing = await db[BrandProfileService.COLLECTION].find_one({"end_user_id": end_user_id})
        elif brand_id:
            # Agency mode: query by brand_id (team brand)
            # Try to find by brand_id first, then by user_id (for migration from old profiles)
            existing = await db[BrandProfileService.COLLECTION].find_one({"brand_id": brand_id})
            if not existing:
                # Fallback: old profile created before brand_id was added
                existing = await db[BrandProfileService.COLLECTION].find_one({"user_id": user_id, "brand_id": {"$exists": False}})

            # Set scope: if existing profile has no brand_id, update by user_id; otherwise by brand_id
            if existing and not existing.get("brand_id"):
                scope = {"user_id": user_id, "brand_id": {"$exists": False}}
            else:
                scope = {"brand_id": brand_id}
        else:
            # Single-user mode: query by user_id (personal brand)
            scope = {"user_id": user_id, "brand_id": {"$exists": False}}
            existing = await db[BrandProfileService.COLLECTION].find_one(scope)

        # OPTION 2: ONBOARDING VALIDATION - Enforce required fields
        # Only validate when user is ACTIVELY TRYING to complete onboarding (transition from False→True)
        # Don't block subsequent saves after onboarding is already complete
        is_completing_onboarding = (
            doc.get("onboarding_completed") and
            (not existing or not existing.get("onboarding_completed"))
        )

        # Allow completing onboarding even without required fields
        # Users can skip all onboarding steps and go directly to dashboard
        # They can fill in brand profile details later
        if is_completing_onboarding:
            # No validation - all fields are optional
            pass

        if existing:
            # Once onboarding_completed is True, never allow it to be reset to False
            if existing.get("onboarding_completed") and not doc.get("onboarding_completed"):
                doc["onboarding_completed"] = True

            identifier = end_user_id or brand_id or user_id
            print(f"🖼️  SAVE DEBUG identifier={identifier}: saving logo_position={repr(doc.get('logo_position'))}")

            # Update existing profile using the scope (respects multi-tenant isolation)
            try:
                result = await db[BrandProfileService.COLLECTION].update_one(
                    scope, {"$set": doc}
                )
                print(f"✅ Updated brand profile for {scope}: matched={result.matched_count}, modified={result.modified_count}")
            except Exception as e:
                print(f"❌ Error updating brand profile for {scope}: {e}")
                raise
        else:
            doc["created_at"] = now
            try:
                await db[BrandProfileService.COLLECTION].insert_one(doc)
                identifier = end_user_id or brand_id or user_id
                print(f"✅ Created new brand profile for {identifier}")
            except Exception as e:
                # Handle duplicate key error
                if "duplicate key" in str(e).lower():
                    identifier = end_user_id or brand_id or user_id
                    print(f"⚠️  Duplicate profile detected for {identifier}, updating instead")
                    # Profile was created by another request, update it instead
                    result = await db[BrandProfileService.COLLECTION].update_one(
                        scope, {"$set": doc}
                    )
                    print(f"✅ Updated via fallback for {identifier}: matched={result.matched_count}, modified={result.modified_count}")
                else:
                    print(f"❌ Error creating brand profile for user {user_id}: {e}")
                    raise

        result = await db[BrandProfileService.COLLECTION].find_one(scope)
        if result:
            result.pop("_id", None)
        return UriResponse.get_single_data_response("brand_profile", result)

    @staticmethod
    async def get(
        user_id: str,
        db: AsyncIOMotorDatabase,
        brand_id: Optional[str] = None,
        end_user_id: Optional[str] = None
    ) -> Dict[str, Any]:
        # Multi-tenant mode: query by end_user_id (highest priority)
        if end_user_id:
            profile = await db[BrandProfileService.COLLECTION].find_one({"end_user_id": end_user_id})
            if not profile:
                return UriResponse.get_single_data_response("brand_profile", None)
            profile.pop("_id", None)
            return UriResponse.get_single_data_response("brand_profile", profile)

        # Agency/personal brand mode (backward compatible)
        from app.models.brand_account import BrandAccount
        personal_bid = BrandAccount.personal_brand_id(user_id)
        is_agency_brand = brand_id and brand_id != personal_bid

        # For personal brands, try multiple query strategies to find legacy profiles
        # that were saved with user_id only (before brand_id was introduced)
        if brand_id and not is_agency_brand:
            scope_options = [
                {"brand_id": brand_id},
                {"user_id": user_id, "brand_id": {"$exists": False}},
                {"user_id": user_id},
            ]
            profile = None
            for scope in scope_options:
                profile = await db[BrandProfileService.COLLECTION].find_one(scope)
                if profile:
                    break
        else:
            scope = {"brand_id": brand_id} if brand_id else {"user_id": user_id}
            profile = await db[BrandProfileService.COLLECTION].find_one(scope)

        # For agency brands: if the profile is missing or lacks key playbook fields
        # (colors, visual style, industry), merge in the personal brand's values so
        # content generation has a full context rather than using generic defaults.
        if is_agency_brand:
            personal_scope_options = [
                {"brand_id": personal_bid},
                {"user_id": user_id, "brand_id": {"$exists": False}},
                {"user_id": user_id},
            ]
            personal_profile = None
            for ps in personal_scope_options:
                personal_profile = await db[BrandProfileService.COLLECTION].find_one(ps)
                if personal_profile:
                    break

            PLAYBOOK_FIELDS = [
                "brand_colors", "industry", "visual_style", "aesthetic_keywords",
                "derived_voice", "personality_quiz", "audience_age_range",
                "audience_interests", "content_tones", "cta_styles", "default_link",
                "tagline", "region", "font_preference", "logo_url", "logo_position",
                "onboarding_completed",
            ]
            if profile is None and personal_profile:
                # No agency profile at all — use personal profile as base
                profile = dict(personal_profile)
                profile.pop("_id", None)
                profile["brand_id"] = brand_id  # stamp agency brand_id for context
            elif profile and personal_profile:
                # Agency profile exists but may be sparse — fill missing fields from personal
                profile.pop("_id", None)
                for field in PLAYBOOK_FIELDS:
                    if not profile.get(field) and personal_profile.get(field):
                        profile[field] = personal_profile[field]
                return UriResponse.get_single_data_response("brand_profile", profile)

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
            "logo_position":        profile.get("logo_position", "bottom_right"),
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
            # Custom font fields (Typography System)
            "custom_font_enabled":  profile.get("custom_font_enabled", False),
            "custom_font_files":    profile.get("custom_font_files") or [],
            "custom_font_analysis": profile.get("custom_font_analysis") or {},
            "custom_font_directive": profile.get("custom_font_directive", ""),
            # Caption Voice System fields
            "voice_profile":        profile.get("voice_profile") or {},
            "voice_sample_analysis": profile.get("voice_sample_analysis") or {},
        }

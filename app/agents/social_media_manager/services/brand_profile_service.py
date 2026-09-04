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
    ) -> Dict[str, Any]:
        now = datetime.utcnow()

        # Debug: Log what data is received from frontend
        print(f"📥 SAVE INPUT: logo_position={repr(data.get('logo_position'))}, logo_size={repr(data.get('logo_size'))}")

        # Fetched up front (rather than right before the update_one call, as
        # before) so the field-defaulting loop below can tell a brand-new
        # profile from an update to an existing one.
        scope = {"brand_id": brand_id} if brand_id else {"user_id": user_id}
        existing = await db[BrandProfileService.COLLECTION].find_one(scope)

        # Only ever $set a field if the caller's payload actually included it —
        # unless this is a brand-new profile, in which case every field still
        # gets backfilled with a sensible default so the initial document is
        # fully populated. This used to build a full ~30-field document on
        # EVERY save using data.get(field, default) regardless of whether it
        # was an insert or an update — meaning any partial save (e.g. the
        # custom-guide-selection or Canvas Editor auto-saves elsewhere in the
        # frontend, which only send a couple of changed fields plus whatever
        # was in their local cached copy of the profile) would silently reset
        # every other field to its empty default. Confirmed live: 0 of 123
        # production brand profiles ever had a non-empty ideal_customer_profile
        # despite the save/read code path working correctly in isolation.
        doc: Dict[str, Any] = {
            "user_id": user_id,
            "brand_id": brand_id,
            "updated_at": now,
        }

        DEFAULTS: Dict[str, Any] = {
            "brand_name": "",
            "industry": "",
            "website": "",
            "tagline": "",
            "product_description": "",
            "key_products_services": [],
            # Business Details (Content Calendar Automation inputs)
            "price_range": "",
            "unique_selling_proposition": "",
            "business_stage": "",  # "" | new | growing | established | market_leader
            "business_priorities": [],
            "logo_url": None,
            "logo_position": "bottom_right",
            "logo_size": "small",
            "sample_template_urls": [],
            "brand_colors": [],
            "personality_quiz": {},
            "derived_voice": "",
            "voice_sample": "",
            "platform_tones": {},
            "same_tone_everywhere": True,
            "target_audience": "",
            "ideal_customer_profile": "",
            # Target Customer Detail (additive to target_audience/ideal_customer_profile
            # above, not a replacement — see to_brand_context() for why these need
            # their own distinct keys rather than reusing "target_audience")
            "customer_gender": "",
            "customer_location": "",
            "customer_occupation": "",
            "customer_income_level": "",
            "customer_interests": [],
            "customer_pain_points": [],
            "customer_needs": [],
            "customer_objections": [],
            "why_customers_choose_us": "",
            "content_pillars": [],
            "preferred_formats": [],
            "guardrails": {},
            "cta_styles": [],
            "default_link": "",
            "audience_age_range": "",
            "target_platforms": [],
            "primary_goal": "",
            "competitor_handles": [],
            "key_dates": [],
            "posting_cadence": "",
            "posting_time_mode": "",
            "posting_time_prefs": {},
            "approval_workflow": "",
            "approval_channels": [],
            "notification_events": [],
            "notification_channel": "",
            "team_members": [],
            "languages": [],
            "region": "",
            "onboarding_completed": False,
            "voice_sample_analysis": {},
        }
        for field, default in DEFAULTS.items():
            if field in data:
                doc[field] = data[field]
            elif existing is None:
                doc[field] = default

        # Caption Voice System fields (PRD Section 3.1) — same "only touch what
        # was sent" rule applied per sub-field, not just at the top level, so a
        # save that only changes e.g. emoji_usage doesn't reset banned_words etc.
        vp_defaults = {
            "formality": "casual",
            "sentence_style": "mixed_rhythm",
            "emoji_usage": "light",
            "emoji_placement": "end_of_lines",
            "slang_level": "pure_english",
            "cta_style": "direct",
            "caption_length": "short",
            "hook_style": "bold_statement",
            "hashtag_style": "minimal",
            "hashtag_placement": "end",
            "humor_level": "none",
            "nigerian_expressions": [],
            "banned_words": [],
            "sample_captions": [],
            "platform_overrides": {},
        }
        if "voice_profile" in data:
            incoming_vp = data.get("voice_profile") or {}
            existing_vp = (existing or {}).get("voice_profile") or {}
            doc["voice_profile"] = {
                key: incoming_vp[key] if key in incoming_vp else existing_vp.get(key, default)
                for key, default in vp_defaults.items()
            }
        elif existing is None:
            doc["voice_profile"] = dict(vp_defaults)

        # Business Pulse — time-sensitive "what's happening right now" fields
        # (current promotions/campaigns/news/milestones/new products), saved as a
        # unit from their own dedicated panel/endpoint rather than the main wizard.
        # Deliberately absent from PLAYBOOK_FIELDS below: one brand's active
        # promotion is meaningless noise for a sibling agency brand, unlike the
        # evergreen fields above which legitimately backfill.
        bp_defaults = {
            "current_period_goal": "",
            "current_promotions": [],
            "current_campaigns": [],
            "business_news_announcements": [],
            "recent_milestones": [],
            "new_products_services": [],
        }
        if "business_pulse" in data:
            incoming_bp = data.get("business_pulse") or {}
            existing_bp = (existing or {}).get("business_pulse") or {}
            doc["business_pulse"] = {
                key: incoming_bp[key] if key in incoming_bp else existing_bp.get(key, default)
                for key, default in bp_defaults.items()
            }
            doc["business_pulse_updated_at"] = now
        elif existing is None:
            doc["business_pulse"] = dict(bp_defaults)
            doc["business_pulse_updated_at"] = None

        # Onboarding save-and-resume: the wizard fires a partial save on every
        # step transition (see brand-setup/page.tsx), sending only that step's
        # own fields plus these three. onboarding_current_step is the step's
        # NAME (e.g. "targetCustomerDetail"), not a numeric index — indexes
        # break silently if a step is later added/removed/reordered, whereas
        # an unrecognized name just falls back to step 0 on the frontend.
        # onboarding_started_at is set once, on the first partial save, and
        # never overwritten after — every subsequent save only touches
        # onboarding_current_step/onboarding_last_saved_at.
        if "onboarding_current_step" in data:
            doc["onboarding_current_step"] = data["onboarding_current_step"]
            doc["onboarding_last_saved_at"] = now
            if not (existing or {}).get("onboarding_started_at"):
                doc["onboarding_started_at"] = now

        if "style_selections" in data:
            doc["style_selections"] = data["style_selections"]
        if "style_prompt_fragments" in data:
            doc["style_prompt_fragments"] = data["style_prompt_fragments"]
        if "font_style" in data:
            doc["font_style"] = data["font_style"]
        if "font_style_prompt" in data:
            doc["font_style_prompt"] = data["font_style_prompt"]

        # Primary & Secondary Font fields
        if "primary_font" in data:
            doc["primary_font"] = data["primary_font"]
        if "primary_font_prompt" in data:
            doc["primary_font_prompt"] = data["primary_font_prompt"]
        if "secondary_font" in data:
            doc["secondary_font"] = data["secondary_font"]
        if "secondary_font_prompt" in data:
            doc["secondary_font_prompt"] = data["secondary_font_prompt"]

        # Custom font fields (Typography System) — per-slot gallery: primary and
        # secondary each independently accumulate uploaded fonts, plus which one (if
        # any) is currently selected. A slot is EITHER a library font (primary_font/
        # secondary_font above) OR a selected custom font, never both — the frontend
        # already clears the other side when either is picked, but enforce it here
        # too so a stale value from an older save (or a client bug) can never leave
        # both set at once and get read ambiguously downstream.
        for slot in ("primary", "secondary"):
            for suffix in ("custom_fonts", "custom_font_selected_url"):
                key = f"{slot}_{suffix}"
                if key in data:
                    doc[key] = data[key]
            if data.get(f"{slot}_custom_font_selected_url"):
                doc[f"{slot}_font"] = ""
            elif data.get(f"{slot}_font"):
                doc[f"{slot}_custom_font_selected_url"] = ""

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
        if "cta_rotation_index" in data:
            doc["cta_rotation_index"] = data["cta_rotation_index"]

        # Per-platform visual style overrides — additive, optional. A platform
        # with no entry here (or an empty list) falls back to the brand-wide
        # fields above unchanged; only present so a user CAN give a platform
        # its own style/guide pool and rotation, never required to. Keyed by
        # the same platform strings _generate_image_bg already receives
        # (instagram/facebook/linkedin/tiktok/x/...).
        if "style_selections_by_platform" in data:
            doc["style_selections_by_platform"] = data["style_selections_by_platform"]
        if "selected_custom_guides_by_platform" in data:
            doc["selected_custom_guides_by_platform"] = data["selected_custom_guides_by_platform"]
        if "selected_custom_guides_v2_by_platform" in data:
            doc["selected_custom_guides_v2_by_platform"] = data["selected_custom_guides_v2_by_platform"]
        if "style_rotation_index_by_platform" in data:
            doc["style_rotation_index_by_platform"] = data["style_rotation_index_by_platform"]

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
            print(f"🖼️  SAVE DEBUG brand={brand_id or user_id}: saving logo_position={repr(doc.get('logo_position'))}, logo_size={repr(doc.get('logo_size'))}")
            await db[BrandProfileService.COLLECTION].update_one(
                scope, {"$set": doc}
            )
        else:
            doc["created_at"] = now
            await db[BrandProfileService.COLLECTION].insert_one(doc)

        result = await db[BrandProfileService.COLLECTION].find_one(scope)
        if result:
            result.pop("_id", None)
        return UriResponse.get_single_data_response("brand_profile", result)

    @staticmethod
    async def get(user_id: str, db: AsyncIOMotorDatabase, brand_id: Optional[str] = None) -> Dict[str, Any]:
        from app.models.brand_account import BrandAccount
        personal_bid = BrandAccount.personal_brand_id(user_id)
        is_agency_brand = brand_id and brand_id != personal_bid

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
                "tagline", "region", "font_preference", "logo_url", "logo_position", "logo_size",
                "target_audience", "ideal_customer_profile",
                # Business Details — evergreen brand facts, same reasoning as tagline/region above
                "price_range", "unique_selling_proposition", "business_stage", "business_priorities",
                # Target Customer Detail — evergreen persona, same reasoning as ideal_customer_profile
                "customer_gender", "customer_location", "customer_occupation", "customer_income_level",
                "customer_interests", "customer_pain_points", "customer_needs", "customer_objections",
                "why_customers_choose_us",
                # Deliberately NOT business_pulse — one brand's active promotion is
                # noise for a sibling agency brand, unlike the evergreen facts above.
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
            # Business Details
            "price_range":          profile.get("price_range", ""),
            "unique_selling_proposition": profile.get("unique_selling_proposition", ""),
            "business_stage":       profile.get("business_stage", ""),
            "business_priorities":  profile.get("business_priorities") or [],
            "logo_url":             profile.get("logo_url"),
            "logo_position":        profile.get("logo_position", "bottom_right"),
            "logo_size":            profile.get("logo_size", "small"),
            "sample_template_urls": [u for u in (profile.get("sample_template_urls") or []) if u],
            "brand_colors":         profile.get("brand_colors") or [],
            "brand_voice":          brand_voice,
            "voice_sample":         profile.get("voice_sample", ""),
            "platform_tones":       profile.get("platform_tones") or {},
            "same_tone_everywhere": profile.get("same_tone_everywhere", True),
            "target_audience":      target_audience,
            "ideal_customer_profile": profile.get("ideal_customer_profile", ""),
            # Target Customer Detail — own distinct keys, additive to target_audience/
            # ideal_customer_profile above (target_audience itself is a synthesized
            # string, not a passthrough of the raw stored field — these are not).
            "customer_gender":       profile.get("customer_gender", ""),
            "customer_location":     profile.get("customer_location", ""),
            "customer_occupation":   profile.get("customer_occupation", ""),
            "customer_income_level": profile.get("customer_income_level", ""),
            "customer_interests":    profile.get("customer_interests") or [],
            "customer_pain_points":  profile.get("customer_pain_points") or [],
            "customer_needs":        profile.get("customer_needs") or [],
            "customer_objections":   profile.get("customer_objections") or [],
            "why_customers_choose_us": profile.get("why_customers_choose_us", ""),
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
            "approval_workflow":    profile.get("approval_workflow", ""),
            "style_selections":     profile.get("style_selections") or [],
            "style_rotation_index": int(profile.get("style_rotation_index") or 0),
            "cta_rotation_index":   int(profile.get("cta_rotation_index") or 0),
            "font_style":           profile.get("font_style", ""),
            "font_style_prompt":    profile.get("font_style_prompt", ""),
            # Primary & Secondary Font fields
            "primary_font":         profile.get("primary_font", ""),
            "primary_font_prompt":  profile.get("primary_font_prompt", ""),
            "secondary_font":       profile.get("secondary_font", ""),
            "secondary_font_prompt": profile.get("secondary_font_prompt", ""),
            # Custom font fields (Typography System) — per-slot gallery, see save-side note above
            "primary_custom_fonts":               profile.get("primary_custom_fonts") or [],
            "primary_custom_font_selected_url":   profile.get("primary_custom_font_selected_url", ""),
            "secondary_custom_fonts":              profile.get("secondary_custom_fonts") or [],
            "secondary_custom_font_selected_url": profile.get("secondary_custom_font_selected_url", ""),
            # Caption Voice System fields
            "voice_profile":        profile.get("voice_profile") or {},
            "voice_sample_analysis": profile.get("voice_sample_analysis") or {},
            # Business Pulse — raw passthrough only, no freshness math here (this is
            # a pure profile→context transform; freshness is computed by whichever
            # caller actually cares about recency, e.g. the calendar service).
            "business_pulse":            profile.get("business_pulse") or {},
            "business_pulse_updated_at": profile.get("business_pulse_updated_at"),
        }

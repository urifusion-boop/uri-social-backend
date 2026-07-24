# app/agents/social_media_manager/services/custom_visual_guide_v2_service.py

"""
Custom Visual Guide V2 Service
Advanced style transfer system using meta-prompts and direct image references.

Key Differences from V1:
- V1: Extracts aesthetic profile → Text prompt fragment → GPT-Image-2 generates
- V2: Extracts style profile JSON → Meta-prompt → GPT-4o generates smart prompt
      → Sends prompt + ACTUAL reference image → GPT-Image-2 edit mode

V2 Flow:
1. Upload: GPT-4o Vision extracts comprehensive style profile JSON
2. Generation: Art director meta-prompt transforms style + brand + content
3. GPT-Image-2 edit mode uses reference image + generated prompt
"""

from typing import Dict, Any, List, Optional
import asyncio
import hashlib
import json
import random
from datetime import datetime
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo.errors import DuplicateKeyError
from fastapi import HTTPException

from app.services.AIService import AIService


class CustomVisualGuideV2Service:
    """Service for Custom Visual Guide V2 - Style transfer with meta-prompts"""

    # Style profile extraction prompt for GPT-4o Vision
    STYLE_EXTRACTION_PROMPT = """You are an expert visual analyst. Analyze this reference design image and extract a comprehensive STYLE PROFILE that can be used to recreate the AESTHETIC and STRUCTURE (but NOT the specific identity/branding).

Your job is to document HOW this design looks, feels, and is structured — so another designer can apply the same STYLE to completely different content for a different brand.

Return ONLY this JSON structure (no markdown, no code blocks, just raw JSON):

{
  "medium": "photographic (real photo) | illustrated (clean vector/digital illustration — smooth shapes, precise even linework, no wobble) | hand_drawn (sketchy doodle style — visible pen/marker linework, loose imperfect lines, hand-sketched wobble/texture, looks drawn by hand not built in vector software) | flat_graphic (solid flat color shapes, minimal/no linework) | 3d_render | mixed_media | collage | typography_driven — look closely at the linework quality before choosing between illustrated and hand_drawn, they are commonly confused",
  "aesthetic_dominance": "low | medium | high",
  "overall_aesthetic": "minimalist | bold | vintage | modern | organic | industrial | playful | elegant | dramatic | warm | energetic | luxurious | casual | professional | artistic",
  "mood": "professional | playful | dramatic | warm | energetic | luxurious | calm | serious | friendly | sophisticated | edgy | nostalgic | futuristic | authentic | aspirational",

  "layout_structure": {
    "composition": "centered | rule_of_thirds | asymmetric | grid | diagonal | circular | layered | split_screen | full_bleed | framed",
    "information_density": "minimal | moderate | packed | complex",
    "focal_strategy": "single_hero | split | layered | scattered | hierarchical | balanced",
    "structural_devices": ["thought_bubble", "split_zones", "overlay_panel", "frame", "badge", "ribbon", "callout_box", "text_panel", "geometric_shapes", "organic_shapes"]
  },

  "imagery_style": {
    "subject_type": "person | product | scene | abstract | food | nature | urban | interior | pattern | text_only",
    "lighting": "soft_natural | dramatic | flat | golden_hour | studio | low_key | high_key | neon | backlit | side_lit | diffused | harsh",
    "treatment": "realistic | stylized | illustrated | flat | hand_drawn | photographic | painterly | geometric | textured",
    "realism_level": "photorealistic | semi_realistic | stylized | abstract | iconic | simplified"
  },

  "color_system": {
    "dominant_color": "#hexcode (or 'transparent' if no clear dominant)",
    "accent_strategy": "single_bright_accent | complementary_pair | monochrome | vibrant_multi | pastel_palette | dark_moody | gradient_driven | neon_pops | earth_tones | analogous",
    "palette_role": "background_dominant | balanced | accent_driven | imagery_dominant",
    "temperature": "warm | cool | neutral | mixed",
    "saturation": "highly_saturated | moderate | desaturated | black_and_white",
    "contrast": "high | medium | low"
  },

  "graphic_elements": [
    "line_icons", "badges", "frames", "textures", "geometric_shapes", "organic_elements",
    "patterns", "gradients", "shadows", "borders", "dividers", "decorative_flourishes",
    "thought_bubbles", "speech_bubbles", "arrows", "stars", "sparkles", "dots", "lines"
  ],

  "typography": {
    "character": "bold_condensed | elegant_serif | playful_rounded | modern_sans | script_handwritten | geometric | retro | futuristic | minimalist | ornate | industrial | organic",
    "hierarchy": "strong | subtle | flat | extreme",
    "text_placement": "overlay_center | side_panel | top_banner | bottom_strip | scattered | diagonal | vertical | circular | integrated_in_imagery",
    "text_treatment": "plain | outlined | shadowed | glowing | textured | gradient_fill | cutout | 3d_effect | handwritten_style"
  },

  "what_to_leave_behind": [
    "List ALL identity elements that belong to the ORIGINAL brand/creator:",
    "- Original brand name (if visible)",
    "- Original logo or brand marks",
    "- Phone numbers, websites, social handles",
    "- Specific person's face/identity (describe as type instead)",
    "- Specific product packaging (describe as product type instead)",
    "- Any copyrighted characters, trademarks, or branded elements"
  ]
}

CRITICAL RULES:
1. Focus on STYLE (how it looks) not IDENTITY (whose brand it is)
2. Be specific about colors (use hex codes where possible)
3. Identify ALL text placement patterns and graphic devices
4. List everything in "what_to_leave_behind" that should NOT be copied
5. The goal is to capture the AESTHETIC DNA so it can be applied to new content
6. Return ONLY the JSON, no explanations"""

    # Art director meta-prompt template
    ART_DIRECTOR_TEMPLATE = """You are a senior art director creating a brand-new, original graphic
for a specific brand. You have been given a STYLE PROFILE extracted
from a reference design, the TARGET BRAND's own identity, and the
CONTENT for the new graphic. Your job is to produce a single, detailed
image-generation prompt that applies the borrowed STYLE to the new
CONTENT, branded entirely for the TARGET BRAND.

═══════════════════════════════════════════════════════
INPUTS
═══════════════════════════════════════════════════════

STYLE PROFILE (extracted from the reference design):
{style_profile_json}

TARGET BRAND IDENTITY (the brand this graphic is FOR):
{{
  "brand_name": "{brand_name}",
  "primary_color": "{primary_color}",
  "secondary_color": "{secondary_color}",
  "accent_color": "{accent_color}",
  "logo_description": "{logo_description}",
  "font_preference": "{brand_font}",
  "tone": "{brand_tone}",
  "contact_handle": "{brand_handle}"
}}

NEW CONTENT FOR THIS GRAPHIC:
{{
  "purpose": "{purpose}",
  "headline": "{headline}",
  "subtext": "{subtext}",
  "call_to_action": "{cta}",
  "format": "{format}"
}}

═══════════════════════════════════════════════════════
HARD RULES — IDENTITY SEPARATION (NON-NEGOTIABLE)
═══════════════════════════════════════════════════════

1. BORROW THE STYLE, NOT THE IDENTITY.
   You are reproducing the reference's AESTHETIC and STRUCTURE only.
   You must NOT reproduce any identity element belonging to the
   reference's original creator. The style profile's
   "what_to_leave_behind" array lists detected identity elements —
   none of these may appear in the output.

2. THE ONLY BRANDING IS THE TARGET BRAND'S.
   The output carries ONLY {brand_name}'s identity:
   - Only {brand_name}'s logo (described, to be composited)
   - Only {brand_name}'s name and handle
   - Only {brand_name}'s colors
   - No other brand's name, logo, mark, tagline, product, watermark,
     or contact details may appear anywhere.

3. COLORS MAP BY ROLE.
   Take the reference's color STRATEGY (from color_system.accent_strategy
   and palette_role) and apply it using the TARGET BRAND's colors.
   Example: if the reference uses "a single bright accent on key words
   against a dark background," reproduce that STRATEGY but with
   {brand_name}'s accent color, not the reference's original color.

4. PEOPLE AND PRODUCTS ARE GENERIC TYPES, NOT COPIES.
   If the reference shows a specific person or product, reproduce only
   the TYPE (e.g. "a candid everyday Nigerian woman in a market
   setting") — never a likeness of the specific individual or the
   specific product from the reference.

5. NO TEXT BAKED INTO COMPLEX AREAS.
   Generate the visual with clean space reserved for text overlay.
   Text will be composited as editable layers afterward. Describe where
   the text zones should be, but design the image so those zones are
   uncluttered and overlay-ready.

═══════════════════════════════════════════════════════
HOW TO BUILD THE PROMPT
═══════════════════════════════════════════════════════

Construct the image-generation prompt in this layered order, drawing
the STYLE from the profile and the IDENTITY from the target brand:

A. MEDIUM FIRST - CRITICAL. You MUST match the exact medium from the
   style profile. Open the prompt by stating the medium EMPHATICALLY.

   Examples:
   - If medium is "hand_drawn": "Hand-drawn doodle illustration, sketchy
     linework, casual marker style, NOT photographic, NOT polished"
   - If medium is "illustrated": "Illustrated graphic design, vector-style
     artwork, NOT photographic, NOT realistic"
   - If medium is "flat_graphic": "Flat graphic design, minimalist vector
     style, simple shapes, NOT photographic, NOT 3D"
   - If medium is "3d_render": "3D rendered CGI, polished digital modeling"
   - If medium is "photographic": "Photographic image, realistic photography"

   ALWAYS add "NOT photographic" if the reference is NOT photographic.
   The medium is NON-NEGOTIABLE - match it exactly.

B. OVERALL AESTHETIC. Translate overall_aesthetic and mood into
   opening descriptive language that sets the visual register.
   CRITICAL: mood is NON-NEGOTIABLE, exactly like the medium in step A —
   it must NOT drift toward whatever tone the new content's subject matter
   typically implies. A "serious"/"professional" mood must stay serious and
   professional even if the new content is about something inherently
   cheerful; a "playful" mood must stay playful even for serious subject
   matter. The new CONTENT changes WHAT is shown; the style profile's mood
   governs HOW it feels — never let the former override the latter.

C. COMPOSITION & STRUCTURE. Apply layout_structure — the composition,
   information density, focal strategy, and structural_devices.
   Reserve the text zones as clean overlay-ready space.

D. IMAGERY. Apply imagery_style as a generic type, populated with the
   new content's subject. CRITICAL: Match the exact "treatment" and
   "realism_level" from imagery_style. If the reference is "hand_drawn"
   treatment with "stylized" realism, DO NOT make it photographic or
   realistic. If it's "flat" and "simplified", DO NOT add depth or
   realism. Preserve the exact visual execution style.

E. COLOR. Apply the reference's color STRATEGY using the target brand's
   palette. State the dominant color, the accent strategy, and where
   the accent lands — all in the brand's colors.

F. GRAPHIC ELEMENTS. Reproduce the reference's reusable graphic_elements
   generically, in the brand's colors. Never reproduce the reference's
   logo or identity marks.

G. TYPOGRAPHY ZONES. Note the typographic character so the composited
   text will match the borrowed style, and indicate where headline /
   subtext / CTA zones sit. Keep these areas clean.

H. NEGATIVE CONSTRAINTS. Explicitly exclude: the reference's logo,
   brand name, contact details, watermarks, specific person/product;
   any other brand's identity; garbled or baked-in text in the overlay
   zones; visual clutter in reserved text areas.

   CRITICAL: If the reference medium is NOT "photographic", you MUST
   explicitly forbid photorealistic rendering. Add negative constraints
   like "NOT photographic", "NOT realistic", "NOT polished 3D render"
   to prevent DALL-E from defaulting to its photorealistic bias.

═══════════════════════════════════════════════════════
OUTPUT
═══════════════════════════════════════════════════════

Return ONLY this JSON (no markdown, no code blocks):
{{
  "image_prompt": "<the full, detailed, layered image-generation prompt following A–H above>",
  "reserved_text_zones": [
    {{"zone": "headline", "position": "<where>", "text": "{headline}", "style_note": "<typographic character>"}},
    {{"zone": "subtext", "position": "<where>", "text": "{subtext}", "style_note": "<...>"}},
    {{"zone": "cta", "position": "<where>", "text": "{cta}", "style_note": "<...>"}}
  ],
  "brand_overlay": {{
    "logo": "composite {brand_name} logo at <position>",
    "handle": "composite {brand_handle} at <position>",
    "colors_used": ["<which brand colors and where>"]
  }},
  "identity_safety_check": {{
    "reference_identity_excluded": ["<items from what_to_leave_behind>"],
    "only_target_brand_present": true,
    "medium_preserved": "<the medium carried from reference>",
    "aesthetic_dominance_applied": "<low|medium|high>"
  }}
}}

Return only the JSON. No preamble, no explanation."""

    # Composition variation directives for V2 generation. Without one of these,
    # GPT-Image-2's edit call reproduces the reference's exact framing/pose
    # every time (since it's editing the same input photo with a similar
    # prompt each generation) — every post from a guide looked like the same
    # shot with different text. One is picked at random per generation so
    # repeated posts from the same guide stay in the same style family without
    # being visually identical.
    VARIATION_DIRECTIVES = [
        "Use a different angle or framing than a plain straight-on centered shot — try a subtle three-quarter view or a slightly lower/higher camera angle.",
        "Vary the pose and placement of the main subject within the frame rather than dead-center — shift it left, right, or change its orientation.",
        "Vary the specific arrangement of the background and decorative details this time — same style family, not the identical layout.",
        "Vary the framing and crop — zoom in slightly tighter or pull back slightly wider than a default centered shot.",
        "Vary the specific action or moment being depicted, within the same subject type and mood — a different instant, not the identical pose.",
    ]

    @staticmethod
    async def _compute_image_hash(image_url: str) -> str:
        """Compute hash of image for duplicate detection"""
        import httpx
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(image_url)
            return hashlib.sha256(response.content).hexdigest()

    @staticmethod
    async def extract_style_profile(image_url: str) -> Dict[str, Any]:
        """
        Extract comprehensive style profile from reference image using GPT-4o Vision.

        Args:
            image_url: URL of uploaded reference image

        Returns:
            Style profile JSON matching the schema in STYLE_EXTRACTION_PROMPT
        """
        try:
            print(f"[V2] Extracting style profile from: {image_url[:80]}...")

            # Call OpenAI directly for vision (bypass ChatModel validation)
            import asyncio
            from app.services.AIService import client

            loop = asyncio.get_running_loop()
            ai_response = await loop.run_in_executor(
                None,
                lambda: client.chat.completions.create(
                    model="gpt-4o",
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": image_url}},
                            {"type": "text", "text": CustomVisualGuideV2Service.STYLE_EXTRACTION_PROMPT}
                        ]
                    }],
                    temperature=0.3,
                )
            )

            if isinstance(ai_response, dict) and "error" in ai_response:
                raise Exception(ai_response["error"])

            raw_content = ai_response.choices[0].message.content.strip()

            # Remove markdown code blocks if present
            if raw_content.startswith("```"):
                lines = raw_content.split("\n")
                json_lines = []
                in_code_block = False
                for line in lines:
                    if line.startswith("```"):
                        in_code_block = not in_code_block
                        continue
                    if in_code_block or (not line.startswith("```") and "{" in line):
                        json_lines.append(line)
                raw_content = "\n".join(json_lines)

            # Parse JSON
            style_profile = json.loads(raw_content)

            print(f"[V2] ✅ Style profile extracted:")
            print(f"     Medium: {style_profile.get('medium')}")
            print(f"     Aesthetic: {style_profile.get('overall_aesthetic')}")
            print(f"     Mood: {style_profile.get('mood')}")
            print(f"     Identity items to exclude: {len(style_profile.get('what_to_leave_behind', []))}")

            return style_profile

        except json.JSONDecodeError as e:
            print(f"[V2] ❌ Failed to parse style profile JSON: {e}")
            print(f"[V2] Raw content: {raw_content[:500]}")
            raise HTTPException(
                status_code=500,
                detail="Failed to extract style profile from image. Please try a different image."
            )
        except Exception as e:
            print(f"[V2] ❌ Style extraction error: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Style extraction failed: {str(e)}"
            )

    @staticmethod
    async def process_reference_image_v2(
        image_url: str,
        user_id: str,
        brand_id: Optional[str],
        name: str,
        db: AsyncIOMotorDatabase,
    ) -> Dict[str, Any]:
        """
        Process reference image for Custom Visual Guide V2.

        Unlike V1 (which extracts aesthetic profile and creates prompt fragment),
        V2 extracts a comprehensive style profile JSON for meta-prompt generation.

        Args:
            image_url: Cloudinary URL of uploaded reference image
            user_id: User who uploaded
            brand_id: Brand (optional)
            name: Guide name
            db: Database connection

        Returns:
            Custom visual guide V2 document
        """
        print(f"[V2] Starting Custom Visual Guide V2 processing: {name}")

        try:
            # Step 1: Compute image hash for duplicate detection
            image_hash = await CustomVisualGuideV2Service._compute_image_hash(image_url)
            print(f"[V2] Image hash: {image_hash}")

            # Step 2: Check if this image already exists
            existing_guide = await db["custom_visual_guides"].find_one({
                "user_id": user_id,
                "original_image_hash": image_hash,
                "version": "v2"
            })

            if existing_guide:
                if existing_guide.get("status") == "archived":
                    # Restore archived guide to active
                    print(f"[V2] 📦 Restoring archived guide to active: {existing_guide['_id']}")
                    await db["custom_visual_guides"].update_one(
                        {"_id": existing_guide["_id"]},
                        {
                            "$set": {
                                "status": "active",
                                "name": name,  # Update name if changed
                                "updated_at": datetime.utcnow()
                            }
                        }
                    )
                    existing_guide["id"] = str(existing_guide["_id"])
                    existing_guide["status"] = "active"
                    existing_guide["name"] = name
                    print(f"[V2] ✅ Guide restored to active: {existing_guide['id']}")
                    return existing_guide
                else:
                    # Already active - show error
                    print(f"[V2] ❌ Duplicate active guide detected: {image_hash}")
                    raise HTTPException(
                        status_code=409,
                        detail="You've already uploaded this image. Find it in your V2 Style Guides."
                    )

            # Step 3: Extract style profile using GPT-4o Vision (only for new uploads)
            print(f"[V2] Extracting style profile with GPT-4o Vision...")
            style_profile = await CustomVisualGuideV2Service.extract_style_profile(image_url)

            # Step 4: Store in database
            guide_doc = {
                "user_id": user_id,
                "brand_id": brand_id,
                "name": name,
                "version": "v2",  # Distinguish from v1
                "original_image_url": image_url,
                "original_image_hash": image_hash,
                "uploaded_at": datetime.utcnow(),

                # V2-specific: comprehensive style profile
                "style_profile": style_profile,

                "times_used": 0,
                "status": "active",
                "updated_at": datetime.utcnow(),
            }

            result = await db["custom_visual_guides"].insert_one(guide_doc)
            guide_doc["id"] = str(result.inserted_id)
            guide_doc["_id"] = result.inserted_id

            print(f"[V2] ✅ Custom Visual Guide V2 created: {guide_doc['id']}")
            return guide_doc

        except HTTPException:
            raise
        except Exception as e:
            print(f"[V2] ❌ Error processing reference image: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"Failed to process reference image: {str(e)}"
            )

    @staticmethod
    async def reanalyze_style_profile(
        guide_id: str,
        user_id: str,
        db: AsyncIOMotorDatabase,
    ) -> Dict[str, Any]:
        """
        Re-run GPT-4o Vision extraction on an existing guide's already-uploaded
        reference image, replacing its stored style_profile in place.

        Exists because the upload endpoint's duplicate-detection (by image
        hash) means re-uploading the same file never re-triggers extraction —
        it either 409s (already active) or silently restores the old guide
        with its old profile. Without this, a guide analyzed before an
        extraction-prompt improvement is permanently stuck on the old,
        possibly-misclassified profile; there was no way to benefit from a
        better prompt without deleting the guide and sourcing a new image.

        Returns the updated guide document.
        """
        from bson import ObjectId

        guide = await db["custom_visual_guides"].find_one({
            "_id": ObjectId(guide_id),
            "user_id": user_id,
            "version": "v2",
        })
        if not guide:
            raise HTTPException(status_code=404, detail="Custom Visual Guide V2 not found")

        print(f"[V2] Re-analyzing guide {guide_id} ({guide.get('name')})")
        old_medium = (guide.get("style_profile") or {}).get("medium")

        new_profile = await CustomVisualGuideV2Service.extract_style_profile(guide["original_image_url"])

        await db["custom_visual_guides"].update_one(
            {"_id": ObjectId(guide_id)},
            {"$set": {"style_profile": new_profile, "updated_at": datetime.utcnow()}},
        )
        print(f"[V2] ✅ Re-analyzed guide {guide_id}: medium {old_medium!r} → {new_profile.get('medium')!r}")

        guide["style_profile"] = new_profile
        guide["id"] = str(guide["_id"])
        return guide

    @staticmethod
    async def generate_image_with_v2_guide(
        guide_id: str,
        brand_context: Dict[str, Any],
        seed_content: str,
        headline: str,
        subtext: str,
        cta: str,
        platform: str,
        db: AsyncIOMotorDatabase,
        override_reference_image: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Generate image using Custom Visual Guide V2 + meta-prompt.

        Flow:
        1. Load guide (style_profile + reference_image_url)
        2. Build art director meta-prompt
        3. GPT-4o generates final image prompt
        4. Send prompt + reference image → GPT-Image-2 edit mode

        Args:
            guide_id: Custom guide V2 ID
            brand_context: Brand profile data
            seed_content: User's content request
            headline: Main text
            subtext: Supporting text
            cta: Call to action
            platform: Target platform
            db: Database connection
            override_reference_image: when a user attaches their OWN photo to
                a post that also has this guide selected, pass that photo
                here (after the normal background-removal cleanup) to use it
                as the edit base instead of the guide's own stored reference
                photo — the guide's style still applies exactly as it does
                for a guide-only generation, it's just applied to this photo
                instead. None (default) uses the guide's own photo, unchanged
                from before.

        Returns:
            {
                "success": bool,
                "image_url": str,
                "image_prompt": str,
                "reserved_text_zones": list,
                "brand_overlay": dict
            }
        """
        from bson import ObjectId
        from app.agents.social_media_manager.services.image_content_service import ImageContentService

        try:
            print(f"\n{'='*70}")
            print(f"CUSTOM VISUAL GUIDE V2 GENERATION")
            print(f"{'='*70}")

            # Step 1: Load guide from database
            guide = await db["custom_visual_guides"].find_one({
                "_id": ObjectId(guide_id),
                "version": "v2"
            })

            if not guide:
                raise HTTPException(status_code=404, detail="Custom Visual Guide V2 not found")

            style_profile = guide.get("style_profile")
            reference_image_url = override_reference_image or guide.get("original_image_url")
            if override_reference_image:
                print(f"[V2] Using caller-supplied reference image (guide's own photo not used this time)")

            print(f"[V2] Loaded guide: {guide.get('name')}")
            print(f"[V2] Style: {style_profile.get('overall_aesthetic')}")

            # Step 2: Build structured style description from extracted profile
            # Extract key attributes for precise style matching

            medium = style_profile.get("medium", "photographic")
            aesthetic = style_profile.get("overall_aesthetic", "modern")
            mood = style_profile.get("mood", "professional")

            # Layout structure
            layout = style_profile.get("layout_structure", {})
            composition = layout.get("composition", "centered")

            # Color system
            color_system = style_profile.get("color_system", {})
            accent_strategy = color_system.get("accent_strategy", "")

            # Graphic elements (decorative details)
            graphic_elements = style_profile.get("graphic_elements", [])
            decorative_elements = ", ".join(graphic_elements[:4]) if graphic_elements else "none"

            # Typography
            typography = style_profile.get("typography", {})
            text_placement = typography.get("text_placement", "overlay_center")
            text_treatment = typography.get("text_treatment", "plain")

            # Brand colors — the ACTUAL colors, not just the reference's abstract
            # color strategy label (e.g. "single_bright_accent"). Without this,
            # the prompt never named a real color, so GPT-Image-2 had nothing to
            # go on but the reference's own original colors, which is why V2
            # renders came out ignoring the brand's actual palette.
            brand_name = brand_context.get("brand_name") or "the brand"
            brand_colors = brand_context.get("brand_colors") or []
            if brand_colors:
                color_list = ", ".join(brand_colors[:3])
                color_instruction = (
                    f"Use {brand_name}'s actual brand colors ({color_list}) — apply them "
                    f"following the reference's color strategy ({accent_strategy or 'its accent placement'}), "
                    f"but the colors themselves must be {brand_name}'s, not the reference's original colors."
                )
            else:
                color_instruction = "match reference palette"

            # Composition variation — without this, GPT-Image-2's edit call
            # reproduces the reference's exact framing/pose every time (it's
            # editing the same input photo with a similar prompt each
            # generation), so every post from a guide looked like the same shot
            # with different text pasted on. Picking one directive at random per
            # generation keeps the style family intact while breaking the
            # sameness.
            if override_reference_image:
                # Using the caller's OWN uploaded photo as the edit base — the
                # whole point is to keep ITS actual subject/content and just
                # restyle how it's rendered (medium, color, mood) to match the
                # guide. Varying the pose/composition or swapping in a
                # different person/product (both needed when editing the
                # GUIDE's own photo, to stop it reappearing verbatim across
                # every generation) would work against that here.
                variation_directive = ""
                identity_instruction = ""
                match_instruction = (
                    "Apply this visual style to the uploaded image's actual subject and "
                    "composition — keep what is being shown, restyle HOW it's rendered "
                    "(medium, color treatment, mood) to match the style described above."
                )
            else:
                variation_directive = random.choice(CustomVisualGuideV2Service.VARIATION_DIRECTIVES)

                # Identity instruction — the reference photo often shows one specific
                # person or product. Without calling that out explicitly, the same
                # face/pose/outfit or the same product packaging kept reappearing in
                # every generation, since the model is editing that literal photo.
                imagery_style = style_profile.get("imagery_style", {})
                subject_type = imagery_style.get("subject_type", "")
                if subject_type == "person":
                    identity_instruction = "The reference features a specific person — depict a DIFFERENT person (different face, pose, and outfit) of the same general type and mood. Never reproduce the same individual."
                elif subject_type == "product":
                    identity_instruction = "The reference features a specific product — depict a DIFFERENT specific product of the same general category. Never reproduce the same packaging, label, or exact product shape."
                else:
                    identity_instruction = ""

                match_instruction = (
                    f"Match the reference image's medium, mood, and overall composition family closely — "
                    f"but this is a NEW piece, not a duplicate of the reference. {variation_directive}"
                )

            # Build structured prompt similar to standard generation
            # Use sections to separate style instructions from content (prevents verbatim rendering)

            style_instructions = f"""=== VISUAL STYLE (MATCH REFERENCE) ===
Design style: {medium}, {aesthetic} aesthetic
Composition: {composition}
Decorative elements: {decorative_elements if decorative_elements != "none" else "minimal"}
Color approach: {color_instruction}
Typography: {text_placement} placement, {text_treatment} style

{match_instruction}
{identity_instruction}
Include MINIMAL text - just a short headline and small CTA.
Do NOT copy logos or brand names from the reference."""

            content_section = f"""=== CONTENT ===
{seed_content.strip()}"""

            # CTA instruction (same format as regular generation)
            cta_instruction = f"""=== CALL-TO-ACTION ===
Display the following CTA text at the bottom of the image in small clean sans-serif text: "{cta}"
Style it subtle but legible, approximately 30% the size of the headline.
Position: bottom-centre or bottom-right within safe zone.
Do NOT style it as a button or banner."""

            final_prompt = f"""{style_instructions}

{content_section}

{cta_instruction}"""

            print(f"[V2] ✅ Pure style cloning prompt generated ({len(final_prompt)} chars)")
            print(f"[V2] Preview: {final_prompt[:200]}...")

            # Step 4: Get platform-specific dimensions (same as other visual guides)
            # Uses platform defaults: Instagram/Facebook = 1080x1350 portrait, LinkedIn/Twitter = landscape
            print(f"[V2] Getting platform specs for {platform}...")
            import httpx
            from PIL import Image
            import io

            specs = ImageContentService._get_platform_image_specs(platform)  # Let it choose platform default
            image_width = specs.get("width", 1200)
            image_height = specs.get("height", 630)
            print(f"[V2] Platform dimensions: {image_width}x{image_height} ({specs.get('format', 'unknown')})")

            # Generate with platform-specific dimensions + reference image for style.
            # NOTE: gpt-image-2 rejects input_fidelity outright (400 error — that
            # param only exists for gpt-image-1), so fidelity/variation for V2
            # guides is steered entirely through the prompt text above
            # (identity_instruction + variation_directive), not an API parameter.
            image_response = await ImageContentService._call_dalle_api(
                prompt=final_prompt,
                size=f"{image_width}x{image_height}",
                reference_image=reference_image_url,  # ← ACTUAL reference image for style
                image_model="openai/gpt-image-2",
            )

            if not image_response.get('success'):
                raise Exception(image_response.get('error', 'Image generation failed'))

            generated_image_url = image_response['url']
            print(f"[V2] ✅ Image generated successfully: {generated_image_url[:80]}...")

            # Step 5: Overlay logo on generated image
            logo_url = brand_context.get("logo_url")
            logo_position = brand_context.get("logo_position", "bottom_right")

            if logo_url:
                print(f"[V2] Overlaying logo at position: {logo_position}")

                # Handle data URLs (base64) vs regular URLs
                import base64
                import re

                if generated_image_url.startswith("data:"):
                    # Extract base64 from data URL
                    match = re.match(r'data:image/\w+;base64,(.+)', generated_image_url)
                    if match:
                        base64_image = match.group(1)
                        print(f"[V2] Extracted base64 from data URL")
                    else:
                        raise Exception("Invalid data URL format")
                else:
                    # Download from regular URL and convert to base64
                    async with httpx.AsyncClient(timeout=20) as client:
                        img_response = await client.get(generated_image_url)
                        base64_image = base64.b64encode(img_response.content).decode('utf-8')
                        print(f"[V2] Downloaded image from URL and converted to base64")

                # Overlay logo using ImageContentService (expects base64 string)
                logo_result_b64 = ImageContentService._overlay_logo(
                    b64=base64_image,
                    logo_url=logo_url,
                    position=logo_position,
                )

                # Upload final image with logo to Cloudinary
                import cloudinary.uploader

                # Decode base64 back to bytes for upload
                final_image_bytes = base64.b64decode(logo_result_b64)

                upload_result = cloudinary.uploader.upload(
                    final_image_bytes,
                    folder="uri-social/generated-images",
                    resource_type="image",
                )
                final_image_url = upload_result['secure_url']
                print(f"[V2] ✅ Logo overlaid and uploaded: {final_image_url[:80]}...")
            else:
                final_image_url = generated_image_url
                print(f"[V2] ⚠️ No logo URL provided, skipping logo overlay")

            # Step 6: Update usage counter
            await db["custom_visual_guides"].update_one(
                {"_id": ObjectId(guide_id)},
                {
                    "$inc": {"times_used": 1},
                    "$set": {"last_used_at": datetime.utcnow()}
                }
            )

            return {
                "success": True,
                "status": True,  # For UriResponse compatibility
                "image_url": final_image_url,
                "responseData": {"image_url": final_image_url},  # For UriResponse compatibility
                "image_prompt": final_prompt,
                "style_profile_used": style_profile.get("overall_aesthetic"),
                "medium_used": medium,
                "mood_used": mood,
                "generated_dimensions": f"{image_width}x{image_height}",
                "platform": platform,
                "logo_applied": bool(logo_url),
            }

        except json.JSONDecodeError as e:
            print(f"[V2] ❌ Failed to parse art director output: {e}")
            raise HTTPException(
                status_code=500,
                detail="Failed to generate image prompt from meta-prompt"
            )
        except Exception as e:
            print(f"[V2] ❌ V2 generation error: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"V2 image generation failed: {str(e)}"
            )

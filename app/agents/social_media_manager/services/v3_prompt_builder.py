"""
V3 Prompt Builder - 10-Block Architecture
Based on URI-Social-Image-Generation-Master-Rulebook-V3.pdf

This is an ISOLATED implementation for testing V3 prompt system performance
against the existing image generation system. Does NOT modify production code.

10-Block Structure (Rulebook p.3-5):
  1. Product Core Definition
  2. Scene DNA
  3. Atmospheric Depth
  4. Motion Signature
  5. Micro-Realism Layer
  6. Typography Hierarchy
  7. Layout Geometry
  8. Cultural Context
  9. Brand Compliance
  10. Quality Control
"""

from typing import Dict, Any, Optional, List
from datetime import datetime


class V3PromptBuilder:
    """
    Builds image generation prompts using V3's 10-block architecture.
    Each block is constructed independently and assembled in priority order.
    """

    @staticmethod
    def build_complete_prompt(
        seed_content: str,
        brand_context: Dict[str, Any],
        platform: str = "instagram",
        style_slug: Optional[str] = None,
        reference_image: Optional[str] = None,
        product_spec: Optional[Dict[str, Any]] = None,
        slide_index: Optional[int] = None,
        total_slides: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Assemble complete V3 prompt from 10 blocks.

        Args:
            seed_content: User's content/request
            brand_context: Brand profile data
            platform: Target platform (instagram, linkedin, etc.)
            style_slug: Visual style identifier
            reference_image: Product image URL (if provided)
            product_spec: Forensic product analysis (if reference_image provided)
            slide_index: Carousel slide number (0-indexed)
            total_slides: Total carousel slides

        Returns:
            Dict with 'prompt', 'metadata', 'blocks_used'
        """

        # Extract brand variables
        brand_name = brand_context.get("brand_name", "")
        brand_colors = brand_context.get("brand_colors", [])
        industry = brand_context.get("industry", "general_other")
        region = brand_context.get("region", "")

        blocks = []
        metadata = {
            "architecture_version": "v3",
            "blocks_count": 10,
            "timestamp": datetime.utcnow().isoformat(),
            "has_product_reference": bool(reference_image),
            "style_slug": style_slug,
            "platform": platform,
        }

        # ========== BLOCK 1: PRODUCT CORE DEFINITION ==========
        if reference_image and product_spec:
            block_1 = V3PromptBuilder._build_product_core_block(product_spec, brand_name)
            blocks.append(("BLOCK 1: PRODUCT CORE", block_1))
        else:
            # No product reference - brand/concept is the "product"
            block_1 = V3PromptBuilder._build_concept_core_block(seed_content, brand_name, brand_colors)
            blocks.append(("BLOCK 1: CONCEPT CORE", block_1))

        # ========== BLOCK 2: SCENE DNA ==========
        # Get style dimensions from V3 style library
        from app.agents.social_media_manager.services.v3_style_library import get_style_dimensions

        style_dims = get_style_dimensions(style_slug, industry) if style_slug else None
        block_2 = V3PromptBuilder._build_scene_dna_block(
            style_dimensions=style_dims,
            industry=industry,
            seed_content=seed_content,
        )
        blocks.append(("BLOCK 2: SCENE DNA", block_2))

        # ========== BLOCK 3: ATMOSPHERIC DEPTH ==========
        block_3 = V3PromptBuilder._build_atmospheric_depth_block(style_dims, industry)
        blocks.append(("BLOCK 3: ATMOSPHERIC DEPTH", block_3))

        # ========== BLOCK 4: MOTION SIGNATURE ==========
        block_4 = V3PromptBuilder._build_motion_signature_block(industry, reference_image)
        blocks.append(("BLOCK 4: MOTION SIGNATURE", block_4))

        # ========== BLOCK 5: MICRO-REALISM LAYER ==========
        block_5 = V3PromptBuilder._build_micro_realism_block(industry, style_dims)
        blocks.append(("BLOCK 5: MICRO-REALISM", block_5))

        # ========== BLOCK 6: TYPOGRAPHY HIERARCHY ==========
        block_6 = V3PromptBuilder._build_typography_hierarchy_block(
            brand_context=brand_context,
            seed_content=seed_content,
            platform=platform,
            slide_index=slide_index,
            total_slides=total_slides,
        )
        blocks.append(("BLOCK 6: TYPOGRAPHY", block_6))

        # ========== BLOCK 7: LAYOUT GEOMETRY ==========
        block_7 = V3PromptBuilder._build_layout_geometry_block(
            platform=platform,
            has_product=bool(reference_image),
            style_dims=style_dims,
        )
        blocks.append(("BLOCK 7: LAYOUT GEOMETRY", block_7))

        # ========== BLOCK 8: CULTURAL CONTEXT ==========
        block_8 = V3PromptBuilder._build_cultural_context_block(region, industry, brand_context)
        blocks.append(("BLOCK 8: CULTURAL CONTEXT", block_8))

        # ========== BLOCK 9: BRAND COMPLIANCE ==========
        block_9 = V3PromptBuilder._build_brand_compliance_block(
            brand_name=brand_name,
            brand_colors=brand_colors,
            brand_context=brand_context,
            seed_content=seed_content,
        )
        blocks.append(("BLOCK 9: BRAND COMPLIANCE", block_9))

        # ========== BLOCK 10: QUALITY CONTROL ==========
        block_10 = V3PromptBuilder._build_quality_control_block(platform, industry)
        blocks.append(("BLOCK 10: QUALITY CONTROL", block_10))

        # ========== ASSEMBLE FINAL PROMPT ==========
        # Order matters: Critical blocks first (GPT-Image-2 weights beginning heavily)
        prompt_parts = []
        for block_name, block_content in blocks:
            if block_content:
                prompt_parts.append(f"=== {block_name} ===\n{block_content}")

        final_prompt = "\n\n".join(prompt_parts)

        # Validation
        if "undefined" in final_prompt or "null" in final_prompt:
            raise ValueError("V3 Prompt contains undefined/null values")

        if len(final_prompt) < 400:
            print(f"⚠️  V3 Warning: Prompt too short ({len(final_prompt)} chars)")

        metadata["prompt_length"] = len(final_prompt)
        metadata["blocks_used"] = [name for name, content in blocks if content]

        return {
            "prompt": final_prompt,
            "metadata": metadata,
            "blocks": {name: content for name, content in blocks if content},
        }

    # ========== BLOCK BUILDERS ==========

    @staticmethod
    def _build_product_core_block(product_spec: Dict[str, Any], brand_name: str) -> str:
        """
        BLOCK 1: Product Core Definition (Rulebook p.10-13)
        Forensic documentation of the reference product.
        """
        return f"""PRODUCT PRESERVATION DIRECTIVE:
This image MUST include the exact product shown in the reference image.

PRODUCT IDENTITY:
- Name: {product_spec.get('product_name', brand_name)}
- Category: {product_spec.get('category', 'Product')}
- Primary Colors: {', '.join(product_spec.get('colors', []))}

MANDATORY PRESERVATION RULES:
1. SHAPE: {product_spec.get('shape_description', 'Preserve exact silhouette and proportions')}
2. LABELS: {product_spec.get('label_description', 'All text and branding on product must be clearly visible')}
3. MATERIALS: {product_spec.get('material_description', 'Preserve surface texture and material properties')}
4. SCALE: Product must be {product_spec.get('relative_size', 'prominently sized')} within the frame

CRITICAL: The product itself is SACRED. Never distort, regenerate, or modify its appearance.
Everything AROUND the product is AI-generated professional styling."""

    @staticmethod
    def _build_concept_core_block(seed_content: str, brand_name: str, brand_colors: List[str]) -> str:
        """
        BLOCK 1 Alternative: When no product reference exists.
        """
        color_str = ", ".join(brand_colors[:3]) if brand_colors else "brand colors"
        return f"""CONCEPT CORE:
This image represents: {seed_content[:300]}

Brand: {brand_name}
Visual Identity: {color_str}

The image must visually communicate the concept above with clarity and impact."""

    @staticmethod
    def _build_scene_dna_block(
        style_dimensions: Optional[Dict[str, Any]],
        industry: str,
        seed_content: str,
    ) -> str:
        """
        BLOCK 2: Scene DNA (Rulebook p.6-9)
        Cinematography, lighting, color science from V3 style library.
        """
        if style_dimensions:
            # Use V3 style library's rich dimensional description
            cinematography = style_dimensions.get("cinematography", "")
            lighting = style_dimensions.get("lighting_physics", "")
            color_science = style_dimensions.get("color_science", "")

            return f"""CINEMATOGRAPHY:
{cinematography}

LIGHTING PHYSICS:
{lighting}

COLOR SCIENCE:
{color_science}"""
        else:
            # Fallback: Use aesthetic vocabulary based on industry
            from app.agents.social_media_manager.services.v3_aesthetic_vocabulary import (
                get_cinematography_cluster,
                get_lighting_preset,
            )

            cinematography = get_cinematography_cluster(industry)
            lighting = get_lighting_preset(industry)

            return f"""CINEMATOGRAPHY:
{cinematography}

LIGHTING:
{lighting}"""

    @staticmethod
    def _build_atmospheric_depth_block(
        style_dimensions: Optional[Dict[str, Any]],
        industry: str,
    ) -> str:
        """
        BLOCK 3: Atmospheric Depth (Rulebook p.5)
        Three-dimensional depth, foreground/midground/background layers.
        """
        if style_dimensions and style_dimensions.get("atmospheric_depth"):
            return style_dimensions["atmospheric_depth"]

        # Default atmospheric depth for all industries
        return """THREE DEPTH LAYERS:
1. FOREGROUND: Subtle soft-focus elements at frame edges (15% opacity)
2. MIDGROUND: Sharp focal plane where main subject exists
3. BACKGROUND: Atmospheric falloff with gentle bokeh or gradient

DEPTH CUES:
- Size variation (distant elements smaller)
- Atmospheric haze (distant elements less saturated)
- Focus differential (sharp subject, softer surroundings)
- Overlapping elements to establish spatial relationships"""

    @staticmethod
    def _build_motion_signature_block(industry: str, has_product: bool) -> str:
        """
        BLOCK 4: Motion Signature (Rulebook p.5)
        Dynamic energy, frozen motion, speed indicators.
        """
        motion_by_industry = {
            "fitness_gym": "Dynamic motion blur on extremities. Sweat droplets frozen mid-air. Explosive energy. Dust particles suspended. High-speed photography aesthetic (1/2000s shutter).",
            "food_beverage": "Steam rising from hot dishes. Liquid splash frozen in perfect arc. Sauce drip captured mid-fall. Condensation beads rolling down cold glass. Ingredients suspended in mid-air.",
            "fashion_ecommerce": "Fabric caught mid-movement with natural drape. Hair floating with slight motion blur. Garment edges showing flow and structure. Subtle kinetic energy.",
            "beauty_wellness": "Cream texture swirl. Liquid droplet falling. Powder particles diffusing. Product application mid-motion. Smooth, luxurious movement.",
        }

        if has_product:
            base_motion = motion_by_industry.get(industry, "Subtle dynamic energy. Elements suggesting motion without blur. Professional product photography precision.")
        else:
            base_motion = "Static composition with implied energy through diagonal lines and asymmetric balance."

        return f"""MOTION QUALITY:
{base_motion}

Frozen moment aesthetic. Every particle sharp and defined. High-speed photography precision."""

    @staticmethod
    def _build_micro_realism_block(industry: str, style_dimensions: Optional[Dict[str, Any]]) -> str:
        """
        BLOCK 5: Micro-Realism Layer (Rulebook p.5)
        Surface texture, material properties, fine details.
        """
        if style_dimensions and style_dimensions.get("material_properties"):
            return style_dimensions["material_properties"]

        # Industry-specific material vocabulary
        material_by_industry = {
            "food_beverage": "Visible food texture: grill marks, caramelization, glaze sheen, herb freshness, bread crust detail, condensation beads, oil sheen on surfaces.",
            "beauty_wellness": "Skin micro-texture: natural pores, dewiness, subtle shine zones. Product texture: cream viscosity, powder fineness, liquid transparency, glass refraction.",
            "fashion_ecommerce": "Fabric weave visible up close. Material drape and weight. Stitching detail. Surface texture: cotton matte, silk sheen, leather grain, denim texture.",
            "fitness_gym": "Sweat sheen on skin. Muscle definition with natural shadows. Metal equipment texture. Rubber mat grip pattern. Chalk dust particles in air.",
        }

        base_materials = material_by_industry.get(
            industry,
            "Surface micro-texture visible. Material properties clear: matte vs. glossy, rough vs. smooth, heavy vs. light. Natural imperfections present."
        )

        return f"""MATERIAL REALISM:
{base_materials}

MICRO-DETAILS:
- Surface texture at close inspection
- Light interaction with materials (reflection, refraction, absorption)
- Natural imperfections and variations
- Environmental effects (dust, condensation, wear)"""

    @staticmethod
    def _build_typography_hierarchy_block(
        brand_context: Dict[str, Any],
        seed_content: str,
        platform: str,
        slide_index: Optional[int],
        total_slides: Optional[int],
    ) -> str:
        """
        BLOCK 6: Typography Hierarchy (Rulebook p.5)
        Text placement, font selection, readability rules.
        """
        # Get CTA from brand context
        cta_styles = brand_context.get("cta_styles", [])
        if cta_styles:
            import random
            cta_text = random.choice(cta_styles)
        else:
            cta_text = brand_context.get("default_link", "Link in bio")

        # Carousel slide indicator
        slide_indicator = ""
        if slide_index is not None and total_slides is not None:
            slide_num = slide_index + 1
            slide_indicator = f"\n- Slide indicator: Small text '({slide_num}/{total_slides})' in bottom-right corner (20% of CTA size, semi-transparent)"

        return f"""TYPOGRAPHY RULES:
1. HIERARCHY: Maximum 3 text elements
   - Primary headline (5-7 words max, bold heavy weight)
   - Optional subtext (10-15 words, regular weight)
   - CTA: "{cta_text}" (subtle, 30% of headline size)

2. FONT SELECTION: Maximum 2 font families
   - Headline: Bold sans-serif or bold serif (never script/decorative)
   - Body/CTA: Regular sans-serif from same family or complementary pair

3. READABILITY:
   - Text contrast ratio minimum 4.5:1 against background
   - If text on photograph: Use semi-transparent dark overlay (rgba(0,0,0,0.4))
   - NO text effects: No drop shadows, glows, bevels, or emboss
   - Flat, clean, modern typography only

4. LAYOUT:
   - Text occupies max 40% of total image area
   - Visual elements dominate at 60%+
   - Minimum 5% margin on all edges
   - 15-20% of image should be empty space{slide_indicator}

5. PLATFORM OPTIMIZATION ({platform}):
   - Text must be legible at thumbnail size
   - Avoid small body copy below 18pt equivalent"""

    @staticmethod
    def _build_layout_geometry_block(
        platform: str,
        has_product: bool,
        style_dimensions: Optional[Dict[str, Any]],
    ) -> str:
        """
        BLOCK 7: Layout Geometry (Rulebook p.5)
        Composition rules, spatial organization, balance.
        """
        if has_product:
            # Product-focused composition
            return """COMPOSITION: Product-Focused Layout

SPATIAL ORGANIZATION:
- Product positioned using rule of thirds (40/60 split, not dead center)
- Product is the gravitational anchor - all other elements orbit around it
- Z-axis depth: Background (soft) → Product (sharp) → Foreground elements (atmospheric)

BALANCE:
- Asymmetric composition for dynamic energy
- Visual weight distributed: Product 50%, Text 25%, Negative space 25%
- Diagonal lines or triangular arrangement for movement

NEGATIVE SPACE:
- Generous breathing room around product (minimum 10% margins)
- Text lives in natural pockets of atmospheric space
- No crowding or claustrophobic framing"""
        else:
            # Concept/typography-focused composition
            return """COMPOSITION: Typography-Focused Layout

SPATIAL ORGANIZATION:
- Bold asymmetric layout using thirds grid
- Primary visual element fills 60% of frame
- Text positioned in contrasting zone for clear separation

BALANCE:
- Strong visual hierarchy: One dominant element, supporting elements 50% smaller
- Diagonal energy through angled text or compositional lines
- Golden ratio spacing between major elements

NEGATIVE SPACE:
- Minimum 20% of frame is empty space
- Margins: 5% minimum on all edges
- Generous padding between text blocks and visual elements"""

    @staticmethod
    def _build_cultural_context_block(
        region: str,
        industry: str,
        brand_context: Dict[str, Any],
    ) -> str:
        """
        BLOCK 8: Cultural Context (Rulebook p.18-19)
        Regional authenticity, African realism vocabulary.
        """
        if not region or "nigeria" not in region.lower() and "africa" not in region.lower():
            return ""  # Skip if not African market

        # Import African realism vocabulary
        from app.agents.social_media_manager.services.v3_african_realism import (
            get_african_realism_directive,
        )

        return get_african_realism_directive(industry, brand_context)

    @staticmethod
    def _build_brand_compliance_block(
        brand_name: str,
        brand_colors: List[str],
        brand_context: Dict[str, Any],
        seed_content: str,
    ) -> str:
        """
        BLOCK 9: Brand Compliance (Rulebook p.14)
        Brand name display rules, color restrictions, sensitive content protection.
        """
        # Import sensitive content rules
        from app.agents.social_media_manager.services.v3_sensitive_content_rules import (
            get_exclusion_rules,
        )

        # Color compliance
        color_str = ", ".join(brand_colors[:4]) if brand_colors else "neutral colors"
        primary = brand_colors[0] if brand_colors else "#000000"
        secondary = brand_colors[1] if len(brand_colors) > 1 else "#FFFFFF"

        # Brand name display logic
        seed_lower = seed_content.lower()
        show_brand_triggers = [
            "add our name", "add the name", "add our logo", "include the logo",
            "event flyer", "event poster", "flyer", "announcement",
        ]
        show_brand = any(trigger in seed_lower for trigger in show_brand_triggers)

        if show_brand:
            brand_directive = f'Display brand name "{brand_name}" prominently. Spell exactly as shown.'
        else:
            brand_directive = 'Do NOT display brand name or logo. Brand identity expressed through colors and visual treatment only.'

        # Get sensitive content exclusion rules
        exclusion_rules = get_exclusion_rules(seed_content, brand_context)

        return f"""BRAND IDENTITY COMPLIANCE:
Brand: {brand_name}

BRAND NAME DISPLAY:
{brand_directive}

COLOR RESTRICTIONS:
- Primary: {primary}
- Secondary: {secondary}
- Additional: {color_str}
- Neutral allowed: Black (#000000), White (#FFFFFF), Grey (#888888)
- NO other colors unless specified in Scene DNA

ABSOLUTE EXCLUSIONS:
{exclusion_rules}

CRITICAL: This image is EXCLUSIVELY for {brand_name}.
- NO other brand names, logos, products, or trademarks
- NO real-world companies or branded packaging
- NO celebrity faces or recognizable public figures
- Every element must come from the instructions in this prompt only"""

    @staticmethod
    def _build_quality_control_block(platform: str, industry: str) -> str:
        """
        BLOCK 10: Quality Control (Rulebook p.20)
        8-dimensional quality ontology validation rules.
        """
        return """QUALITY CONTROL CHECKLIST:

1. PRODUCT INTEGRITY (if applicable):
   - Product shape, colors, labels preserved exactly
   - No distortion or regeneration
   - Clear and recognizable

2. COMPOSITIONAL BALANCE:
   - Rule of thirds applied
   - Visual weight distributed asymmetrically
   - Clear focal point established

3. TYPOGRAPHY LEGIBILITY:
   - Contrast ratio ≥4.5:1
   - Readable at thumbnail size
   - No text effects or decorative fonts

4. COLOR HARMONY:
   - Brand colors used correctly
   - No unauthorized color additions
   - Consistent color temperature throughout

5. CULTURAL SENSITIVITY:
   - Authentic representation (if people shown)
   - Appropriate settings and context
   - No stereotypes or caricatures

6. TECHNICAL PRECISION:
   - Three depth layers clearly defined
   - Lighting direction consistent
   - Materials render realistically

7. BRAND COMPLIANCE:
   - No competitor brands visible
   - Brand guidelines followed
   - Exclusion rules respected

8. VISUAL DEPTH:
   - Foreground/midground/background layers
   - Atmospheric perspective present
   - Spatial relationships clear

OVERALL: The image must look like it was created by a professional human designer.
Restraint over excess. Clean over busy. Intentional over random."""

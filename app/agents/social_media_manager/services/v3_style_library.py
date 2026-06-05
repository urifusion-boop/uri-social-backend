"""
V3 Style Library - 11-Dimensional Style Ontology
Based on URI-Social-Image-Generation-Master-Rulebook-V3.pdf (Pages 16-17)

Each style is defined across 11 dimensions instead of a single prompt fragment:
1. Cinematography
2. Lighting Physics
3. Color Science
4. Material Properties
5. Atmospheric Depth
6. Motion Signature
7. Typography Hierarchy
8. Layout Geometry
9. Cultural Context
10. Brand Compliance
11. Quality Control

This allows parametric control and mixing of styles.
"""

from typing import Dict, Any, Optional, List


class StyleDimensions:
    """
    11-dimensional style definition.
    Each dimension is a detailed prompt fragment for that visual aspect.
    """

    def __init__(
        self,
        slug: str,
        name: str,
        description: str,
        industry_tags: List[str],
        cinematography: str,
        lighting_physics: str,
        color_science: str,
        material_properties: str,
        atmospheric_depth: str,
        motion_signature: str,
        typography_hierarchy: str,
        layout_geometry: str,
        cultural_context: str = "",
        composition_mode: str = "immersive",  # "immersive" or "editorial"
        style_type: Optional[str] = None,  # "art_piece" for 9:16 posters
    ):
        self.slug = slug
        self.name = name
        self.description = description
        self.industry_tags = industry_tags

        # The 11 dimensions
        self.cinematography = cinematography
        self.lighting_physics = lighting_physics
        self.color_science = color_science
        self.material_properties = material_properties
        self.atmospheric_depth = atmospheric_depth
        self.motion_signature = motion_signature
        self.typography_hierarchy = typography_hierarchy
        self.layout_geometry = layout_geometry
        self.cultural_context = cultural_context

        # Meta properties
        self.composition_mode = composition_mode
        self.style_type = style_type

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for easy access."""
        return {
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "industry_tags": self.industry_tags,
            "cinematography": self.cinematography,
            "lighting_physics": self.lighting_physics,
            "color_science": self.color_science,
            "material_properties": self.material_properties,
            "atmospheric_depth": self.atmospheric_depth,
            "motion_signature": self.motion_signature,
            "typography_hierarchy": self.typography_hierarchy,
            "layout_geometry": self.layout_geometry,
            "cultural_context": self.cultural_context,
            "composition_mode": self.composition_mode,
            "style_type": self.style_type,
        }


# ========== V3 STYLE DEFINITIONS ==========
# Migrated from existing style_library.py and expanded with 11 dimensions

V3_STYLES: Dict[str, StyleDimensions] = {}

# ── Fashion & E-commerce ─────────────────────────────────────────────────

V3_STYLES["street_editorial"] = StyleDimensions(
    slug="street_editorial",
    name="Street Editorial",
    description="Urban, edgy, magazine-quality. For brands with attitude.",
    industry_tags=["fashion_ecommerce", "events_entertainment", "general_other"],
    cinematography="High-fashion street photography style. Subject centered with confident pose. Cinematic 2.39:1 crop feel even in square format. Magazine editorial quality. Intentional bokeh background.",
    lighting_physics="Dramatic side lighting creating strong shadows. Harsh directional sunlight or single street lamp. High contrast with deep blacks and bright highlights. Chiaroscuro effect.",
    color_science="Slightly desaturated colour grading with lifted blacks. Gritty texture overlay at 5% opacity. One bold accent colour (neon pink, electric blue) against muted base.",
    material_properties="Urban textures: concrete grain, brick detail, metal rust, worn pavement. Fabric with natural wrinkles and texture. Authentic street wear.",
    atmospheric_depth="Urban environment backdrop with controlled bokeh. Foreground out-of-focus elements (railing, signage). Subject sharp in midground. Background city context softly blurred.",
    motion_signature="Slight motion blur on hair or fabric suggesting movement. Frozen peak moment. High shutter speed with selective blur for energy.",
    typography_hierarchy="Bold condensed sans-serif typography overlaid in white or neon accent colour. Text angled 5-10° for dynamism. All-caps headlines. Minimal body copy.",
    layout_geometry="Asymmetric composition. Subject in power third. Strong diagonal lines from urban architecture. Negative space creates tension.",
    cultural_context="",
    composition_mode="immersive",
)

V3_STYLES["clean_luxe"] = StyleDimensions(
    slug="clean_luxe",
    name="Clean Luxe",
    description="Minimalist, premium, lots of breathing room. For high-end brands.",
    industry_tags=["fashion_ecommerce", "beauty_wellness", "real_estate", "general_other"],
    cinematography="Luxury minimalist product photography. Pure white or soft cream background. Product centered with generous negative space on all sides. Medium format aesthetic.",
    lighting_physics="Soft even lighting with no harsh shadows. North-facing window light quality. Gentle specular highlights defining form. Light wraps completely around subject.",
    color_science="Colour palette limited to neutrals (white, cream, soft grey) plus one brand accent colour. Desaturated, refined, sophisticated. No pure black.",
    material_properties="Premium materials rendered with precision: matte leather, brushed metal, silk sheen, marble smoothness. Surface quality is hero.",
    atmospheric_depth="Minimal depth. Clean white infinity background. Subtle shadow grounding object. No foreground elements. Subject floats in pure space.",
    motion_signature="Absolute stillness. No motion. Static perfection. Every element in its precise place.",
    typography_hierarchy="Thin elegant serif typography in black or dark grey. Positioned with mathematical precision. Small, refined, whisper-quiet. Premium restraint.",
    layout_geometry="Centered composition with vast margins. 60%+ negative space. Golden ratio positioning. Perfect symmetry or controlled asymmetry.",
    cultural_context="",
    composition_mode="editorial",
)

V3_STYLES["afro_glam"] = StyleDimensions(
    slug="afro_glam",
    name="Afro-Glam",
    description="Celebration of African culture. Rich textures, warm tones, gold accents.",
    industry_tags=["fashion_ecommerce", "beauty_wellness", "events_entertainment", "general_other"],
    cinematography="African-inspired luxury aesthetic. Subject confidently centered or in power third. Cultural pride and celebration. Editorial fashion quality.",
    lighting_physics="Warm directional lighting emphasising skin tones beautifully. Golden hour quality or warm studio setup. Highlights create glow on melanin skin. Flattering, luminous.",
    color_science="Rich warm colour palette: deep oranges, golds, burgundy, dark green, royal purple. High saturation. Jewel tones. Warm temperature 3200K-3800K.",
    material_properties="Ankara or kente textile patterns as subtle background textures at low opacity. Gold foil accent elements. Silk, velvet, rich fabrics. Ornate but not cluttered.",
    atmospheric_depth="Layered cultural elements. Textile patterns in background at 30% opacity. Decorative elements creating depth. Warm atmospheric glow throughout.",
    motion_signature="Fabric in gentle motion. Hair with volume and movement. Dynamic but regal. Celebratory energy.",
    typography_hierarchy="Bold display typography mixing serif and hand-lettered styles. Gold foil effect on key words. Large confident headlines. African-inspired geometric decorations.",
    layout_geometry="Ornate but balanced. Cultural patterns frame composition. Subject dominates. Decorative borders or corner elements. Celebratory abundance.",
    cultural_context="Nigerian/African cultural pride. Authentic representation of African beauty, fashion, and aesthetics. No stereotypes. Dignified celebration.",
    composition_mode="immersive",
)

# ── Food & Beverage ──────────────────────────────────────────────────────

V3_STYLES["overhead_feast"] = StyleDimensions(
    slug="overhead_feast",
    name="Overhead Feast",
    description="Top-down spread. Rustic surface. Abundance.",
    industry_tags=["food_beverage"],
    cinematography="Overhead flat-lay food photography. Shot directly from above at perfect 90°. Multiple dishes arranged artfully. Convivial, abundant, sharing-focused.",
    lighting_physics="Warm natural lighting from north-facing window. Soft shadows falling at 45° revealing depth. Even illumination across entire spread. Golden hour warmth 3500K.",
    color_science="Rich saturated food colours. Warm palette: golden browns, deep reds, vibrant greens, creamy whites. Natural food colour accuracy with slight saturation boost.",
    material_properties="Rustic wooden table or marble surface grain visible. Food textures: crusty bread, glistening sauces, fresh herb detail. Ceramic glaze, metal cutlery reflections.",
    atmospheric_depth="Perfect flat plane from overhead. All elements in focus (f/8-f/11). Intentional gaps create negative space. Layered arrangement shows depth through overlap.",
    motion_signature="Static composition. Occasional steam from hot dish. Sauce drip frozen. Mostly still life perfection.",
    typography_hierarchy="Minimal text overlay. If present: small handwritten-style script in corner. Recipe name in elegant serif. Text never competes with food.",
    layout_geometry="Organized flat-lay with intentional asymmetry. Ingredients scattered artfully. Negative space balanced. Visual flow guides eye through spread.",
    cultural_context="",
    composition_mode="editorial",
)

V3_STYLES["dark_moody_food"] = StyleDimensions(
    slug="dark_moody_food",
    name="Dark & Moody Food",
    description="Dramatic. Premium. Chef-quality presentation.",
    industry_tags=["food_beverage"],
    cinematography="Dark food photography style. Single dish as hero, styled with precision. Fine dining and premium brand feel. Chiaroscuro aesthetic.",
    lighting_physics="Dramatic side lighting with visible light falloff. Single hard light source at 90° angle. Deep shadows with detail preserved. Low key lighting setup.",
    color_science="Rich deep colours: mahogany sauces, deep greens, burnished golds. Dark charcoal or black background. Warm shadows 2800K. Highlights 4000K.",
    material_properties="Deep charcoal, slate, or black surfaces. Food texture hyper-detailed: char marks, glaze sheen, herb oil droplets. Premium plating on dark ceramic.",
    atmospheric_depth="Dark background receding to black. Dish emerges from darkness. Subtle foreground elements (cutlery, ingredients) out of focus. Dramatic depth.",
    motion_signature="Sauce drip captured mid-fall. Steam rising. Mostly static with one dynamic element. Frozen moment of plating perfection.",
    typography_hierarchy="Thin gold or cream serif font. Very small, refined. Bottom corner or integrated into dark space. Whisper-quiet elegance.",
    layout_geometry="Centered or power-third placement. Dish isolated. Minimal props. Vast dark negative space. Spotlight effect on food.",
    cultural_context="",
    composition_mode="immersive",
)

# ── Fintech, SaaS & Tech ─────────────────────────────────────────────────

V3_STYLES["corporate_gradient"] = StyleDimensions(
    slug="corporate_gradient",
    name="Corporate Gradient",
    description="Smooth gradients, professional, trust. The LinkedIn standard.",
    industry_tags=["fintech_saas_tech", "education_consulting", "general_other"],
    cinematography="Professional corporate graphic aesthetic. Clean, modern, trustworthy. Enterprise-grade visual quality. Abstract concepts visualized.",
    lighting_physics="Even, shadowless illumination. No harsh contrasts. Soft ambient light. Studio-perfect consistency. No natural light cues.",
    color_science="Smooth gradient background: deep blue to purple, teal to blue, or dark navy to medium blue. Professional, trustworthy palette. Cool temperature 6000K+.",
    material_properties="Digital surfaces: glass, frosted acrylic, subtle metallic accents. Device mockups with screen glow. Abstract geometric shapes in glass/acrylic finish.",
    atmospheric_depth="Layered geometric shapes at varying opacity. Foreground shapes at 80% opacity, midground device/data at 100%, background gradient at 100%.",
    motion_signature="Subtle parallax suggestion. No actual motion. Static perfection with implied depth through layering.",
    typography_hierarchy="Clean sans-serif typography (Inter, SF Pro, Helvetica) in white. Centered or left-aligned. Clear hierarchy: headline, subtext, CTA. Corporate precision.",
    layout_geometry="Centered or grid-based layout. Geometric shapes (circles, lines, grids) as decorative elements at low opacity. Balanced, symmetrical, professional.",
    cultural_context="",
    composition_mode="editorial",
)

V3_STYLES["minimal_tech"] = StyleDimensions(
    slug="minimal_tech",
    name="Minimal Tech",
    description="Apple-inspired. Whitespace. Precision.",
    industry_tags=["fintech_saas_tech", "education_consulting"],
    cinematography="Ultra-minimal tech aesthetic inspired by Apple design language. Single product or concept as focal point. Extreme negative space. Medium format precision.",
    lighting_physics="Perfect studio lighting. Soft overhead key with gentle fill. No shadows visible except subtle product shadow. Clean, clinical, precise illumination.",
    color_science="Vast white or very light grey space. Monochromatic with single accent color (blue, red, orange). Desaturated, restrained, sophisticated.",
    material_properties="Premium tech materials: brushed aluminum, matte plastic, gorilla glass. Every surface rendered with photorealistic precision. Clean, new, pristine.",
    atmospheric_depth="Minimal depth. Product floats in white infinity space. Subtle shadow grounds object. No foreground/background elements. Pure isolation.",
    motion_signature="Absolute stillness. Static perfection. No motion. Product photography precision.",
    typography_hierarchy="Thin light-weight sans-serif (SF Pro Light, Helvetica Neue Ultralight). Minimal text. Small, refined, precise. Whisper-quiet sophistication.",
    layout_geometry="Extreme negative space (70%+). Product centered or power-third. Golden ratio precision. Mathematical perfection. Every element earns its place.",
    cultural_context="",
    composition_mode="editorial",
)

# ── Beauty & Wellness ────────────────────────────────────────────────────

V3_STYLES["glow_up"] = StyleDimensions(
    slug="glow_up",
    name="Glow Up",
    description="Warm golden lighting, dewy skin, aspirational beauty close-ups.",
    industry_tags=["beauty_wellness"],
    cinematography="Soft glowing beauty photography. Close-up portrait with warm backlighting. Beauty editorial quality without heavy retouching. Aspirational but achievable.",
    lighting_physics="Warm golden-hour backlighting creating luminous halo effect. Soft fill light on face. Skin appears naturally dewy. Catchlights at 10 and 2 o'clock.",
    color_science="Warm amber or blush tones. Color temperature 3500K-4200K. Soft bokeh background in warm amber, peach, or blush. Flattering golden glow.",
    material_properties="Skin with natural dewy appearance and smooth texture. Not overly retouched. Natural pores visible. Hair with individual strand definition. Luminous, healthy.",
    atmospheric_depth="Soft bokeh background in warm tones. Subject in sharp focus. Background creates depth through color and blur. Halo backlight creates separation.",
    motion_signature="Gentle hair movement from subtle breeze. Mostly static. Natural micro-movements. Not frozen or stiff.",
    typography_hierarchy="Thin serif or script typography in gold or champagne. Small, refined, elegant. Never competes with subject. Bottom corner placement.",
    layout_geometry="Subject fills frame or positioned in power third. Negative space in warm bokeh. Face angled for flattering perspective. Beauty editorial composition.",
    cultural_context="",
    composition_mode="immersive",
)

# ── Fitness & Gym ────────────────────────────────────────────────────────

V3_STYLES["energy_motion"] = StyleDimensions(
    slug="energy_motion",
    name="Energy & Motion",
    description="Dynamic action shots, motion blur, sweat and intensity.",
    industry_tags=["fitness_gym"],
    cinematography="Dynamic sports action photography. Athlete caught mid-movement. High-speed photography feel. Maximum energy and movement. ESPN or Red Bull aesthetic.",
    lighting_physics="Strong directional lighting creating muscle definition. Dramatic side key light at 90°. High contrast shadows. Sweat sheen highlights. Hard light source.",
    color_science="Bright saturated colours: electric orange, vivid yellow, or lime green against dark background. High contrast. Punchy, energetic palette. 5500K daylight balance.",
    material_properties="Sweat droplets visible. Muscle texture and definition. Athletic fabric texture. Rubber gym floor. Metal equipment reflections. Chalk dust particles in air.",
    atmospheric_depth="Dark background receding. Athlete in sharp focus. Dust/particles suspended in air creating depth. Motion blur on extremities creating forward/backward depth.",
    motion_signature="Motion blur on extremities emphasising speed and power. Dust, water droplets suspended mid-air. High-speed freeze at 1/2000s. Peak action captured.",
    typography_hierarchy="Bold angled typography at 10-15° tilt. Heavy condensed sans-serif. All-caps. High energy. Overlaid with semi-transparent dark bar for legibility.",
    layout_geometry="Asymmetric diagonal composition. Subject breaking frame edge. Dynamic triangular arrangement. Visual movement from bottom-left to top-right.",
    cultural_context="",
    composition_mode="immersive",
)


# ========== HELPER FUNCTIONS ==========

def get_style_dimensions(style_slug: Optional[str], industry: str = "general_other") -> Optional[Dict[str, Any]]:
    """
    Retrieve full 11-dimensional style definition.

    Args:
        style_slug: Style identifier (e.g., "afro_glam")
        industry: Fallback for selecting default style if slug not found

    Returns:
        Dictionary with all 11 dimensions, or None if not found
    """
    if style_slug and style_slug in V3_STYLES:
        return V3_STYLES[style_slug].to_dict()

    # Fallback: suggest default style for industry
    industry_defaults = {
        "fashion_ecommerce": "street_editorial",
        "food_beverage": "overhead_feast",
        "beauty_wellness": "glow_up",
        "fitness_gym": "energy_motion",
        "fintech_saas_tech": "corporate_gradient",
    }

    default_slug = industry_defaults.get(industry)
    if default_slug and default_slug in V3_STYLES:
        return V3_STYLES[default_slug].to_dict()

    return None


def list_styles_for_industry(industry: str) -> List[Dict[str, str]]:
    """
    Get all styles applicable to a given industry.
    Returns list of {slug, name, description} for UI display.
    """
    applicable_styles = []

    for slug, style in V3_STYLES.items():
        if industry in style.industry_tags or "general_other" in style.industry_tags:
            applicable_styles.append({
                "slug": slug,
                "name": style.name,
                "description": style.description,
            })

    return applicable_styles


def get_all_style_slugs() -> List[str]:
    """Return list of all available style slugs."""
    return list(V3_STYLES.keys())

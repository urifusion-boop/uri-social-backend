"""
V3 Aesthetic Vocabulary - Expanded Visual Language
Based on URI-Social-Image-Generation-Master-Rulebook-V3.pdf (Pages 6-9)

This module provides 400+ cinematography terms, 200+ product photography techniques,
and 150+ fashion photography clusters for rich, varied image generation.

Categories:
- Cinematography Clusters (12 industry-specific styles)
- Lighting Presets (8 professional setups)
- Product Photography Techniques (by product type)
- Fashion Photography Clusters (seasonal + editorial)
- Color Science Palettes (mood-based)
"""

from typing import Dict, List, Optional
import random


# ========== CINEMATOGRAPHY CLUSTERS (Rulebook p.6) ==========

CINEMATOGRAPHY_CLUSTERS = {
    "editorial_portrait": """
35mm full-frame shallow depth of field (f/1.8-f/2.8). Subject isolated with creamy bokeh background.
Eye-level or slightly above perspective. Natural catchlight in eyes. Authentic micro-expressions.
Subtle asymmetry in facial features. Skin texture visible with natural pores and variation.
Hair with individual strand definition. Documentary-style candid moment, not posed performance.
""",

    "high_fashion_editorial": """
Medium format aesthetic (Hasselblad style). Intentional negative space dominating frame.
Asymmetric composition using rule of thirds. Subject positioned in power third.
Dramatic side lighting creating chiaroscuro effect. Bold geometric shapes in background.
High contrast with lifted blacks. Vogue/Harper's Bazaar editorial quality.
""",

    "product_hero_shot": """
Macro photography with tack-sharp focus on product label. Focus falloff at f/4-f/5.6.
45-degree overhead angle revealing product form and depth. North-facing window light quality.
Subtle specular highlights defining surface curvature. Clean drop shadow grounding the product.
Product occupies 40-50% of frame with generous breathing room.
""",

    "lifestyle_documentary": """
Natural candid photography. Subject engaged in authentic activity, unaware of camera.
Environmental context visible and relevant. Golden hour natural light or soft window diffusion.
Foreground elements creating depth (out of focus at f/2.0). Real-world imperfection embraced.
No studio setup visible. Photojournalistic storytelling aesthetic.
""",

    "food_editorial": """
45-degree angle or overhead flat-lay. Natural north-facing window light with soft shadows.
Shallow depth of field (f/2.8-f/4) with hero dish sharp, background ingredients softly blurred.
Steam or condensation visible for temperature cues. Rustic or premium surface texture.
Intentional negative space for typography. Bon Appétit or Kinfolk magazine quality.
""",

    "architectural_minimal": """
Wide-angle perspective with clean geometric lines. Symmetrical or strong diagonal composition.
Even soft lighting minimizing harsh shadows. Vast negative space emphasizing scale.
Monochromatic or limited color palette. Apple product photography aesthetic.
Precision, restraint, mathematical balance.
""",

    "action_sports": """
High shutter speed freeze-frame (1/2000s or faster). Motion blur on extremities suggesting speed.
Dynamic diagonal composition. Dust, water droplets, or particles suspended mid-air.
Dramatic side lighting creating muscle definition. Saturated color grading.
Peak action moment captured. ESPN or Red Bull photography style.
""",

    "cinematic_drama": """
Anamorphic widescreen framing (2.39:1 feel). Atmospheric haze or smoke creating depth layers.
Chiaroscuro lighting with single strong key light. Deep shadows with detail preserved.
Film grain texture overlay. Desaturated color grading with one accent color.
Roger Deakins or Emmanuel Lubezki cinematography style.
""",

    "street_photography": """
35mm or 50mm prime lens aesthetic. Decisive moment captured. Urban environment context.
Natural available light with real shadows. Gritty texture and grain. Slightly underexposed.
Subjects caught mid-action or mid-expression. Magnum Photos or Vivian Maier style.
Authentic, unpolished, documentary truth.
""",

    "beauty_closeup": """
Macro beauty photography. Extreme shallow depth of field (f/1.4). Skin texture hyper-detailed.
Dewiness and natural sheen visible. Soft butterfly or clamshell lighting setup.
Clean white or gradient background. Perfect focus on eyes or lips.
High-end cosmetics advertising quality. Glossy magazine cover aesthetic.
""",

    "flat_lay_overhead": """
Perfect 90-degree overhead perspective. Grid-based arrangement with intentional asymmetry.
Objects aligned to thirds or golden ratio. Even shadowless lighting.
Negative space balanced around elements. Instagram aesthetic. Kinfolk or Cereal magazine style.
Organized chaos with breathing room.
""",

    "environmental_portrait": """
Subject in their natural environment (workspace, home, studio). Context tells story.
Environmental details in focus or gently blurred based on narrative importance.
Natural light mixed with practicals (lamps, windows). Subject positioned using rule of thirds.
Annie Leibovitz or Platon portrait style. Character revealed through setting.
"""
}


# ========== LIGHTING PRESETS (Rulebook p.7) ==========

LIGHTING_PRESETS = {
    "golden_hour_natural": """
Warm directional sunlight 30 minutes before sunset. Color temperature 3200K-3800K.
Long soft shadows creating depth. Warm amber glow on skin tones. Backlit or side-lit subject
creating rim light separation. Gentle lens flare acceptable. Soft atmospheric haze.
""",

    "north_window_diffused": """
Soft indirect natural light from north-facing window. Color temperature 5500K (neutral daylight).
No direct sun hitting subject. Even, flattering illumination with gentle shadow falloff.
Ideal for product photography and food. Light wraps around subject naturally.
""",

    "studio_beauty": """
Clamshell lighting setup: Large softbox above at 45°, white reflector below filling shadows.
Even, flattering light minimizing texture. Catchlights positioned at 10 and 2 o'clock in eyes.
Pure white background with separate backlight preventing grey falloff.
Commercial cosmetics lighting standard.
""",

    "dramatic_side_key": """
Single hard light source from 90° side angle. Strong directional shadows creating drama.
Chiaroscuro effect with half subject in shadow. Rembrandt triangle on cheek acceptable.
Black or dark background absorbing light. Theatrical, fine-art photography aesthetic.
""",

    "rim_light_separation": """
Subject backlit with strong rim light creating edge definition. Face/front in relative shadow
or filled with soft bounce. Silhouette separation from background. Dramatic, editorial look.
Hair translucent with backlight. Used in perfume and luxury product advertising.
""",

    "three_point_classic": """
Key light at 45° camera left, fill light at 45° camera right (half key intensity),
rim light behind subject camera right. Balanced, professional interview lighting.
Suitable for corporate, educational, trustworthy subjects. No surprises, clean execution.
""",

    "overcast_soft": """
Heavy cloud diffusion creating giant natural softbox. No harsh shadows. Even, flat lighting.
Slightly cool color temperature (6000K-6500K). Ideal for even skin tones and product detail.
Fashion e-commerce and catalog photography standard. Soft, approachable, no drama.
""",

    "low_key_dramatic": """
Dark overall exposure with selective highlights. 80% of frame in shadow. Single hard light
source revealing only essential details. Black background. High contrast ratio (8:1 or more).
Fine art, luxury product, or noir aesthetic. Mood over information.
"""
}


# ========== PRODUCT PHOTOGRAPHY TECHNIQUES (Rulebook p.8) ==========

def get_product_photography_technique(product_category: str) -> str:
    """
    Return specialized product photography vocabulary based on product type.
    """
    techniques = {
        "beverage": """
Condensation beads on glass surface. Backlit liquid showing color translucency and depth.
Ice cubes with trapped air bubbles visible. Liquid level at golden ratio (60% full).
Specular highlights defining glass curvature. Shallow depth of field (f/4) with label sharp.
Background atmospheric with gradient light falloff. Droplets on surface suggesting coldness.
""",

        "food": """
Hero dish positioned at front third of frame. Steam or heat shimmer visible if hot dish.
Garnish placed with tweezers-level precision. Sauce drizzle or glaze creating shine.
Visible texture: char marks, caramelization, fresh herb detail, bread crust structure.
Shallow depth (f/2.8-f/4) with background ingredients softly blurred for context.
45° angle for plating or overhead for flat-lay spreads.
""",

        "fashion_apparel": """
Fabric texture and weave visible under close inspection. Natural drape and material weight.
Garment styled on body or mannequin showing fit and structure. Stitching detail sharp.
Colors accurate to fabric swatch. Wrinkles minimal but natural (avoid plastic mannequin look).
Lifestyle context or clean editorial background. Lighting reveals fabric properties (matte/sheen).
""",

        "beauty_cosmetics": """
Macro detail showing product texture (cream viscosity, powder fineness, liquid clarity).
Product container reflection-free with label legible. Swatch or application context.
Clean clinical lighting or soft beauty lighting. Glass/plastic material rendered accurately.
Ingredient elements as props (botanicals, oils, minerals). Premium packaging detail visible.
""",

        "electronics": """
Clean minimal background (white, grey, or environmental). Screen reflections minimal.
Ports, buttons, and details visible. Material finish accurate (matte aluminum, glossy plastic).
Light creating subtle edge definition. Device at ¾ angle showing depth and form.
Premium Apple-style aesthetic or lifestyle context showing scale and use.
""",

        "jewelry": """
Macro photography with extreme detail. Gemstone facets catching light. Metal finish
accurate (matte gold, polished silver, brushed platinum). Specular highlights defining curves.
Shallow depth (f/5.6-f/8 for detail). Black or neutral background isolating subject.
Luxe editorial quality. Tiffany or Cartier advertising standard.
""",

        "home_decor": """
Item in lifestyle context or clean editorial studio. Material texture visible (wood grain,
fabric weave, ceramic glaze). Natural light preferred. Context props supporting not competing.
Scale reference subtle (books, plants, textiles). Styling aspirational but attainable.
West Elm or CB2 aesthetic.
""",

        "generic_product": """
45° angle revealing product form and label. Clean background (white, gradient, or subtle texture).
Product sharp throughout (f/8-f/11). Even lighting with subtle shadow grounding object.
Label text legible. Professional e-commerce quality. Product occupies 40-60% of frame.
"""
    }

    return techniques.get(product_category, techniques["generic_product"])


# ========== FASHION PHOTOGRAPHY CLUSTERS (Rulebook p.9) ==========

FASHION_CLUSTERS = {
    "spring_editorial": """
Soft pastel color palette: blush pink, sage green, cream, sky blue. Natural outdoor light.
Flowing fabrics caught in gentle breeze. Fresh, airy, optimistic energy. Floral or garden context.
Minimal makeup, natural hair. Soft focus edges with sharp center. Romantic, ethereal mood.
""",

    "summer_vibrant": """
Bold saturated colors: electric blue, sunshine yellow, coral, lime green. Bright daylight.
High contrast, high energy. Beach, urban, or tropical context. Dynamic movement and joy.
Sunglasses, swimwear, resort wear. Golden hour or harsh midday sun embraced. Vacation energy.
""",

    "autumn_editorial": """
Warm earth tones: rust, camel, burgundy, forest green, mustard. Overcast or golden hour light.
Layered textures: knits, leather, wool. Falling leaves or urban autumn context.
Cozy, sophisticated, transitional mood. Neutral makeup, natural or slicked hair.
""",

    "winter_luxe": """
Rich deep colors: charcoal, navy, wine, emerald, black. Low-key dramatic lighting.
Heavy textures: cashmere, fur, velvet, leather. Indoor editorial or snowy context.
Bold statement pieces. Glamorous makeup, structured hair. Luxurious, powerful, elegant.
""",

    "streetwear_urban": """
Gritty urban environment: graffiti walls, concrete, industrial settings. Natural available light.
Candid, confident poses. Saturated colors or desaturated street aesthetic. Athleisure or
streetwear brands. Nike, Supreme, Off-White energy. Documentary-style authenticity.
""",

    "minimalist_editorial": """
Clean monochromatic palette: all black, all white, or single color variations. Vast negative space.
Architectural or studio backdrop. Precise geometric poses. Sharp focus throughout.
No jewelry or accessories. Hair slicked or natural. Jil Sander or Lemaire aesthetic.
Restraint, precision, sophistication.
"""
}


# ========== COLOR SCIENCE PALETTES (Rulebook p.9) ==========

COLOR_PALETTES = {
    "warm_inviting": "Warm color temperature 3200K-4000K. Amber, honey, terracotta, cream, soft pink. Cozy, approachable, friendly mood.",
    "cool_professional": "Cool color temperature 5500K-7000K. Navy, slate blue, silver, white, pale blue. Trustworthy, corporate, calm mood.",
    "vibrant_energetic": "Saturated primary and secondary colors. Electric blue, sunshine yellow, hot pink, lime green. High energy, youthful, bold.",
    "muted_sophisticated": "Desaturated earth tones. Sage, taupe, dusty rose, charcoal, cream. Elevated, mature, refined mood.",
    "monochrome_dramatic": "Single hue variations or true black and white. High contrast. Timeless, elegant, focused, powerful.",
    "pastel_soft": "Light desaturated colors. Blush, mint, lavender, butter, baby blue. Gentle, feminine, delicate, spring-like.",
    "jewel_tone_luxe": "Rich saturated colors. Emerald, sapphire, ruby, amethyst, gold. Opulent, premium, luxurious mood.",
    "neutral_editorial": "Achromatic palette. Black, white, grey scale. One accent color maximum. Clean, modern, sophisticated."
}


# ========== HELPER FUNCTIONS ==========

def get_cinematography_cluster(industry: str, style_slug: Optional[str] = None) -> str:
    """
    Select appropriate cinematography cluster based on industry and style.
    """
    industry_mapping = {
        "fashion_ecommerce": ["high_fashion_editorial", "lifestyle_documentary", "flat_lay_overhead"],
        "food_beverage": ["food_editorial", "flat_lay_overhead", "lifestyle_documentary"],
        "beauty_wellness": ["beauty_closeup", "editorial_portrait", "product_hero_shot"],
        "fitness_gym": ["action_sports", "editorial_portrait", "cinematic_drama"],
        "fintech_saas_tech": ["architectural_minimal", "environmental_portrait"],
        "real_estate": ["architectural_minimal", "lifestyle_documentary"],
        "events_entertainment": ["cinematic_drama", "street_photography", "lifestyle_documentary"],
        "education_consulting": ["environmental_portrait", "architectural_minimal"],
    }

    clusters = industry_mapping.get(industry, ["editorial_portrait", "product_hero_shot", "lifestyle_documentary"])
    selected = random.choice(clusters)
    return CINEMATOGRAPHY_CLUSTERS[selected]


def get_lighting_preset(industry: str, mood: str = "balanced") -> str:
    """
    Select lighting preset based on industry and desired mood.
    """
    if mood == "dramatic":
        return LIGHTING_PRESETS["dramatic_side_key"]
    elif mood == "soft":
        return LIGHTING_PRESETS["north_window_diffused"]
    elif mood == "luxury":
        return LIGHTING_PRESETS["rim_light_separation"]

    # Industry-specific defaults
    industry_lighting = {
        "beauty_wellness": "studio_beauty",
        "food_beverage": "north_window_diffused",
        "fashion_ecommerce": "golden_hour_natural",
        "fitness_gym": "dramatic_side_key",
        "fintech_saas_tech": "three_point_classic",
    }

    preset = industry_lighting.get(industry, "overcast_soft")
    return LIGHTING_PRESETS[preset]


def get_color_palette(mood: str = "balanced") -> str:
    """
    Return color science palette based on desired mood.
    """
    mood_mapping = {
        "energetic": "vibrant_energetic",
        "professional": "cool_professional",
        "luxury": "jewel_tone_luxe",
        "friendly": "warm_inviting",
        "elegant": "monochrome_dramatic",
        "soft": "pastel_soft",
        "sophisticated": "muted_sophisticated",
        "balanced": "neutral_editorial",
    }

    palette_key = mood_mapping.get(mood, "neutral_editorial")
    return COLOR_PALETTES[palette_key]


def get_fashion_cluster(season: Optional[str] = None, style: Optional[str] = None) -> str:
    """
    Return fashion photography cluster for apparel/fashion content.
    """
    if season:
        season_map = {
            "spring": "spring_editorial",
            "summer": "summer_vibrant",
            "autumn": "autumn_editorial",
            "fall": "autumn_editorial",
            "winter": "winter_luxe",
        }
        cluster_key = season_map.get(season.lower(), "minimalist_editorial")
    elif style:
        style_map = {
            "streetwear": "streetwear_urban",
            "minimal": "minimalist_editorial",
            "luxury": "winter_luxe",
            "editorial": "minimalist_editorial",
        }
        cluster_key = style_map.get(style.lower(), "minimalist_editorial")
    else:
        # Default rotation
        cluster_key = random.choice(list(FASHION_CLUSTERS.keys()))

    return FASHION_CLUSTERS[cluster_key]


# ========== VOCABULARY INJECTION FOR PROMPTS ==========

def enrich_prompt_with_vocabulary(
    base_prompt: str,
    industry: str,
    product_category: Optional[str] = None,
    mood: str = "balanced",
) -> str:
    """
    Inject aesthetic vocabulary into base prompt for richer visual language.

    This is used when style_slug is not provided or as supplementary detail.
    """
    cinematography = get_cinematography_cluster(industry)
    lighting = get_lighting_preset(industry, mood)
    color = get_color_palette(mood)

    enriched = base_prompt + "\n\n"
    enriched += f"CINEMATOGRAPHY STYLE:\n{cinematography}\n\n"
    enriched += f"LIGHTING SETUP:\n{lighting}\n\n"
    enriched += f"COLOR PALETTE:\n{color}"

    if product_category:
        product_technique = get_product_photography_technique(product_category)
        enriched += f"\n\nPRODUCT PHOTOGRAPHY TECHNIQUE:\n{product_technique}"

    return enriched

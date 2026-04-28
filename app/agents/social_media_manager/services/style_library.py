"""
Visual Style System — style library and selection logic.

Each style has:
  slug            — unique key
  name            — user-facing label
  description     — shown in the style picker lightbox (1 sentence)
  industry_tags   — which industry categories show this style
  prompt_fragment — injected verbatim as the first block of every image prompt

Industry slugs must match the values stored in brand_profiles.industry.
"""

from __future__ import annotations
from typing import Dict, List, Optional, Any

# ---------------------------------------------------------------------------
# Style definitions
# ---------------------------------------------------------------------------

STYLES: Dict[str, Dict[str, Any]] = {

    # ── Fashion & E-commerce ─────────────────────────────────────────────────

    "street_editorial": {
        "name": "Street Editorial",
        "description": "Urban, edgy, magazine-quality. For brands with attitude.",
        "industry_tags": ["fashion_ecommerce", "events_entertainment", "general_other"],
        "prompt_fragment": (
            "High-fashion street photography style. Urban environment backdrop with intentional bokeh. "
            "Subject centered with confident pose. Dramatic side lighting creating strong shadows. "
            "Bold condensed sans-serif typography overlaid in white or neon accent colour. "
            "Slightly desaturated colour grading with lifted blacks. Gritty texture overlay at 5% opacity. "
            "Cinematic 2.39:1 crop feel even in square format. Magazine editorial quality."
        ),
    },

    "clean_luxe": {
        "name": "Clean Luxe",
        "description": "Minimalist, premium, lots of breathing room. For high-end brands.",
        "industry_tags": ["fashion_ecommerce", "beauty_wellness", "real_estate", "general_other"],
        "prompt_fragment": (
            "Luxury minimalist product photography. Pure white or soft cream background with subtle shadow. "
            "Product centered with generous negative space on all sides. Soft even lighting with no harsh shadows. "
            "Thin elegant serif typography in black or dark grey, positioned with mathematical precision. "
            "No decorative elements. Premium feel through restraint and whitespace. "
            "Colour palette limited to neutrals plus one brand accent colour."
        ),
    },

    "neon_pop": {
        "name": "Neon Pop",
        "description": "Electric, vibrant, nightlife energy. For bold brands.",
        "industry_tags": ["fashion_ecommerce", "beauty_wellness", "fitness_gym", "events_entertainment", "general_other"],
        "prompt_fragment": (
            "Vivid neon-lit photography style. Dark or black background with strong neon colour accents "
            "in pink, electric blue, or purple. Dramatic coloured lighting casting coloured shadows. "
            "Bold heavy sans-serif typography with glow or neon tube effect. High saturation, high contrast. "
            "Club/nightlife energy. Lens flare effects subtle but present. Cyberpunk-adjacent aesthetic."
        ),
    },

    "afro_glam": {
        "name": "Afro-Glam",
        "description": "Celebration of African culture. Rich textures, warm tones, gold accents.",
        "industry_tags": ["fashion_ecommerce", "beauty_wellness", "events_entertainment", "general_other"],
        "prompt_fragment": (
            "African-inspired luxury aesthetic. Rich warm colour palette: deep oranges, golds, burgundy, and dark green. "
            "Ankara or kente textile patterns as subtle background textures at low opacity. "
            "Gold foil accent elements on typography. Bold display typography mixing serif and hand-lettered styles. "
            "Warm directional lighting emphasising skin tones beautifully. Cultural pride aesthetic. Ornate but not cluttered."
        ),
    },

    "minimal_studio": {
        "name": "Minimal Studio",
        "description": "Product-first. Solid backgrounds. No distractions.",
        "industry_tags": ["fashion_ecommerce", "beauty_wellness", "food_beverage", "general_other"],
        "prompt_fragment": (
            "Professional product photography on solid colour backdrop. "
            "Colours: soft grey, muted blush, sage green, or cream. "
            "Single product hero shot with perfect lighting from 45 degrees above. "
            "No text overlay unless specifically requested. Clean drop shadow or gentle reflection on surface. "
            "Focus on product details, texture, and craftsmanship. E-commerce catalogue quality."
        ),
    },

    "bold_loud": {
        "name": "Bold & Loud",
        "description": "Maximum energy. Big text. In your face. For brands that shout.",
        "industry_tags": [
            "fashion_ecommerce", "food_beverage", "fitness_gym",
            "events_entertainment", "general_other",
        ],
        "prompt_fragment": (
            "High-energy promotional graphic. Full-bleed bold background colour from brand palette. "
            "Massive condensed sans-serif typography filling 60%+ of the frame. "
            "Text stacked vertically or at slight angle for dynamism. "
            "Minimal photography, used as small cutout or background texture only. "
            "Starburst, arrow, or badge elements for emphasis. Reminiscent of sale flyers and event posters. Nothing subtle."
        ),
    },

    "vintage_film": {
        "name": "Vintage Film",
        "description": "Nostalgic, warm, analogue. For brands with a story.",
        "industry_tags": ["fashion_ecommerce", "food_beverage", "general_other"],
        "prompt_fragment": (
            "Analogue film photography aesthetic. Warm colour cast with slight orange/amber tone shift. "
            "Visible film grain at medium intensity. Slightly faded highlights and lifted shadows. "
            "Soft focus edges with sharp centre. Vintage serif or typewriter-style typography. "
            "Light leak effects in corners. 35mm candid photography feel. Nostalgic warmth."
        ),
    },

    "catalogue_clean": {
        "name": "Catalogue Clean",
        "description": "Structured, grid-ready, professional. For brands with multiple products.",
        "industry_tags": ["fashion_ecommerce", "food_beverage", "general_other"],
        "prompt_fragment": (
            "Clean catalogue-style product layout. White or light grey background. "
            "Product arranged in a structured grid or neatly laid out flat-lay composition. "
            "Even shadowless lighting. Small clean sans-serif labels for product name and price. "
            "Professional but approachable. Suitable for multi-product carousel slides. Consistent spacing and alignment."
        ),
    },

    "lifestyle_natural": {
        "name": "Lifestyle Natural",
        "description": "Candid, authentic, in-context. Products in real life.",
        "industry_tags": ["fashion_ecommerce", "food_beverage", "beauty_wellness", "fitness_gym", "general_other"],
        "prompt_fragment": (
            "Lifestyle photography in natural settings. Product shown in use or in an authentic real-life context. "
            "Natural daylight, preferably golden hour or soft window light. "
            "Shallow depth of field with subject in focus, background softly blurred. "
            "Warm natural colour grading. No heavy text overlay. Candid, unposed feel. "
            "The product is part of a moment, not the centre of a studio."
        ),
    },

    "high_contrast_drama": {
        "name": "High Contrast Drama",
        "description": "Dark backgrounds, dramatic lighting, theatre-level intensity.",
        "industry_tags": ["fashion_ecommerce", "events_entertainment", "fitness_gym"],
        "prompt_fragment": (
            "Dramatic chiaroscuro photography. Very dark or black background. "
            "Single strong directional light source creating deep shadows and bright highlights. "
            "High contrast, low key lighting. Subject emerges from darkness. "
            "Typography in white or single bright accent colour. Theatrical and cinematic. Fine art photography quality."
        ),
    },

    # ── Food & Beverage ──────────────────────────────────────────────────────

    "overhead_feast": {
        "name": "Overhead Feast",
        "description": "Top-down spread. Rustic surface. Abundance.",
        "industry_tags": ["food_beverage"],
        "prompt_fragment": (
            "Overhead flat-lay food photography. Shot directly from above. "
            "Rustic wooden table or marble surface as base. Multiple dishes, ingredients, and utensils "
            "arranged artfully with intentional negative space. Warm natural lighting from north-facing window. "
            "Rich saturated food colours. Herbs, spices, and scattered ingredients as styling elements. "
            "Convivial, abundant, sharing-focused."
        ),
    },

    "dark_moody_food": {
        "name": "Dark & Moody Food",
        "description": "Dramatic. Premium. Chef-quality presentation.",
        "industry_tags": ["food_beverage"],
        "prompt_fragment": (
            "Dark food photography style. Deep charcoal, slate, or black background and surfaces. "
            "Single dish as hero, styled with precision. Dramatic side lighting with visible light falloff. "
            "Rich deep colours: mahogany sauces, deep greens, burnished golds. Minimal props. "
            "Typography in thin gold or cream serif font. Fine dining and premium brand feel."
        ),
    },

    "bright_fresh": {
        "name": "Bright & Fresh",
        "description": "High-key, clean, healthy vibes. Lots of white.",
        "industry_tags": ["food_beverage", "beauty_wellness", "general_other"],
        "prompt_fragment": (
            "High-key bright food photography. White or very light backgrounds and surfaces. "
            "Abundant natural light with minimal shadows. Vibrant food colours pop against the clean background. "
            "Fresh ingredients: greens, citrus, herbs prominently visible. Clean sans-serif typography. "
            "Healthy, fresh, approachable energy. Brunch-menu aesthetic."
        ),
    },

    "street_food_energy": {
        "name": "Street Food Energy",
        "description": "Handheld, outdoor, messy, real. Authentic energy.",
        "industry_tags": ["food_beverage", "events_entertainment"],
        "prompt_fragment": (
            "Street food documentary-style photography. Food held in hand or shown being prepared at a stall. "
            "Outdoor natural light, possibly harsh midday sun with real shadows. "
            "Slightly messy, unpolished plating. Smoke, steam, or motion blur for dynamism. "
            "Bold chunky sans-serif typography. Saturated warm colours. Authentic, not styled. The anti-studio look."
        ),
    },

    "menu_board": {
        "name": "Menu Board",
        "description": "Practical. Prices visible. Clear layout for ordering.",
        "industry_tags": ["food_beverage"],
        "prompt_fragment": (
            "Restaurant menu board style layout. Structured grid with clear sections. "
            "Each item has: photo (small, square), name (bold), description (small), and price (prominent). "
            "Dark background with cream or white text for readability. "
            "Subtle food photography as background at very low opacity. Practical, scannable, designed for someone deciding what to order."
        ),
    },

    "rustic_warmth": {
        "name": "Rustic Warmth",
        "description": "Wooden textures, earthy tones, handcraft feel.",
        "industry_tags": ["food_beverage", "general_other"],
        "prompt_fragment": (
            "Rustic artisanal food photography. Warm earth-tone colour palette: browns, ambers, creams, forest greens. "
            "Textured surfaces: reclaimed wood, linen cloth, terracotta. Soft warm lighting with gentle shadows. "
            "Hand-lettered or rough serif typography evoking chalkboard or hand-painted signs. "
            "Artisan, homemade, craft-focused aesthetic. Farm-to-table energy."
        ),
    },

    "vibrant_tropical": {
        "name": "Vibrant Tropical",
        "description": "Bold colours, tropical ingredients, celebration energy.",
        "industry_tags": ["food_beverage", "events_entertainment", "general_other"],
        "prompt_fragment": (
            "Vibrant tropical colour palette. Bright saturated colours: mango orange, lime green, hibiscus pink, ocean blue. "
            "Bold graphic elements: colour blocks, geometric shapes, tropical leaf patterns. "
            "Playful rounded sans-serif typography. Energetic composition with elements breaking the frame. "
            "Carnival, celebration, summer-party energy. Maximalist but organised."
        ),
    },

    "minimalist_plating": {
        "name": "Minimalist Plating",
        "description": "Fine dining. Single plate. Lots of negative space.",
        "industry_tags": ["food_beverage"],
        "prompt_fragment": (
            "Fine dining plating photography. Single plate or bowl as sole subject, centered with vast negative space. "
            "Neutral background: warm grey, soft linen, or brushed concrete. Overhead or 45-degree angle. "
            "Minimal garnish placed with tweezers-level precision. Soft diffused lighting. "
            "No text overlay unless explicitly requested. The food speaks for itself."
        ),
    },

    # ── Fintech, SaaS & Tech ─────────────────────────────────────────────────

    "corporate_gradient": {
        "name": "Corporate Gradient",
        "description": "Smooth gradients, professional, trust. The LinkedIn standard.",
        "industry_tags": ["fintech_saas_tech", "education_consulting", "general_other"],
        "prompt_fragment": (
            "Professional corporate graphic with smooth gradient background. "
            "Gradient colours: deep blue to purple, teal to blue, or dark navy to medium blue. "
            "Clean sans-serif typography in white, centered or left-aligned. "
            "Subtle geometric shapes (circles, lines, grids) as decorative elements at low opacity. "
            "Device mockups or abstract data visualisation elements. Enterprise-grade, trustworthy, modern. No playfulness."
        ),
    },

    "data_visual": {
        "name": "Data Visual",
        "description": "Charts and numbers as design. For data-driven brands.",
        "industry_tags": ["fintech_saas_tech", "education_consulting"],
        "prompt_fragment": (
            "Data-driven infographic style. Key metric or statistic displayed as the hero element: "
            "large bold number with unit. Supporting mini-charts, progress bars, or comparison graphics. "
            "Clean grid-based layout. Monochrome base with one accent colour for data highlights. "
            "Sans-serif typography only. Dashboard aesthetic. The data IS the design."
        ),
    },

    "trust_builder": {
        "name": "Trust Builder",
        "description": "Real people, real photography. For brands that need credibility.",
        "industry_tags": ["fintech_saas_tech", "real_estate", "education_consulting", "general_other"],
        "prompt_fragment": (
            "Professional corporate photography. Real people in business settings: meetings, handshakes, "
            "collaborative work, presentations. Diverse representation. Warm but professional lighting. "
            "Slightly warm colour grading. Clean sans-serif typography overlaid with semi-transparent dark bar "
            "for readability. Trust, competence, human connection. Not stock-photo generic — authentic and specific."
        ),
    },

    "minimal_tech": {
        "name": "Minimal Tech",
        "description": "Apple-inspired. Whitespace. Precision.",
        "industry_tags": ["fintech_saas_tech", "education_consulting"],
        "prompt_fragment": (
            "Ultra-minimal tech aesthetic inspired by Apple design language. Vast white or very light grey space. "
            "Thin light-weight sans-serif typography. Single product or concept as the focal point with extreme negative space. "
            "Subtle shadows and gradients. No decorative elements. Precision, restraint, sophistication. "
            "Every element earns its place."
        ),
    },

    "bold_statement": {
        "name": "Bold Statement",
        "description": "Text-forward. One big idea. Maximum impact.",
        "industry_tags": ["fintech_saas_tech", "education_consulting", "fitness_gym", "general_other"],
        "prompt_fragment": (
            "Text-dominant motivational or statement graphic. Large bold statement or quote as the entire design. "
            "Background: solid colour, subtle gradient, or dark texture. Typography fills 70%+ of the frame. "
            "Mixed weights (one word bold, rest light) for emphasis hierarchy. Minimal or no imagery. "
            "The words ARE the visual. TED-talk-slide aesthetic."
        ),
    },

    "dark_mode_pro": {
        "name": "Dark Mode Pro",
        "description": "Dark backgrounds, glowing accents. For developer-adjacent brands.",
        "industry_tags": ["fintech_saas_tech"],
        "prompt_fragment": (
            "Dark mode UI-inspired aesthetic. Near-black background. "
            "Subtle glowing accent elements in electric blue, cyan, or green. "
            "Code-editor-inspired monospace typography for data points. Thin neon borders and divider lines. "
            "Glassmorphism elements with frosted transparency. Developer, hacker, cutting-edge tech aesthetic."
        ),
    },

    "isometric_3d": {
        "name": "Isometric 3D",
        "description": "Stylised 3D illustrations. For abstract concepts.",
        "industry_tags": ["fintech_saas_tech", "education_consulting"],
        "prompt_fragment": (
            "Isometric 3D illustration style. Clean geometric shapes rendered in a consistent isometric perspective. "
            "Soft shadows and gradients giving depth. Pastel or muted colour palette with one vibrant accent. "
            "Objects representing abstract concepts: buildings for growth, gears for process, graphs for data. "
            "Clean sans-serif labels. Friendly and explanatory."
        ),
    },

    "clean_startup": {
        "name": "Clean Startup",
        "description": "Approachable, modern, fresh. For early-stage brands.",
        "industry_tags": ["fintech_saas_tech", "education_consulting", "general_other"],
        "prompt_fragment": (
            "Modern startup aesthetic. Light backgrounds with a single accent colour from brand palette. "
            "Rounded UI elements and card-based layouts. Friendly sans-serif typography (Inter, Poppins, Urbanist style). "
            "Abstract blob shapes or wavy lines as subtle decorative elements. "
            "Screenshots or device mockups showing the product. Approachable, optimistic, forward-looking."
        ),
    },

    # ── Beauty & Wellness ────────────────────────────────────────────────────

    "glow_up": {
        "name": "Glow Up",
        "description": "Warm golden lighting, dewy skin, aspirational beauty close-ups.",
        "industry_tags": ["beauty_wellness"],
        "prompt_fragment": (
            "Soft glowing beauty photography. Close-up portrait with warm golden-hour backlighting creating "
            "a luminous halo effect. Skin appears naturally dewy and radiant with smooth, even texture. "
            "Soft bokeh background in warm amber or blush tones. Thin serif or script typography in gold or champagne. "
            "Aspirational but achievable beauty ideal. Beauty editorial quality without heavy retouching."
        ),
    },

    "soft_pastel": {
        "name": "Soft Pastel",
        "description": "Delicate pastels, airy gradients, gentle feminine energy.",
        "industry_tags": ["beauty_wellness", "general_other"],
        "prompt_fragment": (
            "Soft pastel colour palette: blush pink, lavender, mint, baby blue, and ivory. "
            "Gentle gradient backgrounds blending two pastel tones. Airy, light-filled composition with minimal shadows. "
            "Delicate serif or thin script typography. Floral or botanical accent elements at low opacity. "
            "Beauty brand lookbook quality. Feminine, soft, and approachable without being saccharine."
        ),
    },

    "bold_glam": {
        "name": "Bold Glam",
        "description": "High-glamour beauty, full makeup, confident and striking.",
        "industry_tags": ["beauty_wellness", "fashion_ecommerce"],
        "prompt_fragment": (
            "High-glamour beauty photography. Bold full-coverage makeup with saturated lip colours and dramatic eye looks. "
            "Dramatic studio lighting with strong catchlights. Rich jewel-tone or deep neutral backgrounds. "
            "Confident, direct gaze at camera. Magazine cover quality. Typography in thick serif or metallic sans-serif. "
            "Striking, powerful, unapologetically glamorous."
        ),
    },

    "clean_clinical": {
        "name": "Clean Clinical",
        "description": "Medical-aesthetic trust. Ingredient-forward, science-backed.",
        "industry_tags": ["beauty_wellness"],
        "prompt_fragment": (
            "Medical-aesthetic clinic style. Pure white and light grey colour palette with one soft accent colour. "
            "Clean laboratory-quality lighting with no harsh shadows. Product displayed with clinical precision alongside "
            "key ingredient visuals (molecular diagrams, botanical extracts, droplets). "
            "Thin clean sans-serif typography. Trust, expertise, and ingredient transparency are the message."
        ),
    },

    "natural_organic": {
        "name": "Natural Organic",
        "description": "Earth tones, raw ingredients, botanical handcraft feel.",
        "industry_tags": ["beauty_wellness", "food_beverage"],
        "prompt_fragment": (
            "Earth-toned natural beauty aesthetic. Warm organic palette: terracotta, cream, sage green, and honey. "
            "Raw natural ingredients as props: honey dripping, aloe leaves, coconut halves, dried botanicals. "
            "Linen cloth and wood textures as surfaces. Soft diffused natural light. "
            "Handwritten or rough serif typography evoking artisan labels. Farm-sourced, botanical, zero-waste energy."
        ),
    },

    "editorial_beauty": {
        "name": "Editorial Beauty",
        "description": "Avant-garde beauty. Fashion-magazine artistic direction.",
        "industry_tags": ["beauty_wellness", "fashion_ecommerce"],
        "prompt_fragment": (
            "High-fashion editorial beauty photography. Artistic, conceptual composition that prioritises visual impact over product clarity. "
            "Unexpected colour combinations and dramatic lighting contrasts. Model as art subject, not just product vehicle. "
            "Typography minimal or absent — the image carries the story alone. "
            "Vogue or i-D magazine aesthetic. Bold, experimental, designed to stop the scroll."
        ),
    },

    "before_after": {
        "name": "Before & After",
        "description": "Split-screen transformation. Proof-focused and results-driven.",
        "industry_tags": ["beauty_wellness", "fitness_gym"],
        "prompt_fragment": (
            "Clean split-screen before-and-after layout. Vertical or horizontal divider line splitting the frame precisely in half. "
            "Left side labelled 'Before' in small sans-serif, right side 'After'. "
            "Consistent lighting and framing between both sides. "
            "Result difference is the clear visual hero. Stats or timeline text overlaid in clean sans-serif at bottom. "
            "Clinical, credible, and conversion-focused."
        ),
    },

    # ── Fitness & Gym ────────────────────────────────────────────────────────

    "energy_motion": {
        "name": "Energy & Motion",
        "description": "Dynamic action shots, motion blur, sweat and intensity.",
        "industry_tags": ["fitness_gym"],
        "prompt_fragment": (
            "Dynamic sports action photography. Athlete caught mid-movement with motion blur on extremities "
            "emphasising speed and power. Bright saturated colours: electric orange, vivid yellow, or lime green. "
            "Strong directional lighting creating muscle definition. Bold angled typography at 10–15 degree tilt. "
            "Sweat, dust, or water droplets visible for authenticity. Maximum energy and movement in every frame."
        ),
    },

    "dark_grit": {
        "name": "Dark & Grit",
        "description": "Moody gym photography. Hardcore. No-frills. Raw.",
        "industry_tags": ["fitness_gym"],
        "prompt_fragment": (
            "Dark hardcore gym photography. Moody low-key lighting from a single industrial source. "
            "Concrete walls, metal equipment, chalk dust visible in the air. Desaturated colour grading with heavy contrast. "
            "Distressed or stencil-style typography. Subject shown mid-exertion with visible effort. "
            "Raw, unfiltered, no glamour. The aesthetic of serious athletes who don't care about aesthetics."
        ),
    },

    "transformation": {
        "name": "Transformation",
        "description": "Before/after results. Stats-driven. Proof over aesthetics.",
        "industry_tags": ["fitness_gym", "beauty_wellness"],
        "prompt_fragment": (
            "Results-focused fitness transformation layout. Split panel showing clear physical change. "
            "Same pose, same angle, different body composition. Stats prominently displayed: weight lost, weeks taken, "
            "percentage improvement. Clean divider line with 'Week 1' / 'Week 12' labels. "
            "Neutral background keeping focus on the subject. Credibility and proof are the design goal."
        ),
    },

    "motivational_type": {
        "name": "Motivational Type",
        "description": "One powerful phrase. Dark background. Athletic silhouette.",
        "industry_tags": ["fitness_gym", "education_consulting", "general_other"],
        "prompt_fragment": (
            "Large motivational quote or phrase as the visual centrepiece. "
            "Dark gradient or textured background (concrete, smoke, dark gradient). "
            "Single powerful phrase in massive bold uppercase condensed sans-serif typography. "
            "Athletic silhouette or action shot used as very low opacity background texture. "
            "Minimal colour: monochrome with one strong accent. TED-talk-slide meets gym locker-room poster."
        ),
    },

    "clean_athletic": {
        "name": "Clean Athletic",
        "description": "Nike/Adidas-inspired. Premium sportswear feel. Minimal.",
        "industry_tags": ["fitness_gym", "fashion_ecommerce"],
        "prompt_fragment": (
            "Premium sportswear aesthetic inspired by Nike and Adidas campaigns. "
            "Clean white or light grey background with athlete or product as the sole focus. "
            "Perfect studio lighting revealing fabric texture and product quality. "
            "Minimal typography: one word or tagline in bold sans-serif. "
            "No decorative elements. The product and athlete speak for themselves. Aspirational and premium."
        ),
    },

    # ── Real Estate ──────────────────────────────────────────────────────────

    "property_showcase": {
        "name": "Property Showcase",
        "description": "Wide-angle bright interiors. Blue sky, green lawn. HDR clarity.",
        "industry_tags": ["real_estate"],
        "prompt_fragment": (
            "Professional real estate photography. Wide-angle interior or exterior shot with HDR-style clarity and brightness. "
            "Deep blue sky, well-manicured lawn, clean architectural lines. "
            "Interior shots show warm inviting lighting with natural light flooding through windows. "
            "Clean info bar at the bottom: bedroom count, bathrooms, price, and neighbourhood. "
            "Sans-serif typography in dark overlay bar. The property looks its absolute best."
        ),
    },

    "luxury_listing": {
        "name": "Luxury Listing",
        "description": "Twilight exteriors. Gold serif type. Exclusivity.",
        "industry_tags": ["real_estate"],
        "prompt_fragment": (
            "Luxury property listing aesthetic. Twilight exterior photography: warm interior lights glowing against "
            "deep blue dusk sky. Gold or champagne serif typography for property name and key details. "
            "Dark overlay at bottom for text readability. Premium finishes highlighted in close-up detail shots. "
            "Exclusivity, aspiration, and discretion in every element. Sotheby's-level presentation."
        ),
    },

    "neighbourhood_life": {
        "name": "Neighbourhood Life",
        "description": "Community and lifestyle. Sell the area, not just the property.",
        "industry_tags": ["real_estate"],
        "prompt_fragment": (
            "Community lifestyle photography emphasising neighbourhood quality of life. "
            "Families walking, children playing, cafés and parks, tree-lined streets. "
            "Warm natural golden-hour lighting. Candid, authentic, unposed moments. "
            "Sans-serif caption text in clean overlay. The message is: this is where you want to live. "
            "Human connection and community belonging as the primary visual story."
        ),
    },

    "blueprint_modern": {
        "name": "Blueprint Modern",
        "description": "Architectural line drawings. Technical precision. Modern.",
        "industry_tags": ["real_estate"],
        "prompt_fragment": (
            "Architectural blueprint aesthetic. Deep navy or dark slate background with white technical line drawings. "
            "Floor plan outlines, elevation sketches, and site layouts as decorative graphic elements. "
            "Clean technical sans-serif typography with precise grid-based layout. "
            "Property dimensions or room labels incorporated as design elements. "
            "Modern, precise, and developer-grade professional presentation."
        ),
    },

    "aerial_clean": {
        "name": "Aerial Clean",
        "description": "Drone-style overhead photography. Wide context. Clean info overlay.",
        "industry_tags": ["real_estate"],
        "prompt_fragment": (
            "Drone-style aerial or high-angle photography of property and surroundings. "
            "Wide contextual view showing the property within its neighbourhood, proximity to landmarks, roads, and green spaces. "
            "Clear blue sky, sharp shadow detail from above. "
            "Clean white semi-transparent overlay at bottom with property details in dark sans-serif. "
            "Conveys location value and neighbourhood context clearly."
        ),
    },

    # ── Education & Consulting ───────────────────────────────────────────────

    "warm_professional": {
        "name": "Warm Professional",
        "description": "Approachable expertise. Warm tones, real people, credibility.",
        "industry_tags": ["education_consulting", "general_other"],
        "prompt_fragment": (
            "Warm professional photography blending credibility with approachability. "
            "Expert or educator photographed in a natural work setting: desk, whiteboard, or classroom. "
            "Warm amber-toned lighting creating an inviting, trustworthy atmosphere. "
            "Slight smile, engaged body language. Clean sans-serif typography on semi-transparent warm overlay. "
            "The visual says: this person is accomplished AND easy to work with."
        ),
    },

    "authority_editorial": {
        "name": "Authority Editorial",
        "description": "Magazine-style expert portrait. Gravitas and credibility.",
        "industry_tags": ["education_consulting"],
        "prompt_fragment": (
            "Business authority editorial photography. Executive or thought-leader portrait in dramatic studio lighting. "
            "Strong three-point lighting creating depth and gravitas. Neutral or dark background. "
            "Subject in business formal attire with confident, direct gaze. "
            "Typography in bold serif or heavy sans-serif conveying weight and authority. "
            "Harvard Business Review or Forbes contributor aesthetic."
        ),
    },

    # ── Events & Entertainment ───────────────────────────────────────────────

    "festival_energy": {
        "name": "Festival Energy",
        "description": "Concert poster aesthetic. Explosive. Layered. Loud.",
        "industry_tags": ["events_entertainment"],
        "prompt_fragment": (
            "Concert and festival poster aesthetic. Multiple layered elements: artist photo cutouts, "
            "abstract geometric shapes, texture overlays. Explosive typographic hierarchy with headline act massive, "
            "supporting acts smaller. Date, venue, and ticket info prominently placed. "
            "Neon, metallic, or gradient colour palette. High energy. "
            "Reminiscent of Coachella, Afropunk, or Felabration poster design."
        ),
    },

    # ── General / Cross-industry ─────────────────────────────────────────────

    "warm_professional_general": {
        "name": "Warm Professional",
        "description": "Approachable and credible. Works for any service brand.",
        "industry_tags": ["general_other"],
        "prompt_fragment": (
            "Warm professional photography blending credibility with approachability. "
            "People shown in natural work or community settings with warm amber-toned lighting. "
            "Slightly warm colour grading. Clean sans-serif typography on semi-transparent overlay. "
            "Trustworthy, competent, and human. Works for any brand that needs to build confidence."
        ),
    },
}


# ---------------------------------------------------------------------------
# Industry → style mapping
# ---------------------------------------------------------------------------

INDUSTRY_STYLE_MAP: Dict[str, List[str]] = {
    "fashion_ecommerce": [
        "street_editorial", "clean_luxe", "neon_pop", "afro_glam",
        "minimal_studio", "bold_loud", "vintage_film", "catalogue_clean",
        "lifestyle_natural", "high_contrast_drama",
    ],
    "food_beverage": [
        "overhead_feast", "dark_moody_food", "bright_fresh", "street_food_energy",
        "menu_board", "rustic_warmth", "bold_loud", "vibrant_tropical", "minimalist_plating",
    ],
    "fintech_saas_tech": [
        "corporate_gradient", "data_visual", "trust_builder", "minimal_tech",
        "bold_statement", "dark_mode_pro", "isometric_3d", "clean_startup",
    ],
    "beauty_wellness": [
        "glow_up", "soft_pastel", "bold_glam", "clean_clinical",
        "natural_organic", "editorial_beauty", "neon_pop", "lifestyle_natural", "before_after",
    ],
    "real_estate": [
        "property_showcase", "luxury_listing", "neighbourhood_life",
        "blueprint_modern", "aerial_clean", "trust_builder", "bold_statement",
    ],
    "fitness_gym": [
        "energy_motion", "dark_grit", "bold_loud", "transformation",
        "clean_athletic", "neon_pop", "motivational_type", "lifestyle_natural",
    ],
    "education_consulting": [
        "trust_builder", "clean_startup", "bold_statement", "data_visual",
        "warm_professional", "minimal_tech", "authority_editorial",
    ],
    "events_entertainment": [
        "neon_pop", "bold_loud", "high_contrast_drama", "afro_glam",
        "vibrant_tropical", "street_food_energy", "festival_energy",
    ],
    "general_other": [
        "bold_loud", "clean_startup", "lifestyle_natural", "minimal_studio",
        "trust_builder", "vibrant_tropical", "warm_professional", "afro_glam",
        "bright_fresh", "corporate_gradient",
    ],
}

# Normalise common industry string variants to our canonical slugs
_INDUSTRY_ALIASES: Dict[str, str] = {
    "fashion": "fashion_ecommerce",
    "ecommerce": "fashion_ecommerce",
    "e-commerce": "fashion_ecommerce",
    "food": "food_beverage",
    "beverage": "food_beverage",
    "restaurant": "food_beverage",
    "fintech": "fintech_saas_tech",
    "saas": "fintech_saas_tech",
    "tech": "fintech_saas_tech",
    "technology": "fintech_saas_tech",
    "beauty": "beauty_wellness",
    "wellness": "beauty_wellness",
    "health": "beauty_wellness",
    "real estate": "real_estate",
    "property": "real_estate",
    "fitness": "fitness_gym",
    "gym": "fitness_gym",
    "sport": "fitness_gym",
    "sports": "fitness_gym",
    "education": "education_consulting",
    "consulting": "education_consulting",
    "coaching": "education_consulting",
    "events": "events_entertainment",
    "entertainment": "events_entertainment",
    "event": "events_entertainment",
}


def _canonical_industry(raw: str) -> str:
    """Map a raw industry string to a canonical INDUSTRY_STYLE_MAP key.

    Handles compound values like 'Tech & SaaS', 'Food & Beverage' via partial keyword match.
    """
    key = raw.lower().strip()
    if key in INDUSTRY_STYLE_MAP:
        return key
    if key in _INDUSTRY_ALIASES:
        return _INDUSTRY_ALIASES[key]
    # Partial keyword match — catches compound industry strings
    for alias, canonical in _INDUSTRY_ALIASES.items():
        if alias in key:
            return canonical
    return "general_other"


def get_styles_for_industry(industry: str) -> List[str]:
    """Return the list of style slugs shown during onboarding for this industry."""
    return INDUSTRY_STYLE_MAP.get(_canonical_industry(industry), INDUSTRY_STYLE_MAP["general_other"])


def get_style(slug: str) -> Optional[Dict[str, Any]]:
    """Look up a style by slug. Returns None if not found."""
    return STYLES.get(slug)


def get_prompt_fragment(slug: str) -> str:
    """Return the prompt fragment for a style slug, or empty string if not found."""
    style = STYLES.get(slug)
    return style["prompt_fragment"] if style else ""


def pick_next_style(
    style_selections: List[str],
    rotation_index: int,
    industry: str = "",
    style_prompt_fragments: Optional[List[str]] = None,
) -> tuple[str, str, int]:
    """
    Select the next style in the rotation and return:
      (slug, prompt_fragment, next_rotation_index)

    Uses stored prompt_fragments when available (PRD DEV NOTE: copy fragment at selection
    time so library updates don't affect existing users). Falls back to live lookup.

    If the user has no selections yet, auto-assign the first style for their industry.
    """
    if not style_selections:
        industry_styles = get_styles_for_industry(industry)
        slug = industry_styles[0] if industry_styles else "bold_loud"
        fragment = get_prompt_fragment(slug)
        return slug, fragment, 0

    idx = rotation_index % len(style_selections)
    slug = style_selections[idx]

    # Prefer the fragment stored at selection time over a live library lookup
    if style_prompt_fragments and idx < len(style_prompt_fragments) and style_prompt_fragments[idx]:
        fragment = style_prompt_fragments[idx]
    else:
        fragment = get_prompt_fragment(slug)

    next_index = (idx + 1) % len(style_selections)
    return slug, fragment, next_index

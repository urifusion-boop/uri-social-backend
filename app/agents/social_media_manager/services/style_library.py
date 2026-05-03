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

    # ── SaaS / Tech / Fintech (Expanded 2026) ────────────────────────────────

    "saas_dashboard_hero": {
        "name": "Dashboard Hero",
        "description": "Your product IS the visual. Clean UI screenshots as the centrepiece.",
        "industry_tags": ["fintech_saas_tech", "general_other"],
        "prompt_fragment": (
            "Professional product screenshot showcase on a clean gradient background. The hero element is a device mockup "
            "(laptop, phone, or tablet) displaying the product's actual UI or a stylised representation of it. Subtle shadow "
            "beneath the device for depth. Clean sans-serif typography above or below the device in white or dark text. "
            "Background gradient uses brand primary and secondary colours blending smoothly. No decorative clutter. "
            "The product screenshot is the entire visual story. Apple-keynote-presentation quality."
        ),
    },

    "saas_metric_spotlight": {
        "name": "Metric Spotlight",
        "description": "One big number tells the whole story. Data as design.",
        "industry_tags": ["fintech_saas_tech", "education_consulting", "general_other"],
        "prompt_fragment": (
            "Data-forward graphic with a single large metric as the hero element: big bold number (72pt+) in brand accent colour, "
            "centered vertically. Unit or label directly below in smaller text. Supporting context in 1–2 lines of small text at the bottom. "
            "Background: solid dark colour or very subtle gradient. No imagery — the number IS the image. Inspired by investor pitch decks "
            "and annual report covers. The typography should be monospaced or geometric sans-serif for the number, clean sans-serif for labels."
        ),
    },

    "saas_comparison_grid": {
        "name": "Comparison Grid",
        "description": "Side-by-side visual proof. Before/after, us vs. them.",
        "industry_tags": ["fintech_saas_tech", "general_other"],
        "prompt_fragment": (
            "Clean two-column or split-screen comparison layout. Left side labelled 'Before' or 'Other tools' with muted/desaturated "
            "colours and a subtle red or grey tint. Right side labelled 'After' or 'With [Brand]' with vibrant, saturated brand colours "
            "and a green checkmark or glow. Clear divider line (solid or dashed) separating the halves. Clean sans-serif labels. "
            "Each side shows either a UI screenshot, a metric, or an icon-based feature list. The visual bias should obviously favour the right side."
        ),
    },

    "saas_blog_header": {
        "name": "Blog Header",
        "description": "Clean editorial imagery for thought leadership content.",
        "industry_tags": ["fintech_saas_tech", "education_consulting", "general_other"],
        "prompt_fragment": (
            "Wide-format editorial blog header image. Left-aligned bold headline text (2–4 words max) with a complementary abstract "
            "illustration or subtle photography on the right. Brand primary colour as an accent bar or background block on one side. "
            "Clean whitespace separating text from imagery. Typography: bold geometric sans-serif for the headline, thin sans-serif for "
            "any subtitle. Feels like a premium tech publication cover: The Verge, TechCrunch, or Wired. No stock photography — abstract "
            "shapes, gradients, or stylised icons preferred."
        ),
    },

    "saas_feature_card": {
        "name": "Feature Card",
        "description": "One feature, one icon, one message. Modular and clean.",
        "industry_tags": ["fintech_saas_tech", "general_other"],
        "prompt_fragment": (
            "Single-feature spotlight card on a clean background. Large custom icon or illustration (line-art style, 2px stroke, brand accent colour) "
            "centered or left-aligned. Feature name in bold sans-serif below the icon. One-line description in lighter weight text beneath. "
            "Background: soft gradient, solid light colour, or white with a subtle brand-coloured border. Rounded corners (16px) on the overall card shape. "
            "Designed to work as a standalone post or as one slide in a carousel where each slide highlights a different feature."
        ),
    },

    "saas_abstract_gradient": {
        "name": "Abstract Gradient",
        "description": "Ambient, atmospheric, modern. When you don't need a screenshot.",
        "industry_tags": ["fintech_saas_tech", "beauty_wellness", "general_other"],
        "prompt_fragment": (
            "Full-bleed abstract gradient background with smooth colour transitions using brand palette colours. Organic flowing shapes: "
            "blurred orbs, mesh gradients, or aurora-like colour waves. Typography floats on the gradient: large bold statement text in white "
            "or very light colour with subtle text shadow for legibility. No imagery, no icons, no screenshots. The mood is ambient, premium, "
            "and contemplative. Inspired by Stripe's and Linear's marketing visuals. The gradient itself IS the design."
        ),
    },

    "saas_code_snippet": {
        "name": "Code Snippet",
        "description": "Developer-facing. Dark mode. Technical credibility.",
        "industry_tags": ["fintech_saas_tech"],
        "prompt_fragment": (
            "Dark code editor aesthetic (#0D1117 or #1E1E1E background). Featured code snippet rendered in monospace font (Fira Code or "
            "JetBrains Mono style) with syntax highlighting: strings in green, keywords in purple/blue, comments in grey. Line numbers visible "
            "on the left margin. Terminal-style header bar with coloured dots (red/yellow/green) at the top. Brand logo or product name in small "
            "text at the bottom. Below or beside the code: a one-line plain-language explanation in clean sans-serif. Appeals to developers and "
            "technical audiences."
        ),
    },

    "saas_testimonial_card": {
        "name": "Testimonial Card",
        "description": "Social proof that looks designed, not screenshotted.",
        "industry_tags": ["fintech_saas_tech", "education_consulting", "general_other"],
        "prompt_fragment": (
            "Professional testimonial card with large quotation marks (\"\") as decorative elements in brand accent colour at 15% opacity behind the text. "
            "Customer quote in medium-weight serif or sans-serif, centered. Customer name and title below the quote in bold, with company logo (small, greyscale) "
            "beneath. Background: soft brand colour tint or clean white with a subtle coloured border. Optional: small 5-star rating above the quote. "
            "Avatar circle photo of the customer in top-centre or left-aligned. Premium, not templated."
        ),
    },

    "saas_changelog_card": {
        "name": "Changelog Card",
        "description": "Ship fast, show fast. Clean update announcements.",
        "industry_tags": ["fintech_saas_tech"],
        "prompt_fragment": (
            "Clean update announcement card with a coloured category badge at the top: 'NEW' in green, 'IMPROVED' in blue, 'FIXED' in yellow. "
            "Feature name as bold headline below the badge. One-line description. Optional: small product UI screenshot showing the change, displayed "
            "in a subtle device frame or browser window. Background: white or very light grey with a thin brand-coloured top border. Version number or "
            "date in small grey text at the bottom. Developer changelog aesthetic."
        ),
    },

    "saas_infographic_flow": {
        "name": "Infographic Flow",
        "description": "Process visualisation. Steps and flows made visual.",
        "industry_tags": ["fintech_saas_tech", "education_consulting", "general_other"],
        "prompt_fragment": (
            "Clean infographic-style process diagram. 3–5 numbered steps arranged vertically or horizontally, connected by arrows or dotted lines. "
            "Each step has a circular icon (line-art, brand accent colour) and a short label (2–4 words). The flow direction is clear and visual. "
            "Background: white or very light brand tint. Typography: clean geometric sans-serif. Colours: primary brand colour for the step numbers/icons, "
            "grey for the connecting lines, dark text for labels. Educational and structural. Think process.st or Notion-style diagrams."
        ),
    },

    "saas_dark_announcement": {
        "name": "Dark Mode Announcement",
        "description": "Premium dark background. For important news that demands attention.",
        "industry_tags": ["fintech_saas_tech", "events_entertainment", "general_other"],
        "prompt_fragment": (
            "Full dark background (#0A0A0A or #111827) with a single dramatic element: either a glowing product icon, a large embossed number, or a bold "
            "text statement. Subtle animated-looking light effects: a soft glow, lens flare, or spotlight illuminating the text from behind. Typography: "
            "large, bold, white or light brand colour. Minimal supporting text. No images or screenshots. The darkness creates weight and importance. "
            "Inspired by Apple event invitations and Linear's launch pages. Reserve for major announcements."
        ),
    },

    "saas_social_proof_wall": {
        "name": "Social Proof Wall",
        "description": "Logo grids and numbers. Trust at scale.",
        "industry_tags": ["fintech_saas_tech", "education_consulting", "general_other"],
        "prompt_fragment": (
            "Clean grid of customer logos arranged in a 3×4 or 4×3 matrix on a white or very light background. All logos rendered in greyscale for visual "
            "consistency. Above the grid: a bold headline like 'Trusted by 500+ companies' in dark text. Below the grid: a key metric or social proof number "
            "(e.g., '$2.4B processed'). The logos should feel deliberately curated, not crammed. Generous spacing between logos. Optional: a subtle brand-coloured "
            "underline beneath the headline."
        ),
    },

    # ── Product-Based Business (Expanded 2026) ───────────────────────────────

    "prod_hero_pedestal": {
        "name": "Hero Pedestal",
        "description": "Product on a stage. Elevated. Premium. The Apple approach.",
        "industry_tags": ["fashion_ecommerce", "beauty_wellness", "general_other"],
        "prompt_fragment": (
            "Single product centered on a clean surface or floating against a solid-colour background. Dramatic studio lighting from above-left creating a "
            "defined shadow beneath. The product occupies 40–60% of the frame with generous negative space. No text unless explicitly needed — the product IS "
            "the message. Background colour pulled from brand palette (muted version). Subtle gradient on the surface beneath the product suggesting a platform "
            "or pedestal. Shot at slight low angle for a heroic perspective. Luxury product photography quality."
        ),
    },

    "prod_flat_lay_curated": {
        "name": "Curated Flat-Lay",
        "description": "Top-down arrangement. Intentional, styled, Instagram-perfect.",
        "industry_tags": ["fashion_ecommerce", "beauty_wellness", "food_beverage", "general_other"],
        "prompt_fragment": (
            "Overhead flat-lay photography of product arranged with complementary lifestyle props on a textured surface (marble, linen, wood, or concrete). "
            "The product is the dominant element, surrounded by 3–5 smaller styling props that create context: coffee cup, plant sprig, fabric swatch, tool, "
            "or ingredient. Everything arranged with geometric precision and intentional negative space. Soft even lighting with minimal shadows. Warm natural "
            "colour grading. The arrangement tells a story about the product's lifestyle context without words."
        ),
    },

    "prod_unboxing_reveal": {
        "name": "Unboxing Reveal",
        "description": "The packaging IS the experience. Premium unboxing energy.",
        "industry_tags": ["fashion_ecommerce", "beauty_wellness", "general_other"],
        "prompt_fragment": (
            "Unboxing-style product photography showing the product emerging from or arranged with its packaging. Box slightly open with tissue paper or "
            "branded wrapping visible. The product partially revealed, creating anticipation. Dramatic lighting highlighting the packaging materials and brand "
            "details. Dark or brand-coloured background for contrast. Optional: hands pulling the product from the box for human context. The feeling should be "
            "'this is a gift worth opening.' Focus on packaging quality, materials, and the tactile experience."
        ),
    },

    "prod_ingredient_exploded": {
        "name": "Ingredient Exploded",
        "description": "Show what it's made of. Transparency builds trust.",
        "industry_tags": ["beauty_wellness", "food_beverage", "general_other"],
        "prompt_fragment": (
            "Exploded/deconstructed view showing the product's key ingredients or components arranged around it. Product centered, with raw materials, "
            "ingredients, or parts floating or arranged in a circle/arc around it. Clean background (white or light). Each ingredient may have a small label "
            "or line pointing to it. Bright, clinical lighting that makes every element look fresh and identifiable. Inspired by cosmetics and food brands that "
            "show 'what's inside.' Scientific yet approachable. Transparency and quality as the message."
        ),
    },

    "prod_lifestyle_in_use": {
        "name": "Lifestyle In-Use",
        "description": "Product in its natural habitat. Real context, real life.",
        "industry_tags": ["fashion_ecommerce", "beauty_wellness", "fitness_gym", "general_other"],
        "prompt_fragment": (
            "Environmental product photography showing the product being used or displayed in a real-life context. A skincare bottle on a bathroom counter, "
            "a tool in a workshop, food on a dining table, electronics on a desk. Natural daylight or warm interior lighting. Shallow depth of field with the "
            "product in sharp focus and background softly blurred. Warm, inviting colour grading. No text overlay. Candid and aspirational simultaneously. "
            "The viewer should think 'I want my life to look like this' with the product naturally part of that scene."
        ),
    },

    "prod_colour_swatch": {
        "name": "Colour Swatch",
        "description": "Show the range. Multiple variants, one clean layout.",
        "industry_tags": ["fashion_ecommerce", "beauty_wellness", "general_other"],
        "prompt_fragment": (
            "Clean product variant display showing multiple colourways, sizes, or flavours of the same product arranged in a satisfying grid, row, or gradient "
            "sequence. Each variant gets equal visual weight. Background: pure white or light grey for maximum colour accuracy. Even studio lighting with no "
            "colour cast. Small clean labels beneath each variant (colour name, flavour, size). The visual rhythm of the arrangement should be satisfying and "
            "orderly. Inspired by Pantone swatches and paint chip displays. Perfect for product lines with variety."
        ),
    },

    "prod_scale_context": {
        "name": "Scale Context",
        "description": "How big is it actually? Show it next to something familiar.",
        "industry_tags": ["fashion_ecommerce", "general_other"],
        "prompt_fragment": (
            "Product photographed next to a common object for scale reference: a hand, a coin, a phone, a ruler, a cup. Clean background, even lighting. "
            "The scale relationship should be immediately obvious. Clean informational typography showing dimensions if relevant. Not artistic — practical and "
            "informative. The viewer's primary question ('how big is this?') is answered instantly. Useful for online sellers where size is a common purchase "
            "barrier. E-commerce practical, not editorial."
        ),
    },

    "prod_process_bts": {
        "name": "Process / Behind the Scenes",
        "description": "How it's made. Craft and care visible.",
        "industry_tags": ["fashion_ecommerce", "food_beverage", "beauty_wellness", "general_other"],
        "prompt_fragment": (
            "Behind-the-scenes manufacturing or crafting photography. Raw materials being transformed into finished product. Workshop, kitchen, factory, or "
            "studio environment. Warm directional lighting. Visible hands at work. Slightly gritty, authentic feel — not over-polished. Subtle film grain at "
            "low opacity for an artisanal feel. Text overlay (if any) uses hand-lettered or typewriter-style font. The story is 'real people make this with care.' "
            "Builds trust through transparency and craftsmanship. Documentary style."
        ),
    },

    "prod_seasonal_collection": {
        "name": "Seasonal Collection",
        "description": "Holiday and seasonal launches. Festive but on-brand.",
        "industry_tags": ["fashion_ecommerce", "food_beverage", "beauty_wellness", "general_other"],
        "prompt_fragment": (
            "Seasonal themed product arrangement with holiday or seasonal styling cues: warm tones and dry leaves for autumn, cool blues and silver for "
            "holiday/winter, bright pastels and flowers for spring, vivid saturated colours for summer. Product remains the hero, but the surrounding styling "
            "creates seasonal context. Typography uses elegant serif or script fonts for the seasonal label. Background textures match the season: wood grain, "
            "snow texture, floral patterns, sand. Festive without being tacky. The product feels 'of the moment.'"
        ),
    },

    "prod_comparison_duo": {
        "name": "Comparison Duo",
        "description": "Two products side by side. Let the customer choose.",
        "industry_tags": ["fashion_ecommerce", "beauty_wellness", "general_other"],
        "prompt_fragment": (
            "Two-product comparison layout with clean vertical divider. Each product gets exactly half the frame with identical lighting, angle, and background "
            "treatment. Product name and key differentiator (e.g., size, price, feature) labelled cleanly beneath each. Neutral background that doesn't favour "
            "either option. The visual says 'you decide.' Optional: a subtle 'BEST SELLER' or 'NEW' badge on one product. Clean, e-commerce, decision-making "
            "aesthetic. Works perfectly for carousel slides comparing options."
        ),
    },

    "prod_360_angles": {
        "name": "Multi-Angle Grid",
        "description": "Every angle. Four views. Full product clarity.",
        "industry_tags": ["fashion_ecommerce", "general_other"],
        "prompt_fragment": (
            "Four-panel grid showing the same product from four different angles: front, side, back, and detail close-up. Each panel has identical lighting and "
            "background. Clean white or light grey background. Thin divider lines between panels. Small angle labels optional (FRONT, SIDE, BACK, DETAIL). Studio "
            "product photography quality. The grid communicates thoroughness and confidence — 'we have nothing to hide, here's every angle.' E-commerce standard, "
            "trust-building, practical."
        ),
    },

    "prod_bundle_stack": {
        "name": "Bundle Stack",
        "description": "Multiple products grouped together. The package deal visual.",
        "industry_tags": ["fashion_ecommerce", "beauty_wellness", "food_beverage", "general_other"],
        "prompt_fragment": (
            "Product bundle arrangement with 3–5 products artfully grouped together. Slight overlap and layering creates depth and suggests value. Clean background "
            "with a subtle shadow grounding the arrangement. A banner or badge element ('BUNDLE', 'SAVE 20%', 'STARTER KIT') in brand accent colour. The composition "
            "should feel abundant without being cluttered. Each product remains identifiable. Warm inviting lighting. The visual promise is 'you get all of this.' "
            "Gift-set and value-pack energy."
        ),
    },

    "prod_customer_photo_frame": {
        "name": "Customer Photo Frame",
        "description": "User-generated content elevated. Real customer, branded frame.",
        "industry_tags": ["fashion_ecommerce", "beauty_wellness", "fitness_gym", "general_other"],
        "prompt_fragment": (
            "Customer-submitted photo (or UGC-style photo) displayed within a branded frame or border. The frame uses brand colours, logo, and a small customer "
            "name/handle credit. The photo itself looks authentic and unpolished — real person, real setting, real use. The branded frame elevates it from 'random "
            "repost' to 'curated customer spotlight.' Optional: a quote from the customer overlaid on a brand-coloured bar above or below the photo. Celebrates the "
            "customer while reinforcing brand identity."
        ),
    },

    "prod_price_tag": {
        "name": "Price Tag",
        "description": "Product + price. Clear, direct, ready to buy.",
        "industry_tags": ["fashion_ecommerce", "food_beverage", "general_other"],
        "prompt_fragment": (
            "Product photography with a clean integrated price display. Product occupies 60% of the frame, with the price prominently displayed in a styled tag, "
            "banner, or clean typographic treatment. Price in bold large text with currency symbol. Product name in smaller text above. Optional: a 'SHOP NOW' or "
            "'DIRECT LINK IN BIO' call-to-action at the bottom. Background: solid brand colour or gradient. The post is designed to convert, not just inspire. "
            "Clear, direct, transactional. Instagram shopping post aesthetic."
        ),
    },

    # ── Service-Based Business (Expanded 2026) ───────────────────────────────

    "svc_authority_quote": {
        "name": "Authority Quote",
        "description": "Your words are your product. Make them visual.",
        "industry_tags": ["education_consulting", "general_other"],
        "prompt_fragment": (
            "Bold typographic quote card with the founder's or expert's insight as the hero element. Large quotation marks (\"\") in brand accent colour as "
            "decorative elements, either oversized behind the text at low opacity or small and precise at the start of the quote. The quote text in medium-weight "
            "serif or clean sans-serif, 18–22pt equivalent, with generous line height. Speaker's name, title, and small headshot circle below the quote. Background: "
            "solid dark colour (brand dark) or rich gradient. The post positions the person as a thought leader. LinkedIn executive voice."
        ),
    },

    "svc_tip_carousel": {
        "name": "Tip Carousel",
        "description": "Swipeable knowledge. Each slide is one insight.",
        "industry_tags": ["education_consulting", "fitness_gym", "general_other"],
        "prompt_fragment": (
            "Carousel-format tip slide with a consistent template across multiple slides. Each slide: numbered step or tip in large bold text ('01' in brand colour), "
            "tip headline in bold (1 line), explanation in regular weight (2–3 lines). Clean icon or small illustration accompanying each tip. Consistent background "
            "colour or subtle gradient across all slides for visual continuity when swiping. First slide is the cover: bold title + 'Swipe →' prompt. Last slide is "
            "CTA: 'Follow for more' or 'Book a call.' Clean, educational, Instagram-carousel-native design."
        ),
    },

    "svc_case_study_result": {
        "name": "Case Study Result",
        "description": "Before/after results. Proof that you deliver.",
        "industry_tags": ["education_consulting", "fitness_gym", "general_other"],
        "prompt_fragment": (
            "Results-focused case study card with a clear before/after or metric-transformation layout. Large percentage or number as the hero ('+340%' or '₦4.2M saved') "
            "in brand accent colour. Client name or industry below (anonymised if needed: 'A Lagos-based fintech'). One-line context: 'in 6 months' or 'with our strategy.' "
            "Background: dark or brand-coloured for impact. Optional: a subtle arrow graphic showing upward trajectory. The visual says 'we deliver measurable results' "
            "without showing the client's actual data. Consultant social proof."
        ),
    },

    "svc_framework_diagram": {
        "name": "Framework Diagram",
        "description": "Your methodology, visualised. Intellectual property as content.",
        "industry_tags": ["education_consulting", "general_other"],
        "prompt_fragment": (
            "Clean visual diagram of a proprietary framework, methodology, or process. Concentric circles, a pyramid, a 2×2 matrix, or a cyclical flow diagram with "
            "4–6 labelled stages. Each stage has a clean icon and a 2–3 word label. Brand colours used for the diagram elements. Dark or white background. The framework "
            "name as a bold title above. The visual communicates 'we have a systematic approach' and positions the framework as intellectual property. Management "
            "consulting and strategy firm aesthetic. McKinsey/BCG energy."
        ),
    },

    "svc_headshot_branded": {
        "name": "Branded Headshot",
        "description": "Professional photo meets brand identity. The personal brand builder.",
        "industry_tags": ["education_consulting", "real_estate", "general_other"],
        "prompt_fragment": (
            "Professional headshot of a person (to be composited or AI-generated) on a branded background. Background uses brand primary colour as a solid fill or "
            "gradient, with the person's photo cropped to shoulders-up. Name in bold white text below the photo. Title and company in lighter weight beneath. Optional: "
            "a subtle brand pattern or geometric shape framing the headshot. The style communicates 'professional, approachable, branded.' Suitable for team introductions, "
            "speaking engagement announcements, and personal brand posts."
        ),
    },

    "svc_stat_grid": {
        "name": "Stat Grid",
        "description": "Four numbers. Four proof points. Impact at a glance.",
        "industry_tags": ["education_consulting", "fintech_saas_tech", "general_other"],
        "prompt_fragment": (
            "Clean 2×2 grid of key statistics, each in its own cell. Each cell: large bold number in brand accent colour, label below in smaller text ('clients served', "
            "'years experience', 'projects delivered', 'countries'). Thin divider lines between cells. Background: white or light with a subtle brand-coloured header bar "
            "showing the company name. No photography — the numbers tell the story. The grid format communicates scale and track record. Annual report energy. Trust through "
            "transparency."
        ),
    },

    "svc_event_speaker": {
        "name": "Event / Speaking",
        "description": "Conference energy. Stage presence. Thought leader moment.",
        "industry_tags": ["education_consulting", "events_entertainment", "general_other"],
        "prompt_fragment": (
            "Event announcement or speaking engagement graphic. Large event name in bold typography. Date, time, venue as clean secondary text. Speaker's headshot in a "
            "circle or rounded rectangle, positioned prominently. Event theme or talk title in a coloured banner. Background: dark (stage-like) with subtle spotlight or "
            "gradient effect. Brand colours used for accents. The post says 'I'm speaking at this event' or 'join us for this session.' Conference poster meets LinkedIn "
            "announcement aesthetic."
        ),
    },

    "svc_newsletter_teaser": {
        "name": "Newsletter Teaser",
        "description": "Content preview card. Drive traffic off-platform.",
        "industry_tags": ["education_consulting", "fintech_saas_tech", "general_other"],
        "prompt_fragment": (
            "Newsletter or blog teaser card with a clean editorial design. Bold article headline in dark text on a light background. 1–2 line preview excerpt in grey. "
            "A coloured 'READ MORE →' button or link in brand accent colour. Optional: a small article thumbnail image or abstract illustration. Clean layout with generous "
            "whitespace. Subtle brand logo at the top. The card format says 'this is valuable content worth clicking.' Substack/Beehiiv newsletter aesthetic."
        ),
    },

    "svc_checklist_graphic": {
        "name": "Checklist Graphic",
        "description": "Actionable list. Checkboxes. Save-worthy content.",
        "industry_tags": ["education_consulting", "fitness_gym", "general_other"],
        "prompt_fragment": (
            "Clean checklist-format graphic with 5–7 items, each with a checkbox or checkmark icon. Items in clear sans-serif text with generous spacing between lines. "
            "Title at top in bold: '7 Signs You Need [Service]' or 'Your Q2 Marketing Checklist.' Background: white or very light colour with a brand-coloured header bar. "
            "The checkboxes use brand accent colour when filled. Designed to be saved and screenshotted — high save-to-reach ratio content. Practical, actionable, reference-worthy."
        ),
    },

    "svc_before_after_text": {
        "name": "Before/After (Text-Based)",
        "description": "What they said before vs. after working with you. No photos needed.",
        "industry_tags": ["education_consulting", "fitness_gym", "beauty_wellness", "general_other"],
        "prompt_fragment": (
            "Split-screen text comparison. Left side: red/muted tint, labelled 'BEFORE' with a frustrated client quote or problematic state ('Our marketing had no strategy. "
            "We were posting randomly.'). Right side: green/vibrant tint, labelled 'AFTER' with the transformed state ('3x engagement. Consistent pipeline. Clear ROI.'). "
            "Clean sans-serif typography. Subtle icons (sad face/happy face, down arrow/up arrow) reinforcing the contrast. No photography needed. The text IS the transformation proof."
        ),
    },

    "svc_question_hook": {
        "name": "Question Hook",
        "description": "Provocative question that stops the scroll. Engagement bait done right.",
        "industry_tags": ["education_consulting", "fitness_gym", "general_other"],
        "prompt_fragment": (
            "Single provocative question as the entire visual. Large bold text centered on a solid or gradient background using brand colours. The question uses 6–12 words "
            "maximum. Typography: oversized, commanding, impossible to ignore. Optional: a small 'Comment your answer ↓' prompt at the bottom in smaller text. The background "
            "colour should be attention-grabbing but not garish. This format exists to provoke engagement (comments, shares, saves). The question should challenge an assumption "
            "or invite opinion."
        ),
    },

    "svc_client_logo_showcase": {
        "name": "Client Logo Showcase",
        "description": "Who you've worked with. Logos speak louder than words.",
        "industry_tags": ["education_consulting", "fintech_saas_tech", "general_other"],
        "prompt_fragment": (
            "Grid or horizontal strip of client/partner logos rendered in greyscale or single-colour treatment for visual consistency. Clean background (white, light grey, or dark). "
            "'Trusted by' or 'Our Clients' as a simple header. Each logo has equal sizing and spacing. Maximum 8–12 logos visible. The greyscale treatment prevents any single brand "
            "from visually dominating. Optional: a subtle brand-coloured underline beneath the header. The post communicates 'serious businesses trust us' without saying it explicitly."
        ),
    },

    "svc_hiring_card": {
        "name": "Hiring / Team Card",
        "description": "We're growing. Join us. Clean, professional, energetic.",
        "industry_tags": ["education_consulting", "fintech_saas_tech", "general_other"],
        "prompt_fragment": (
            "Job opening or team announcement card. Bold 'WE'RE HIRING' headline in brand accent colour. Role title in large dark text below. 3–4 bullet points showing key "
            "requirements or perks. Location and type (Remote / Lagos / Full-time) in a coloured badge. 'APPLY NOW' CTA button in brand colour at the bottom. Background: clean "
            "gradient or solid colour. The energy is 'we're growing fast and we want great people.' Startup hiring aesthetic meets LinkedIn job post."
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
        # Product-based styles (Expanded 2026)
        "prod_hero_pedestal", "prod_flat_lay_curated", "prod_unboxing_reveal",
        "prod_lifestyle_in_use", "prod_colour_swatch", "prod_scale_context",
        "prod_process_bts", "prod_seasonal_collection", "prod_comparison_duo",
        "prod_360_angles", "prod_bundle_stack", "prod_customer_photo_frame", "prod_price_tag",
    ],
    "food_beverage": [
        "overhead_feast", "dark_moody_food", "bright_fresh", "street_food_energy",
        "menu_board", "rustic_warmth", "bold_loud", "vibrant_tropical", "minimalist_plating",
        # Product-based styles (Expanded 2026)
        "prod_flat_lay_curated", "prod_ingredient_exploded", "prod_process_bts",
        "prod_seasonal_collection", "prod_bundle_stack", "prod_price_tag",
    ],
    "fintech_saas_tech": [
        "corporate_gradient", "data_visual", "trust_builder", "minimal_tech",
        "bold_statement", "dark_mode_pro", "isometric_3d", "clean_startup",
        # SaaS styles (Expanded 2026)
        "saas_dashboard_hero", "saas_metric_spotlight", "saas_comparison_grid",
        "saas_blog_header", "saas_feature_card", "saas_abstract_gradient",
        "saas_code_snippet", "saas_testimonial_card", "saas_changelog_card",
        "saas_infographic_flow", "saas_dark_announcement", "saas_social_proof_wall",
        # Service-based styles
        "svc_stat_grid", "svc_newsletter_teaser", "svc_client_logo_showcase", "svc_hiring_card",
    ],
    "beauty_wellness": [
        "glow_up", "soft_pastel", "bold_glam", "clean_clinical",
        "natural_organic", "editorial_beauty", "neon_pop", "lifestyle_natural", "before_after",
        # Product-based styles (Expanded 2026)
        "prod_hero_pedestal", "prod_flat_lay_curated", "prod_unboxing_reveal",
        "prod_ingredient_exploded", "prod_lifestyle_in_use", "prod_colour_swatch",
        "prod_process_bts", "prod_seasonal_collection", "prod_comparison_duo",
        "prod_bundle_stack", "prod_customer_photo_frame",
        # Service-based styles
        "svc_before_after_text",
    ],
    "real_estate": [
        "property_showcase", "luxury_listing", "neighbourhood_life",
        "blueprint_modern", "aerial_clean", "trust_builder", "bold_statement",
        # Service-based styles (Expanded 2026)
        "svc_headshot_branded", "svc_authority_quote", "svc_stat_grid",
    ],
    "fitness_gym": [
        "energy_motion", "dark_grit", "bold_loud", "transformation",
        "clean_athletic", "neon_pop", "motivational_type", "lifestyle_natural",
        # Product-based styles (Expanded 2026)
        "prod_lifestyle_in_use", "prod_customer_photo_frame",
        # Service-based styles (Expanded 2026)
        "svc_tip_carousel", "svc_case_study_result", "svc_before_after_text",
        "svc_question_hook", "svc_checklist_graphic",
    ],
    "education_consulting": [
        "trust_builder", "clean_startup", "bold_statement", "data_visual",
        "warm_professional", "minimal_tech", "authority_editorial",
        # SaaS styles (Expanded 2026)
        "saas_metric_spotlight", "saas_blog_header", "saas_testimonial_card",
        "saas_infographic_flow", "saas_social_proof_wall",
        # Service-based styles (Expanded 2026)
        "svc_authority_quote", "svc_tip_carousel", "svc_case_study_result",
        "svc_framework_diagram", "svc_headshot_branded", "svc_stat_grid",
        "svc_event_speaker", "svc_newsletter_teaser", "svc_checklist_graphic",
        "svc_before_after_text", "svc_question_hook", "svc_client_logo_showcase", "svc_hiring_card",
    ],
    "events_entertainment": [
        "neon_pop", "bold_loud", "high_contrast_drama", "afro_glam",
        "vibrant_tropical", "street_food_energy", "festival_energy",
        # SaaS/Service styles (Expanded 2026)
        "saas_dark_announcement", "svc_event_speaker",
    ],
    "general_other": [
        "bold_loud", "clean_startup", "lifestyle_natural", "minimal_studio",
        "trust_builder", "vibrant_tropical", "warm_professional", "afro_glam",
        "bright_fresh", "corporate_gradient",
        # SaaS styles (Expanded 2026)
        "saas_dashboard_hero", "saas_metric_spotlight", "saas_comparison_grid",
        "saas_blog_header", "saas_feature_card", "saas_abstract_gradient",
        "saas_testimonial_card", "saas_infographic_flow", "saas_dark_announcement",
        "saas_social_proof_wall",
        # Product-based styles (Expanded 2026)
        "prod_hero_pedestal", "prod_flat_lay_curated", "prod_unboxing_reveal",
        "prod_ingredient_exploded", "prod_lifestyle_in_use", "prod_colour_swatch",
        "prod_scale_context", "prod_process_bts", "prod_seasonal_collection",
        "prod_comparison_duo", "prod_360_angles", "prod_bundle_stack",
        "prod_customer_photo_frame", "prod_price_tag",
        # Service-based styles (Expanded 2026)
        "svc_authority_quote", "svc_tip_carousel", "svc_case_study_result",
        "svc_framework_diagram", "svc_headshot_branded", "svc_stat_grid",
        "svc_event_speaker", "svc_newsletter_teaser", "svc_checklist_graphic",
        "svc_before_after_text", "svc_question_hook", "svc_client_logo_showcase", "svc_hiring_card",
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
        # CRITICAL FIX: Default to lifestyle_natural (no text) instead of bold_loud (massive text)
        # bold_loud has "Massive typography filling 60%+ of frame" which causes unwanted text overlays
        # lifestyle_natural has "No heavy text overlay" - better default for users without brand profiles
        slug = "lifestyle_natural" if not industry_styles else industry_styles[0]
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

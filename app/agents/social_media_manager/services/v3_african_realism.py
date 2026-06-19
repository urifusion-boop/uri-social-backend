"""
V3 African Realism Vocabulary
Based on URI-Social-Image-Generation-Master-Rulebook-V3.pdf (Pages 18-19)

Provides specific language for authentic African representation in image generation:
- Skin tone vocabulary (melanin-rich tones, avoiding generic descriptions)
- Natural and protective hairstyle language
- Culturally-appropriate styling and context
- African settings and environments
- Dignified, authentic representation (no stereotypes)

This module is ONLY activated when region contains "Nigeria" or "Africa".
"""

from typing import Dict, Any, Optional


# ========== SKIN TONE VOCABULARY ==========

SKIN_TONE_VOCABULARY = """
SKIN TONE RENDERING (for African/Nigerian subjects):
- Rich melanin tones: deep mahogany, warm bronze, golden brown, deep ebony, caramel, copper
- Avoid generic terms like "dark skin" or "black skin" — use specific warm undertones
- Natural skin texture visible: pores, natural sheen, variation across face and body
- Lighting must flatter melanin-rich skin: warm key light (3200K-3800K), avoid cool lighting that desaturates
- Highlights create natural glow, not ashy appearance
- No oversaturation or unnatural color shifts
- Celebrate the full spectrum of African skin tones with accuracy and dignity
"""


# ========== HAIR VOCABULARY ==========

HAIR_VOCABULARY = """
HAIR RENDERING (for African/Nigerian subjects):
- Natural hair textures: coily (4A-4C), kinky, afro texture with volume and definition
- Protective styles: box braids, cornrows, Senegalese twists, Ghana braids, faux locs, passion twists
- Natural styles: afro (pick-out or rounded), twist-out, bantu knots, wash-and-go coils
- Locs/dreadlocks: well-maintained with visible texture and natural sheen
- Wigs and weave: Brazilian, Peruvian, or straight styles when culturally appropriate
- NO: unkempt, messy, or "wild" hair descriptions — always render with care and intention
- Hair has individual strand definition, natural sheen, and intentional styling
- Volume and texture are celebrated, not minimized
"""


# ========== FASHION & STYLING ==========

AFRICAN_FASHION_VOCABULARY = """
FASHION & STYLING (for African/Nigerian subjects):
- Traditional: Ankara prints, Kente cloth, Aso-oke, Dashiki, Buba and wrapper, Agbada
- Contemporary African fashion: modern cuts with African prints, fusion styles, Afropolitan aesthetic
- Western fashion on African subjects: rendered with same sophistication as any fashion photography
- Jewelry: traditional beads, coral beads, cowrie shells, gold accessories
- NO: tribal costumes, safari/jungle themes, or primitive styling — modern, dignified representation
- Styling should reflect contemporary African urban fashion (Lagos, Accra, Nairobi, Johannesburg)
- When in doubt: clean modern styling that respects subject's dignity
"""


# ========== SETTINGS & ENVIRONMENTS ==========

AFRICAN_SETTINGS_VOCABULARY = """
SETTINGS & ENVIRONMENTS (Nigerian/African context):
- Urban: Lagos cityscape, modern office buildings, contemporary homes, Lagos traffic, street markets
- Modern interiors: contemporary Nigerian home décor, African art on walls, modern furniture
- Business settings: professional Nigerian office environments, conference rooms, co-working spaces
- Food context: Nigerian cuisine (jollof rice, suya, plantain, egusi, pounded yam, pepper soup)
- Street context: Lagos street food vendors, keke napep, yellow danfo buses, street markets
- Celebratory: owambe party settings, aso-ebi fabrics, Nigerian weddings, cultural celebrations
- NO: jungle, safari, poverty porn, rural-only settings — balance modern urban and traditional
- Default to contemporary, aspirational settings that reflect modern African life
"""


# ========== FACIAL FEATURES & EXPRESSION ==========

FACIAL_FEATURES_VOCABULARY = """
FACIAL FEATURES (for African/Nigerian subjects):
- Natural facial structure: varied nose widths (broad, medium, narrow), full lips, diverse facial shapes
- NO: exaggerated features, caricature, or stereotypical rendering
- Expressions: confident, joyful, professional, thoughtful, aspirational — full range of human emotion
- Eye contact: direct, confident gaze when appropriate for context
- Render with photographic accuracy and individual variation
- Celebrate beauty in authentic features without Eurocentric idealization
"""


# ========== CULTURAL CONTEXT ==========

CULTURAL_CONTEXT_VOCABULARY = """
CULTURAL CONTEXT (Nigerian-specific):
- Language: English with Nigerian pidgin influence acceptable in casual contexts
- Currency: Naira (₦) symbol when showing pricing
- Locations: Lagos (VI, Lekki, Ikoyi, Surulere), Abuja, Port Harcourt, Ibadan
- Brands: Nigerian brands when appropriate (Glo, MTN, Dangote, etc. only if explicitly requested)
- Food: Nigerian dishes rendered with accuracy and appetite appeal
- Celebrations: Nigerian Independence Day, Christmas, New Year, traditional festivals
- Religion: Christian and Muslim contexts both appropriate for Nigerian audience
- NO: Generic "African" that erases Nigerian specificity
"""


# ========== DIGNITY & RESPECT PRINCIPLES ==========

DIGNITY_PRINCIPLES = """
DIGNITY & RESPECT PRINCIPLES (MANDATORY for all African representation):
1. NO poverty, suffering, or "charity case" imagery unless explicitly requested for NGO content
2. NO tribal/primitive stereotypes — modern, aspirational representation is default
3. NO safari, jungle, or "exotic" framing — African subjects in contemporary contexts
4. NO caricature, exaggeration, or stereotypical features
5. CELEBRATE: beauty, success, professionalism, joy, family, entrepreneurship, culture
6. REPRESENT: full socioeconomic spectrum — not just struggle or only luxury
7. AUTHENTIC: use Nigerian-specific context, not generic pan-African generalizations
8. DIGNITY: every subject rendered with same respect and sophistication as any global brand photography
"""


# ========== MASTER DIRECTIVE BUILDER ==========

def get_african_realism_directive(industry: str, brand_context: Dict[str, Any]) -> str:
    """
    Build complete African realism directive for prompt injection.

    This is activated ONLY when region contains "Nigeria" or "Africa".
    Returns comprehensive cultural context and representation guidelines.
    """
    region = brand_context.get("region", "")

    # Only activate if Nigerian/African market
    if not ("nigeria" in region.lower() or "africa" in region.lower()):
        return ""

    # Build comprehensive directive
    directive_parts = [
        "=== AFRICAN REALISM & CULTURAL AUTHENTICITY ===",
        "This image is for a Nigerian/African audience. Representation must be authentic, dignified, and culturally accurate.",
        "",
        SKIN_TONE_VOCABULARY,
        "",
        HAIR_VOCABULARY,
        "",
        AFRICAN_FASHION_VOCABULARY,
        "",
        AFRICAN_SETTINGS_VOCABULARY,
        "",
        FACIAL_FEATURES_VOCABULARY,
        "",
        CULTURAL_CONTEXT_VOCABULARY,
        "",
        DIGNITY_PRINCIPLES,
    ]

    # Add industry-specific context
    if industry == "food_beverage":
        directive_parts.append("""
NIGERIAN FOOD PHOTOGRAPHY SPECIFICS:
- Jollof rice: vibrant orange-red color, visible tomato base, garnished with vegetables
- Suya: skewered grilled meat with visible spice rub (yaji), served with onions and tomatoes
- Plantain: fried golden-yellow, recognizable curved shape
- Pounded yam: smooth white mound with visible sheen
- Pepper soup: reddish broth with visible spices and meat/fish
- Render with same appetite appeal as any global cuisine photography
""")

    if industry == "fashion_ecommerce":
        directive_parts.append("""
NIGERIAN FASHION PHOTOGRAPHY SPECIFICS:
- Ankara/African prints: bold geometric or floral patterns in vibrant colors
- Modern styling: African prints in contemporary cuts (pencil dresses, blazers, jumpsuits)
- Aso-ebi: matching fabric for events, often in luxe fabrics (lace, silk, brocade)
- Urban streetwear: Nigerian youth fashion mixing global brands with local style
- Professional attire: same sophistication as any global business fashion photography
""")

    if industry == "beauty_wellness":
        directive_parts.append("""
NIGERIAN BEAUTY PHOTOGRAPHY SPECIFICS:
- Makeup for melanin skin: foundation matching rich brown tones, not ashy or oxidized
- Gele (head wrap): traditional headwear styled with precision and volume
- Natural beauty: celebrate unprocessed hair, natural skin texture, authentic features
- Beauty standards: diverse representation across skin tones, features, and styles
- NO: Eurocentric beauty ideals imposed on African features
""")

    return "\n".join(directive_parts)


def get_quick_african_context(context_type: str = "general") -> str:
    """
    Get concise African context snippets for specific use cases.

    Args:
        context_type: "skin", "hair", "setting", "fashion", "dignity"

    Returns:
        Short directive string for that specific aspect
    """
    snippets = {
        "skin": "Rich melanin tones with warm lighting (3200K-3800K). Natural skin texture visible. No ashy or desaturated appearance.",
        "hair": "Natural or protective hairstyles (afro, braids, twists, locs) with volume and definition. Individual strand detail.",
        "setting": "Contemporary Nigerian urban setting (Lagos cityscape, modern interiors, professional environments). Aspirational, not poverty imagery.",
        "fashion": "Modern African fashion (Ankara prints in contemporary cuts) or global fashion styled with cultural sensitivity.",
        "dignity": "Authentic, dignified representation. No stereotypes, no caricature, no primitive framing. Same sophistication as global brand photography.",
    }

    return snippets.get(context_type, snippets["dignity"])

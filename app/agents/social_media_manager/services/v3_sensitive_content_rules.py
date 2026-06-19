"""
V3 Sensitive Content Protection Rules
Based on URI-Social-Image-Generation-Master-Rulebook-V3.pdf (Page 14)

100+ hard exclusion rules across 8 categories to prevent hallucinations and brand contamination:
1. Brand Contamination (competing brands, logos, trademarks)
2. Celebrity Likenesses (recognizable public figures)
3. Seasonal Hijacking (holiday-specific brand associations)
4. Product Category Conflicts (real-world product packaging)
5. Cultural Misappropriation (stereotypes, caricatures)
6. Inappropriate Content (violence, explicit imagery)
7. Copyrighted Material (characters, franchises, IP)
8. Technical Artifacts (watermarks, stock photo markers)
"""

from typing import Dict, List, Any
import re


# ========== CATEGORY 1: BRAND CONTAMINATION ==========

COMPETING_BRANDS = {
    "food_beverage": [
        "Coca-Cola", "Pepsi", "Sprite", "Fanta", "7UP", "Mountain Dew",
        "Nestlé", "Cadbury", "Hershey's", "Mars", "KitKat",
        "McDonald's", "KFC", "Burger King", "Subway", "Domino's",
        "Indomie", "Golden Penny", "Dangote", "Honeywell", "Peak Milk",
        "Dano", "Cowbell", "Three Crowns", "Milo", "Bournvita",
        "Nescafé", "Lipton", "Starbucks", "Red Bull", "Monster Energy",
    ],
    "fashion_ecommerce": [
        "Nike", "Adidas", "Puma", "Reebok", "Under Armour",
        "Zara", "H&M", "Forever 21", "Gap", "Uniqlo",
        "Gucci", "Louis Vuitton", "Chanel", "Prada", "Versace",
        "Supreme", "Off-White", "Balenciaga", "Yeezy",
    ],
    "beauty_wellness": [
        "L'Oréal", "Maybelline", "MAC", "Clinique", "Estée Lauder",
        "Dove", "Nivea", "Vaseline", "Johnson & Johnson",
        "Olay", "Garnier", "Neutrogena", "Aveeno",
    ],
    "fitness_gym": [
        "Nike", "Adidas", "Under Armour", "Lululemon", "Gymshark",
        "Gold's Gym", "Planet Fitness", "CrossFit", "Peloton",
    ],
    "fintech_saas_tech": [
        "Apple", "Microsoft", "Google", "Amazon", "Meta", "Facebook",
        "PayPal", "Stripe", "Square", "Visa", "Mastercard",
        "Flutterwave", "Paystack", "Interswitch", "Opay", "PalmPay",
    ],
}

# Generic brand markers to exclude
BRAND_MARKERS = [
    "® symbol", "™ symbol", "©", "brand logo", "company logo",
    "registered trademark", "trademarked name", "brand name visible",
    "corporate branding", "franchise branding", "chain store branding",
]


# ========== CATEGORY 2: CELEBRITY LIKENESSES ==========

CELEBRITY_EXCLUSIONS = [
    "No recognizable celebrity faces",
    "No public figures or politicians",
    "No athletes with identifiable features",
    "No influencers or social media personalities",
    "No historical figures (unless explicitly requested and public domain)",
    "No entertainment industry celebrities (actors, musicians, TV personalities)",
    "Generic human models only - no likeness to real people",
]


# ========== CATEGORY 3: SEASONAL HIJACKING ==========

def get_seasonal_exclusions(seed_content: str) -> List[str]:
    """
    Return seasonal exclusions based on content context.
    Prevents holiday-specific brand associations (e.g., Coca-Cola at Christmas).
    """
    seed_lower = seed_content.lower()
    exclusions = []

    if any(word in seed_lower for word in ["christmas", "xmas", "holiday", "santa", "festive"]):
        exclusions.extend([
            "No Coca-Cola branding or red-and-white Santa imagery",
            "No branded soft drinks or beverages with recognizable packaging",
            "No Christmas tree ornaments with brand logos",
            "No gift boxes with recognizable luxury brand patterns (LV, Gucci, etc.)",
            "No branded holiday packaging (Ferrero Rocher, Quality Street, etc.)",
        ])

    if "valentine" in seed_lower or "love" in seed_lower:
        exclusions.extend([
            "No Cadbury or Hershey's chocolate packaging",
            "No recognizable chocolate brand wrappers",
            "No heart-shaped boxes with brand logos",
            "No branded flower bouquets (Hallmark, FTD, etc.)",
        ])

    if any(word in seed_lower for word in ["mother", "mom", "mum", "mama"]):
        exclusions.extend([
            "No milk brands (Peak Milk, Dano, Cowbell, Three Crowns, Lactogen, etc.)",
            "No baby formula or dairy product packaging",
            "No cooking oil brands (Devon Kings, Turkey, Mamador, etc.)",
            "No food product packaging with recognizable labels",
        ])

    if "father" in seed_lower or "dad" in seed_lower:
        exclusions.extend([
            "No beer brands (Guinness, Star, Trophy, Heineken, etc.)",
            "No alcoholic beverage packaging",
            "No automotive brand logos (Toyota, Honda, Mercedes, etc.)",
        ])

    if "back to school" in seed_lower or "education" in seed_lower:
        exclusions.extend([
            "No branded notebooks (Olympia, Premier, etc.)",
            "No branded stationery with recognizable logos",
            "No backpack brands (JanSport, Herschel, etc.)",
        ])

    return exclusions


# ========== CATEGORY 4: PRODUCT CATEGORY CONFLICTS ==========

PRODUCT_PACKAGING_EXCLUSIONS = {
    "food_beverage": [
        "No recognizable food packaging (milk cartons, cereal boxes, soda bottles, snack bags)",
        "No branded soft drink bottles or cans",
        "No fast food restaurant packaging (burger boxes, fries containers)",
        "No branded condiment bottles (ketchup, mustard, mayo with logos)",
        "No recognizable candy or chocolate wrappers",
        "No branded beverage bottles (water, juice, energy drinks)",
        "No instant noodle packets with brand names",
        "No cooking ingredient packages with visible brand labels",
    ],
    "beauty_wellness": [
        "No recognizable cosmetic packaging or product containers",
        "No skincare brand bottles or jars with logos",
        "No perfume bottles with designer branding",
        "No drugstore product packaging (vitamins, supplements)",
        "No branded beauty tools (hair dryers, straighteners)",
    ],
    "fashion_ecommerce": [
        "No shoe boxes with brand names",
        "No clothing tags with designer logos",
        "No branded shopping bags",
        "No recognizable sportswear with visible swoosh, stripes, or brand marks",
    ],
    "general_other": [
        "No real-world product packaging of any kind",
        "No barcodes or UPC codes on products",
        "No price tags with store branding",
        "No branded shopping carts or baskets",
    ],
}


# ========== CATEGORY 5: CULTURAL MISAPPROPRIATION ==========

CULTURAL_SENSITIVITY_RULES = [
    "No stereotypical representations of any culture or ethnicity",
    "No caricatures or exaggerated ethnic features",
    "No traditional cultural dress used as costume or prop (unless brand is from that culture)",
    "No religious symbols used decoratively without cultural context",
    "No appropriation of indigenous patterns or designs without cultural authority",
    "If depicting African subjects: authentic representation with dignity, no stereotypes",
    "If depicting Asian subjects: avoid orientalist tropes, authentic modern representation",
    "If depicting Middle Eastern subjects: avoid stereotype, show contemporary diversity",
    "No tribal or indigenous costumes on non-indigenous models",
]


# ========== CATEGORY 6: INAPPROPRIATE CONTENT ==========

CONTENT_SAFETY_RULES = [
    "No violence, weapons, or aggressive imagery",
    "No explicit nudity or sexually suggestive content",
    "No drugs, drug paraphernalia, or substance use imagery",
    "No excessive alcohol consumption or party drug culture",
    "No gambling imagery (cards, dice, casino elements)",
    "No political symbols, flags, or partisan imagery",
    "No controversial religious imagery",
    "No self-harm or body modification imagery",
    "No dangerous activities without safety context",
]


# ========== CATEGORY 7: COPYRIGHTED MATERIAL ==========

COPYRIGHT_EXCLUSIONS = [
    "No Disney characters or any animated franchise characters",
    "No Marvel or DC superhero imagery",
    "No Star Wars, Harry Potter, or major franchise IP",
    "No video game characters (Mario, Sonic, Fortnite, etc.)",
    "No anime or manga characters (Naruto, Dragon Ball, etc.)",
    "No cartoon characters (SpongeBob, Simpsons, Family Guy, etc.)",
    "No movie posters or recognizable film imagery",
    "No TV show characters or sets",
    "No sports team logos or uniforms",
    "No university or school logos",
    "No copyrighted artwork or famous paintings",
    "No recognizable architectural landmarks (Eiffel Tower, Statue of Liberty) unless generic cityscape",
]


# ========== CATEGORY 8: TECHNICAL ARTIFACTS ==========

TECHNICAL_EXCLUSIONS = [
    "No stock photography watermarks (Shutterstock, Getty Images, etc.)",
    "No 'Sample' or 'Preview' text overlays",
    "No photographer credits or attribution text",
    "No camera metadata or EXIF data visible",
    "No image editing software UI elements",
    "No Lorem Ipsum placeholder text",
    "No FPO (For Position Only) markers",
    "No grid lines or ruler markings",
    "No color calibration charts or test patterns",
]


# ========== MASTER EXCLUSION BUILDER ==========

def get_exclusion_rules(seed_content: str, brand_context: Dict[str, Any]) -> str:
    """
    Build comprehensive exclusion rules based on content context and brand industry.

    Returns formatted string ready for injection into prompt.
    """
    industry = brand_context.get("industry", "general_other")
    rules = []

    # Always include universal rules
    rules.extend(COPYRIGHT_EXCLUSIONS)
    rules.extend(TECHNICAL_EXCLUSIONS)
    rules.extend(CONTENT_SAFETY_RULES)
    rules.extend(CULTURAL_SENSITIVITY_RULES)
    rules.extend(CELEBRITY_EXCLUSIONS)

    # Add industry-specific brand exclusions
    if industry in COMPETING_BRANDS:
        brand_list = COMPETING_BRANDS[industry]
        rules.append(f"No competing brands: {', '.join(brand_list[:20])}, or any other recognizable brands")

    # Add industry-specific product packaging exclusions
    if industry in PRODUCT_PACKAGING_EXCLUSIONS:
        rules.extend(PRODUCT_PACKAGING_EXCLUSIONS[industry])

    # Add seasonal context exclusions
    seasonal_rules = get_seasonal_exclusions(seed_content)
    if seasonal_rules:
        rules.extend(seasonal_rules)

    # Add brand markers
    rules.extend(BRAND_MARKERS)

    # Format as bulleted list
    formatted_rules = "\n".join(f"- {rule}" for rule in rules)

    return formatted_rules


def validate_prompt_for_violations(prompt: str) -> Dict[str, Any]:
    """
    Scan prompt for potential sensitive content violations before generation.

    Returns:
        {
            "passed": bool,
            "violations": List[str],  # List of detected issues
            "severity": "low" | "medium" | "high"
        }
    """
    violations = []
    prompt_lower = prompt.lower()

    # Check for brand mentions
    all_brands = []
    for brand_list in COMPETING_BRANDS.values():
        all_brands.extend([b.lower() for b in brand_list])

    for brand in all_brands:
        if brand in prompt_lower:
            violations.append(f"Competing brand mentioned: {brand}")

    # Check for celebrity indicators
    celebrity_keywords = ["celebrity", "famous", "star", "actor", "musician", "athlete"]
    if any(kw in prompt_lower for kw in celebrity_keywords):
        violations.append("Potential celebrity reference detected")

    # Check for copyrighted material keywords
    copyright_keywords = ["disney", "marvel", "star wars", "harry potter", "pokemon", "anime"]
    if any(kw in prompt_lower for kw in copyright_keywords):
        violations.append("Copyrighted material reference detected")

    # Determine severity
    if len(violations) == 0:
        severity = "none"
    elif len(violations) <= 2:
        severity = "low"
    elif len(violations) <= 5:
        severity = "medium"
    else:
        severity = "high"

    return {
        "passed": len(violations) == 0,
        "violations": violations,
        "severity": severity,
    }

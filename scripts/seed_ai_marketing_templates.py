# scripts/seed_ai_marketing_templates.py

"""
Seed default AI marketing image templates into the database.

Usage:
    python scripts/seed_ai_marketing_templates.py
"""

import asyncio
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.database import connect_to_mongo
from app.core.config import settings
from app.models.ai_prompt_template import PromptTemplate, PromptSection


async def seed_templates(db):
    """Seed default AI marketing image templates"""

    print("🌱 Seeding AI Marketing Image Templates...")
    print(f"   Database: {db.name}")

    templates = [
        # Template 1: Modern Doodle Collage
        {
            "template_id": "modern-doodle-collage",
            "name": "Modern Doodle Collage",
            "description": "Editorial fashion poster with playful doodle graphics. Perfect for Gen Z brands and creative products.",
            "category": "editorial",
            "default_aspect_ratio": "4:5",
            "default_size": "1024x1792",
            "variables": ["PRODUCT", "SERVICE", "BRAND"],
            "sections": [
                PromptSection(
                    name="FORMAT",
                    content=[
                        "Square or 4:5 social media poster",
                        "Minimalist editorial composition"
                    ]
                ),
                PromptSection(
                    name="STYLE",
                    content=[
                        "Modern Gen Z fashion-editorial branding",
                        "Black-and-white photography combined with playful doodle graphics",
                        "Clean anti-boring startup aesthetic",
                        "Pinterest-worthy magazine-style composition",
                        "Minimal but expressive",
                        "Human-centered visual storytelling",
                        "Premium streetwear-meets-editorial energy"
                    ]
                ),
                PromptSection(
                    name="COMPOSITION",
                    content=[
                        "Single realistic human model positioned centrally",
                        "Model seated or posed confidently in a minimalist studio environment",
                        "Large negative space surrounding the subject",
                        "Handwritten doodles, symbols, arrows, scribbles, lightning bolts, swirls, and small graphic elements floating around the model",
                        "Motivational or product-related typography placed asymmetrically around the composition",
                        "Text should feel integrated into the subject's energy and body language"
                    ]
                ),
                PromptSection(
                    name="MODEL",
                    content=[
                        "Use a realistic young African creative professional",
                        "Natural skin texture",
                        "Authentic facial expression",
                        "Relaxed but confident pose",
                        "Fashion-editorial styling",
                        "No overly polished AI skin",
                        "Subtle attitude and individuality"
                    ]
                ),
                PromptSection(
                    name="TYPOGRAPHY",
                    content=[
                        "Mix of handwritten marker-style typography and bold sans-serif text",
                        "Dynamic curved or angled text placement",
                        "One highlighted keyword in orange, yellow, or neon accent color",
                        "Words should feel spontaneous and expressive",
                        "Magazine-collage layout style",
                        "Include text: [PRODUCT] [BRAND]"
                    ]
                ),
                PromptSection(
                    name="COLOR PALETTE",
                    content=[
                        "Mostly monochrome",
                        "Black, white, gray",
                        "Small orange/yellow accent colors",
                        "Minimal overall palette"
                    ]
                ),
                PromptSection(
                    name="MOOD",
                    content=[
                        "Confident, Creative, Youthful, Independent, Expressive",
                        "Modern digital-culture energy"
                    ]
                ),
                PromptSection(
                    name="QUALITY",
                    content=[
                        "Shot like a fashion campaign for Nike, Acne Studios, or Zara",
                        "Behance-level editorial design",
                        "Premium typography balance",
                        "Modern magazine art direction"
                    ]
                )
            ]
        },

        # Template 2: Minimalist Editorial
        {
            "template_id": "minimalist-editorial",
            "name": "Minimalist Editorial",
            "description": "Bold typography-driven advertising with dynamic human interaction. High-fashion editorial energy.",
            "category": "editorial",
            "default_aspect_ratio": "1:1",
            "default_size": "1024x1024",
            "variables": ["PRODUCT", "SERVICE"],
            "sections": [
                PromptSection(
                    name="STYLE",
                    content=[
                        "Minimalist startup campaign aesthetic",
                        "High-fashion editorial energy",
                        "Clean white seamless studio background",
                        "Bold typography-driven layout",
                        "Dynamic asymmetrical composition",
                        "Behance-level anti-boring advertising design",
                        "Youthful modern commercial art direction"
                    ]
                ),
                PromptSection(
                    name="COMPOSITION",
                    content=[
                        "Use strong negative space",
                        "One realistic human model interacting dynamically with the frame",
                        "The model should appear in motion or exaggerated perspective",
                        "Pose should visually reinforce the headline message",
                        "Large oversized typography positioned on one side of the composition",
                        "Text stacked vertically with aggressive spacing",
                        "Model partially overlapping typography or entering the frame dramatically"
                    ]
                ),
                PromptSection(
                    name="TYPOGRAPHY",
                    content=[
                        "Huge bold condensed sans-serif typography",
                        "Black primary text",
                        "One highlighted keyword in bright neon color",
                        "Minimal text hierarchy",
                        "Tight line spacing",
                        "Editorial magazine-style text composition",
                        "Include text: [PRODUCT]"
                    ]
                ),
                PromptSection(
                    name="MOOD",
                    content=[
                        "Energetic, Bold, Youthful, Modern, Disruptive",
                        "Fast-paced startup energy"
                    ]
                )
            ]
        },

        # Template 3: Drink Splash
        {
            "template_id": "drink-splash",
            "name": "Drink Splash",
            "description": "Premium FMCG beverage photography with dramatic liquid splashes. Perfect for drinks and beverages.",
            "category": "beverage",
            "default_aspect_ratio": "4:5",
            "default_size": "1024x1792",
            "variables": ["PRODUCT"],
            "sections": [
                PromptSection(
                    name="STYLE",
                    content=[
                        "Ultra-premium FMCG advertising aesthetic",
                        "High-end Instagram luxury product campaign",
                        "Hyper-realistic studio-quality product photography",
                        "Dramatic cinematic lighting",
                        "Modern youthful branding",
                        "Vibrant but premium color grading",
                        "Behance-level composition quality"
                    ]
                ),
                PromptSection(
                    name="COMPOSITION",
                    content=[
                        "The product [PRODUCT] is positioned at the center as the dominant hero object",
                        "Dynamic liquid splash explosion behind and around the product",
                        "Floating ingredients/elements orbiting naturally around the scene",
                        "Foreground depth elements partially framing the composition",
                        "Strong depth layering from foreground, midground, and background"
                    ]
                ),
                PromptSection(
                    name="BACKGROUND",
                    content=[
                        "Dark cinematic environment with subtle glow effects",
                        "Purple-black gradient atmosphere with floating particles",
                        "Soft bokeh lights and mist",
                        "Luxury nightclub-meets-commercial-studio mood"
                    ]
                ),
                PromptSection(
                    name="VISUAL EFFECTS",
                    content=[
                        "Explosive juice/liquid splash frozen in motion",
                        "Individual droplets sharply visible",
                        "Floating fruits and ingredients suspended mid-air",
                        "Tiny atmospheric particles and sparkles",
                        "Subtle glow accents around important elements"
                    ]
                ),
                PromptSection(
                    name="COLOR PALETTE",
                    content=[
                        "Deep purple background",
                        "Neon pink accents",
                        "Orange-to-purple gradients",
                        "Fresh fruit saturation",
                        "Bright glossy highlights"
                    ]
                ),
                PromptSection(
                    name="MOOD",
                    content=[
                        "Fresh, Energetic, Youthful, Healthy, Premium",
                        "Addictive visual appeal",
                        "Luxury social-media-ready advertising"
                    ]
                )
            ]
        },

        # Template 4: Food Trust Builder
        {
            "template_id": "food-trust-builder",
            "name": "Food Trust Builder",
            "description": "Authentic African FMCG food advertising with warm family moments. Perfect for Nigerian food brands.",
            "category": "food",
            "default_aspect_ratio": "9:16",
            "default_size": "1024x1792",
            "variables": ["PRODUCT_NAME"],
            "sections": [
                PromptSection(
                    name="STYLE",
                    content=[
                        "Luxury Nigerian food brand campaign",
                        "Premium FMCG commercial photography",
                        "Modern African lifestyle advertising",
                        "Warm emotional storytelling",
                        "High-end supermarket-ready branding aesthetic",
                        "Behance-level product marketing composition"
                    ]
                ),
                PromptSection(
                    name="COMPOSITION",
                    content=[
                        "The product packaging [PRODUCT_NAME] is positioned prominently in the foreground as the hero object",
                        "A warm realistic African family or couple enjoying the food in the background",
                        "Traditional Nigerian cultural elements subtly integrated",
                        "Food plated beautifully beside the product",
                        "Strong depth separation between foreground product and lifestyle background",
                        "Balanced typography hierarchy with large headline at the top"
                    ]
                ),
                PromptSection(
                    name="MODELS",
                    content=[
                        "Use realistic Nigerian models",
                        "Authentic dark skin tones",
                        "Natural facial expressions",
                        "Warm laughter and emotional connection",
                        "Human skin texture preserved",
                        "No artificial AI smoothness",
                        "Lifestyle realism"
                    ]
                ),
                PromptSection(
                    name="COLOR PALETTE",
                    content=[
                        "Natural greens",
                        "Warm earth tones",
                        "White typography",
                        "Organic beige/brown textures",
                        "Cinematic warm highlights"
                    ]
                ),
                PromptSection(
                    name="MOOD",
                    content=[
                        "Trustworthy, Natural, Healthy, Family-oriented",
                        "Premium Nigerian authenticity",
                        "Comforting and emotionally warm"
                    ]
                )
            ]
        },

        # Template 5: Hyperpop Perspective
        {
            "template_id": "hyperpop-perspective",
            "name": "Hyperpop Perspective Advertising",
            "description": "Y2K-inspired ecommerce with exaggerated forced perspective. Perfect for Gen Z product launches.",
            "category": "product",
            "default_aspect_ratio": "4:5",
            "default_size": "1024x1792",
            "variables": ["PRODUCT_NAME"],
            "sections": [
                PromptSection(
                    name="STYLE",
                    content=[
                        "Modern Gen Z ecommerce advertising",
                        "Hyperpop fashion-commercial aesthetic",
                        "Y2K-inspired startup campaign design",
                        "Clean minimalist studio composition",
                        "High-energy perspective distortion",
                        "Bold playful commercial art direction"
                    ]
                ),
                PromptSection(
                    name="COMPOSITION",
                    content=[
                        "The product [PRODUCT_NAME] is held extremely close to the camera using exaggerated forced perspective, making it appear oversized and dominant in the frame",
                        "A stylish realistic young model positioned behind the product in a dynamic crouching or bent pose",
                        "The model should interact naturally with the product while maintaining strong fashion-editorial energy",
                        "Minimal seamless studio background with lots of negative space",
                        "Clean asymmetrical layout"
                    ]
                ),
                PromptSection(
                    name="MODEL",
                    content=[
                        "Use a realistic young African fashion-forward model",
                        "Authentic skin texture",
                        "Natural imperfections preserved",
                        "Trendy Gen Z styling",
                        "Confident expressive pose",
                        "No artificial AI smoothness"
                    ]
                ),
                PromptSection(
                    name="COLOR PALETTE",
                    content=[
                        "White background",
                        "One dominant bold color theme",
                        "Black typography",
                        "Small neon accent colors",
                        "High contrast"
                    ]
                ),
                PromptSection(
                    name="MOOD",
                    content=[
                        "Playful, Bold, Trendy, Youthful, Confident",
                        "Internet-native commercial energy"
                    ]
                )
            ]
        }
    ]

    # Insert or update templates
    from datetime import datetime
    collection = db["ai_prompt_templates"]

    for template_data in templates:
        template_id = template_data["template_id"]

        # Convert PromptSection objects to dicts
        sections_data = [{"name": s.name, "content": s.content} for s in template_data["sections"]]

        # Prepare document for MongoDB
        doc = {
            "template_id": template_data["template_id"],
            "name": template_data["name"],
            "description": template_data["description"],
            "category": template_data["category"],
            "default_aspect_ratio": template_data["default_aspect_ratio"],
            "default_size": template_data["default_size"],
            "variables": template_data["variables"],
            "sections": sections_data,
            "example_images": [],
            "is_active": True,
            "is_premium": False,
            "usage_count": 0,
            "workspace_id": None,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }

        # Check if template already exists
        existing = await collection.find_one({"template_id": template_id})

        if existing:
            print(f"   ↻ Updating template: {template_data['name']}")
            # Update existing template
            await collection.update_one(
                {"template_id": template_id},
                {"$set": {**doc, "updated_at": datetime.utcnow()}}
            )
        else:
            print(f"   ✨ Creating template: {template_data['name']}")
            # Create new template
            await collection.insert_one(doc)

    print("\n✅ AI Marketing Image Templates seeded successfully!")
    print(f"   Total templates: {len(templates)}")


async def main():
    """Main execution"""
    from datetime import datetime
    import app.database as db_module

    # Connect to MongoDB
    connect_to_mongo(settings.MONGODB_DB)

    # Get database
    db = db_module.client[settings.MONGODB_DB]

    try:
        await seed_templates(db)
    except Exception as e:
        print(f"\n❌ Error seeding templates: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

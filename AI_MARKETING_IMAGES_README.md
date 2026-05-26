# AI Marketing Image Generation System

## Overview

The AI Marketing Image Generation system allows users to create professional marketing images using structured prompt templates. This feature integrates seamlessly with the existing URI Social platform and reuses the existing DALL-E integration.

## Features

- **5 Professional Templates**: Modern Doodle Collage, Minimalist Editorial, Drink Splash, Food Trust Builder, Hyperpop Perspective
- **Variable Substitution**: Customize templates with product names, brand names, etc.
- **Multiple Aspect Ratios**: 1:1 (Square), 4:5 (Instagram), 9:16 (Story), 16:9 (Landscape)
- **Generation History**: Track all generated images with metadata
- **Cost Tracking**: Monitor API costs per generation
- **Workspace Support**: Templates can be global or workspace-specific

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                  URI Social Backend                      │
├─────────────────────────────────────────────────────────┤
│                                                           │
│  New Components:                                          │
│  ┌──────────────────────────────────────────┐           │
│  │ AIMarketingImageService                   │           │
│  │ - Template management                     │           │
│  │ - Prompt generation                       │           │
│  │ - Image generation coordination           │           │
│  └──────────────────────────────────────────┘           │
│                   │                                       │
│                   ▼                                       │
│  ┌──────────────────────────────────────────┐           │
│  │ Existing ImageContentService              │           │
│  │ - DALL-E API integration (reused)         │           │
│  │ - Image upload to Cloudinary              │           │
│  └──────────────────────────────────────────┘           │
│                                                           │
│  Models:                                                  │
│  - PromptTemplate: Template storage                       │
│  - AIImageGeneration: Generation history                  │
│                                                           │
│  Router:                                                  │
│  - /social-media/ai-marketing-images/*                    │
│                                                           │
└─────────────────────────────────────────────────────────┘
```

## Database Models

### PromptTemplate

Stores structured prompt templates:

```python
{
    "template_id": "modern-doodle-collage",
    "name": "Modern Doodle Collage",
    "description": "Editorial fashion poster with playful doodle graphics",
    "category": "editorial",
    "default_aspect_ratio": "4:5",
    "default_size": "1024x1792",
    "variables": ["PRODUCT", "BRAND"],
    "sections": [
        {
            "name": "STYLE",
            "content": ["Modern Gen Z fashion-editorial branding", ...]
        }
    ],
    "is_active": true,
    "is_premium": false,
    "usage_count": 0
}
```

### AIImageGeneration

Tracks generated images:

```python
{
    "user_id": "user123",
    "workspace_id": "workspace456",
    "template_id": "modern-doodle-collage",
    "template_name": "Modern Doodle Collage",
    "prompt": "Complete generated prompt...",
    "variables": {"PRODUCT": "URISocial SDK"},
    "image_url": "https://cloudinary.com/...",
    "status": "completed",
    "provider": "dall-e-3",
    "cost_usd": 0.08,
    "created_at": "2024-05-26T10:30:00Z"
}
```

## API Endpoints

### List Templates

```http
GET /social-media/ai-marketing-images/templates?category=editorial
```

**Response:**
```json
[
    {
        "id": "66...",
        "template_id": "modern-doodle-collage",
        "name": "Modern Doodle Collage",
        "description": "Editorial fashion poster...",
        "category": "editorial",
        "default_aspect_ratio": "4:5",
        "variables": ["PRODUCT", "BRAND"],
        "is_premium": false,
        "usage_count": 42
    }
]
```

### Get Template Details

```http
GET /social-media/ai-marketing-images/templates/modern-doodle-collage
```

**Response:**
```json
{
    "id": "66...",
    "template_id": "modern-doodle-collage",
    "name": "Modern Doodle Collage",
    "sections": [
        {
            "name": "STYLE",
            "content": ["Modern Gen Z fashion-editorial branding", ...]
        }
    ],
    "variables": ["PRODUCT", "BRAND"],
    ...
}
```

### Generate Image

```http
POST /social-media/ai-marketing-images/generate
Content-Type: application/json

{
    "template_id": "modern-doodle-collage",
    "variables": {
        "PRODUCT": "URISocial SDK",
        "BRAND": "URISocial"
    },
    "aspect_ratio": "4:5"
}
```

**Response:**
```json
{
    "success": true,
    "generation_id": "66...",
    "image_url": "data:image/webp;base64,...",
    "prompt": "Complete generated prompt...",
    "template_name": "Modern Doodle Collage",
    "size": "1024x1792",
    "aspect_ratio": "4:5"
}
```

### List Generations

```http
GET /social-media/ai-marketing-images/generations?limit=20
```

**Response:**
```json
{
    "generations": [
        {
            "id": "66...",
            "template_id": "modern-doodle-collage",
            "template_name": "Modern Doodle Collage",
            "image_url": "https://...",
            "status": "completed",
            "variables": {"PRODUCT": "URISocial SDK"},
            "created_at": "2024-05-26T10:30:00Z"
        }
    ],
    "count": 1
}
```

### Get Statistics

```http
GET /social-media/ai-marketing-images/stats
```

**Response:**
```json
{
    "total_generations": 25,
    "successful_generations": 23,
    "total_cost_usd": 1.84
}
```

### List Categories

```http
GET /social-media/ai-marketing-images/categories
```

**Response:**
```json
{
    "categories": [
        {"category": "editorial", "template_count": 2},
        {"category": "beverage", "template_count": 1},
        {"category": "food", "template_count": 1},
        {"category": "product", "template_count": 1}
    ]
}
```

## Setup Instructions

### 1. Install Dependencies

Dependencies are already included in `requirements.txt`:
- `openai` - DALL-E API
- `Pillow` - Image processing
- `beanie` - MongoDB ODM

### 2. Seed Templates

Run the seeding script to populate default templates:

```bash
python scripts/seed_ai_marketing_templates.py
```

### 3. Verify Installation

Check that the router is registered in `app/main.py`:

```python
from app.routers.ai_marketing_image_router import router as ai_marketing_image_router

app.include_router(
    ai_marketing_image_router,
    prefix="/social-media",
    tags=["AI Marketing Images"],
)
```

### 4. Test Endpoints

```bash
# List templates
curl http://localhost:8080/social-media/ai-marketing-images/templates

# Generate image (requires authentication)
curl -X POST http://localhost:8080/social-media/ai-marketing-images/generate \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "template_id": "modern-doodle-collage",
    "variables": {
      "PRODUCT": "Test Product",
      "BRAND": "Test Brand"
    }
  }'
```

## Integration with Existing Code

### Reuses ImageContentService

The new system integrates seamlessly by calling the existing `ImageContentService._call_dalle_api()` method:

```python
# In AIMarketingImageService.generate_image()
result = await ImageContentService._call_dalle_api(
    prompt=prompt,
    size=size,
    image_model="dall-e-3"
)
```

This ensures:
- ✅ Consistent DALL-E integration
- ✅ Same error handling
- ✅ Same image processing pipeline
- ✅ No duplicate code

### Uses Existing Authentication

The router uses the existing `get_current_workspace_context` dependency:

```python
@router.post("/generate")
async def generate_image(
    request: GenerateImageRequest,
    current_context: dict = Depends(get_current_workspace_context)
):
    user_id = current_context.get("user_id")
    workspace_id = current_context.get("workspace_id")
    ...
```

## Cost Information

### DALL-E 3 Pricing

- **1024x1024 (Square)**: $0.04 per image
- **1024x1792 (Portrait)**: $0.08 per image
- **1792x1024 (Landscape)**: $0.08 per image

All costs are tracked in the `AIImageGeneration.cost_usd` field.

## Template Categories

1. **editorial** - Fashion and lifestyle advertising
2. **beverage** - Drink product photography
3. **food** - Food product and lifestyle
4. **product** - General product advertising

## Creating Custom Templates

### Via Database

```python
from app.models.ai_prompt_template import PromptTemplate, PromptSection

template = PromptTemplate(
    template_id="custom-template",
    name="My Custom Template",
    description="Description here",
    category="product",
    default_aspect_ratio="1:1",
    default_size="1024x1024",
    variables=["PRODUCT_NAME", "TAGLINE"],
    sections=[
        PromptSection(
            name="STYLE",
            content=["Style guideline 1", "Style guideline 2"]
        ),
        PromptSection(
            name="COMPOSITION",
            content=["Composition rule 1", "Composition rule 2"]
        )
    ],
    is_active=True,
    workspace_id="workspace123"  # Workspace-specific
)
await template.save()
```

## Future Enhancements

### Phase 2 (Potential)
- [ ] Midjourney integration for even higher quality
- [ ] Custom template builder UI
- [ ] Batch generation
- [ ] Style transfer from reference images
- [ ] A/B testing different templates
- [ ] Premium template marketplace

### Phase 3 (Advanced)
- [ ] Video generation using same templates
- [ ] Multi-language support
- [ ] Brand kit integration (auto-apply colors/fonts)
- [ ] AI-powered template recommendations

## Testing

### Unit Tests (TODO)

```python
# tests/test_ai_marketing_images.py
async def test_build_prompt_from_template():
    template = await PromptTemplate.find_one({"template_id": "modern-doodle-collage"})
    variables = {"PRODUCT": "Test Product"}

    prompt = AIMarketingImageService.build_prompt_from_template(template, variables)

    assert "Test Product" in prompt
    assert "[PRODUCT]" not in prompt
```

### Integration Tests (TODO)

```python
async def test_generate_image_endpoint(client):
    response = await client.post(
        "/social-media/ai-marketing-images/generate",
        json={
            "template_id": "modern-doodle-collage",
            "variables": {"PRODUCT": "Test"}
        }
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
```

## Troubleshooting

### Template Not Found
```
Error: Template 'xyz' not found
Solution: Run seed script or check template_id spelling
```

### DALL-E API Error
```
Error: Generation error: RateLimitError
Solution: Check OPENAI_API_KEY in .env and API quotas
```

### Variable Not Replaced
```
Issue: [PRODUCT] still appears in generated prompt
Solution: Ensure variable name matches exactly (case-insensitive)
```

## Support

For issues or questions:
- Check existing `ImageContentService` documentation
- Review DALL-E API docs: https://platform.openai.com/docs/guides/images
- Contact: urifusion@gmail.com

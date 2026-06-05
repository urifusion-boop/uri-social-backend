# V3 Image Generation System - Testing Documentation

## 🎯 Overview

The V3 Image Generation System is an **isolated, parallel implementation** for A/B testing against the current production system (V2). It implements the 10-block prompt architecture from the **URI-Social-Image-Generation-Master-Rulebook-V3.pdf**.

**Status**: Testing Phase
**Deployment**: Isolated endpoints under `/social-media/v3/test/*`
**Impact**: Zero risk to production - completely separate codebase

---

## 📂 File Structure

```
app/agents/social_media_manager/services/
├── v3_prompt_builder.py              # 10-block prompt architecture (core)
├── v3_aesthetic_vocabulary.py        # 400+ cinematography terms, lighting presets
├── v3_style_library.py               # 11-dimensional style ontology
├── v3_sensitive_content_rules.py     # 100+ exclusion rules (8 categories)
├── v3_african_realism.py             # Authentic African representation vocabulary
├── v3_image_content_service.py       # Main V3 generation service

app/agents/social_media_manager/routers/
└── v3_test_router.py                 # A/B comparison endpoints

app/main.py                            # V3 router registered (line 154)
```

---

## 🚀 Quick Start

### 1. Test Endpoints

All V3 endpoints are under `/social-media/v3/test/`:

**A/B Comparison (Primary Testing Method)**
```bash
POST /social-media/v3/test/compare-generation
Authorization: Bearer <jwt_token>
Content-Type: application/json

{
  "seed_content": "Mother's Day sale - 30% off all skincare products",
  "platform": "instagram",
  "reference_image": null,  # Optional product image URL
  "image_model": "gpt-image-2"
}
```

**Response:**
```json
{
  "status": true,
  "responseData": {
    "comparison_id": "abc123...",
    "v2_result": {
      "draft_id": "...",
      "image_url": "https://cloudinary.../v2_image.webp",
      "prompt_used": "... (856 chars)"
    },
    "v3_result": {
      "draft_id": "...",
      "image_url": "https://cloudinary.../v3_image.webp",
      "v3_metadata": {
        "prompt": "... (1847 chars)",
        "blocks_used": ["BLOCK 1: PRODUCT CORE", "BLOCK 2: SCENE DNA", ...],
        "prompt_length": 1847
      }
    },
    "prompt_diff": {
      "v2_length": 856,
      "v3_length": 1847,
      "length_increase_pct": 115,
      "v3_new_blocks": ["BLOCK 3: ATMOSPHERIC DEPTH", "BLOCK 5: MICRO-REALISM", ...]
    }
  }
}
```

**Record User Choice (Primary Success Metric)**
```bash
POST /social-media/v3/test/record-choice

{
  "comparison_id": "abc123...",
  "chosen_version": "v3"  # or "v2"
}
```

**Get Statistics**
```bash
GET /social-media/v3/test/stats
```

**Response:**
```json
{
  "total_comparisons": 150,
  "v3_wins": 100,
  "v2_wins": 50,
  "v3_win_rate": 0.67,  # 67% chose V3
  "v2_win_rate": 0.33,
  "avg_prompt_length_v2": 856,
  "avg_prompt_length_v3": 1847,
  "recommendation": "V3 is winning!"
}
```

---

## 📊 Success Metrics

### Primary Metric: **Approval Rate**
- **V3 Wins**: v3_win_rate > 60% (users consistently choose V3 images)
- **Keep Testing**: v3_win_rate 40-60% (inconclusive, need more data)
- **V2 Wins**: v3_win_rate < 40% (V2 performs better)

### Secondary Metrics (tracked via PostHog):
1. **Edit Reduction**: Lower edit_count before approval
2. **Generation Time**: V3 prompts are longer - does it slow down?
3. **Hallucination Rate**: Fewer brand contamination/copyright violations

### PostHog Events Tracked:
- `v3_comparison_generated` - When comparison is created
- `v3_comparison_choice` - When user picks V2 or V3 (**PRIMARY METRIC**)
- `v3_generation_only` - When V3 is used standalone

---

## 🧪 Testing Phases

### Week 1: Internal Beta (Target: 50-100 comparisons)
- **Goal**: Catch bugs, validate prompt quality
- **Who**: Internal team or 5-10 power users
- **Track**: Generation failures, obvious hallucinations

### Week 2: Limited Rollout (Target: 500+ comparisons)
- **Goal**: Measure v3_win_rate and gather statistical significance
- **Who**: 10% of users OR feature-flag opt-in
- **Track**: All 4 metrics + user feedback

### Decision Point (End of Week 2):
- ✅ **Ship V3**: If v3_win_rate > 60% + no major bugs
- 🔄 **Iterate**: If promising but needs refinement
- ❌ **Keep V2**: If no measurable improvement

---

## 🔬 What's Different in V3?

### 1. **10-Block Prompt Architecture** (vs. V2's 6 sections)
- **BLOCK 1**: Product Core Definition (forensic product preservation)
- **BLOCK 2**: Scene DNA (cinematography, lighting, color science)
- **BLOCK 3**: Atmospheric Depth (3-layer depth with bokeh/haze)
- **BLOCK 4**: Motion Signature (frozen motion, dynamic energy)
- **BLOCK 5**: Micro-Realism (surface texture, material properties)
- **BLOCK 6**: Typography Hierarchy (font rules, text placement)
- **BLOCK 7**: Layout Geometry (composition, negative space)
- **BLOCK 8**: Cultural Context (African realism vocabulary)
- **BLOCK 9**: Brand Compliance (exclusion rules, brand restrictions)
- **BLOCK 10**: Quality Control (8-dimensional quality checklist)

### 2. **Expanded Aesthetic Vocabulary**
- 12 cinematography clusters (editorial portrait, food photography, action sports, etc.)
- 8 lighting presets (golden hour, dramatic side key, studio beauty, etc.)
- Product photography techniques (beverage, food, fashion, beauty, electronics, etc.)
- 6 fashion clusters (seasonal + editorial)
- 8 color science palettes

### 3. **11-Dimensional Style System** (vs. V2's flat style fragments)
Each style defined across 11 dimensions:
- Cinematography
- Lighting Physics
- Color Science
- Material Properties
- Atmospheric Depth
- Motion Signature
- Typography Hierarchy
- Layout Geometry
- Cultural Context
- Composition Mode
- Style Type

### 4. **Sensitive Content Protection** (100+ exclusion rules)
- Brand Contamination (competing brands by industry)
- Celebrity Likenesses
- Seasonal Hijacking (e.g., Coca-Cola at Christmas)
- Product Category Conflicts (real packaging)
- Cultural Misappropriation (no stereotypes)
- Inappropriate Content
- Copyrighted Material
- Technical Artifacts

### 5. **African Realism Vocabulary** (Nigeria-specific)
- Skin tone vocabulary (melanin-rich, warm lighting)
- Hair vocabulary (natural, protective styles with dignity)
- Fashion & styling (Ankara, contemporary African fashion)
- Settings (Lagos urban, modern Nigerian contexts)
- Facial features (authentic, no caricature)
- Cultural context (Nigerian specifics, not generic "African")

---

## 📈 PostHog Dashboard Queries

### V3 Win Rate (PRIMARY METRIC)
```sql
SELECT
  COUNT(*) as total_choices,
  COUNTIF(properties.chosen_version = 'v3') as v3_wins,
  COUNTIF(properties.v3_won = true) / COUNT(*) * 100 as v3_win_rate_pct
FROM events
WHERE event = 'v3_comparison_choice'
  AND timestamp >= NOW() - INTERVAL 14 DAY
```

### Average Generation Time
```sql
SELECT
  AVG(properties.v3_generation_time_ms) as avg_v3_time,
  PERCENTILE(properties.v3_generation_time_ms, 0.95) as p95_v3_time
FROM events
WHERE event = 'v3_comparison_generated'
  AND timestamp >= NOW() - INTERVAL 7 DAY
```

### Prompt Length Analysis
```sql
SELECT
  AVG(properties.v2_prompt_length) as avg_v2_length,
  AVG(properties.v3_prompt_length) as avg_v3_length,
  AVG(properties.v3_prompt_length - properties.v2_prompt_length) as avg_increase
FROM events
WHERE event = 'v3_comparison_generated'
```

---

## 🛠️ Development Notes

### Reused from V2 (Production):
- ✅ DALL-E/GPT-Image-2 API calls (`ImageContentService._call_dalle_api()`)
- ✅ Cloudinary upload logic
- ✅ Logo composition
- ✅ Background removal
- ✅ Product analysis service
- ✅ Credit system
- ✅ Database schema (`content_drafts`, `content_requests`)

### New in V3:
- ✅ `V3PromptBuilder` - 10-block assembly
- ✅ `v3_aesthetic_vocabulary` - Rich visual language
- ✅ `v3_style_library` - 11-dimensional styles
- ✅ `v3_sensitive_content_rules` - Context-aware exclusions
- ✅ `v3_african_realism` - Cultural authenticity
- ✅ `v3_test_router` - A/B comparison endpoints
- ✅ `v3_test_generations` collection - Testing database

---

## 🗄️ Database Schema

### New Collection: `v3_test_generations`
```javascript
{
  _id: ObjectId,
  comparison_id: "abc123...",  // Links V2 and V3 results
  user_id: "user_uuid",
  seed_content: "Mother's Day sale...",
  platform: "instagram",

  // V2 data
  v2_draft_id: "...",
  v2_image_url: "...",
  v2_prompt: "... (856 chars)",

  // V3 data
  v3_draft_id: "...",
  v3_image_url: "...",
  v3_prompt: "... (1847 chars)",
  v3_metadata: {
    blocks_used: [...],
    style_slug: "afro_glam",
    has_product_reference: false,
    generation_time_ms: 3420
  },

  // Comparison data
  prompt_diff: {
    v2_length: 856,
    v3_length: 1847,
    length_increase_pct: 115,
    v3_new_blocks: [...]
  },

  // User choice (PRIMARY METRIC)
  user_choice: "v3",  // or "v2" or null
  chosen_at: ISODate,

  created_at: ISODate
}
```

### Indexes:
```javascript
db.v3_test_generations.createIndex({ user_id: 1, created_at: -1 })
db.v3_test_generations.createIndex({ comparison_id: 1 })
db.v3_test_generations.createIndex({ user_choice: 1 })
```

---

## 🚨 Rollback Plan

If V3 underperforms or has issues:

1. **Immediate**: Stop routing users to `/v3/test/*` endpoints
2. **Data preserved**: All V3 test data remains in `v3_test_generations` for analysis
3. **Zero production impact**: V2 (production) system completely untouched
4. **Quick re-enable**: Fix issues, resume testing

---

## ✅ Migration Plan (If V3 Wins)

### Step 1: Deprecation Notice (1 week)
- Announce V3 graduation to production
- Document prompt improvements
- Notify users of enhanced quality

### Step 2: Code Migration
```bash
# Rename files
mv app/agents/social_media_manager/services/image_content_service.py \
   app/agents/social_media_manager/services/image_content_service_v2_legacy.py

mv app/agents/social_media_manager/services/v3_image_content_service.py \
   app/agents/social_media_manager/services/image_content_service.py

# Update imports in routers
# Remove v3_ prefix from all V3 modules
```

### Step 3: Deprecate V2
- Keep V2 code for 2 weeks as fallback
- Monitor production metrics
- Delete V2 code after grace period

---

## 📞 Support

- **Questions**: Check `/v3/test/stats` for real-time performance
- **Bugs**: File issue with comparison_id for debugging
- **Feature requests**: Suggest improvements based on test data

---

## 🎓 Key Learnings from V3 Rulebook

1. **Prompt length matters**: V3 prompts are ~2x longer but more specific
2. **Block order is critical**: GPT-Image-2 weights prompt beginning heavily
3. **African representation**: Specific vocabulary prevents stereotypes
4. **Sensitive content**: Context-aware exclusions reduce hallucinations
5. **11 dimensions > flat fragments**: Parametric styles allow mixing

---

**Built with**: FastAPI, MongoDB, OpenAI GPT-Image-2, PostHog
**Testing Period**: 2 weeks (configurable)
**Decision Criteria**: v3_win_rate > 60%
**Status**: ✅ Ready for testing

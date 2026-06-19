# V3 Frontend Integration Guide

## Overview

V3 prompt system is now integrated into production with a **frontend toggle**. Users can enable/disable V3 at any time through their settings.

---

## 🎛️ Backend Endpoints

### 1. **Enable/Disable V3 for User**

```bash
POST /social-media/v3/toggle
Authorization: Bearer <jwt_token>
Content-Type: application/json

{
  "enabled": true  # or false
}
```

**Response:**
```json
{
  "status": true,
  "responseData": {
    "use_v3_prompts": true,
    "message": "V3 prompt system enabled successfully!",
    "info": "Your next image generations will use the V3 10-block prompt system."
  }
}
```

**What it does:**
- Updates `brand_profiles.use_v3_prompts` field in database
- Tracks event in PostHog: `v3_toggle_changed`
- All future image generations for this user will use V3

---

### 2. **Check V3 Status**

```bash
GET /social-media/v3/status
Authorization: Bearer <jwt_token>
```

**Response:**
```json
{
  "status": true,
  "responseData": {
    "use_v3_prompts": true,
    "prompt_system": "V3 10-Block",  # or "V2 6-Section"
    "message": "Currently using V3 prompt system"
  }
}
```

**What it does:**
- Returns current V3 status for logged-in user
- Use this to show toggle state in settings UI

---

### 3. **A/B Comparison (Testing Only)**

```bash
POST /social-media/v3/test/compare-generation
Authorization: Bearer <jwt_token>
Content-Type: application/json

{
  "seed_content": "Mother's Day sale - 30% off",
  "platform": "instagram"
}
```

**Response:**
```json
{
  "status": true,
  "responseData": {
    "comparison_id": "abc123",
    "v2_result": {
      "image_url": "...",
      "prompt_used": "... (856 chars)"
    },
    "v3_result": {
      "image_url": "...",
      "v3_metadata": {
        "prompt": "... (1847 chars)",
        "blocks_used": ["BLOCK 1", "BLOCK 2", ...]
      }
    }
  }
}
```

**What it does:**
- Generates with BOTH V2 and V3 for side-by-side comparison
- Use for internal testing or power users

---

## 🎨 Frontend Implementation

### Option 1: Settings Toggle (Recommended)

Add a toggle switch in user settings:

```typescript
// Settings.tsx or ProfileSettings.tsx

import { useState, useEffect } from 'react';

const V3Toggle = () => {
  const [useV3, setUseV3] = useState(false);
  const [loading, setLoading] = useState(false);

  // Fetch current status on mount
  useEffect(() => {
    const fetchV3Status = async () => {
      try {
        const response = await fetch('/social-media/v3/status', {
          headers: {
            'Authorization': `Bearer ${getAuthToken()}`
          }
        });
        const data = await response.json();
        setUseV3(data.responseData.use_v3_prompts);
      } catch (error) {
        console.error('Failed to fetch V3 status:', error);
      }
    };

    fetchV3Status();
  }, []);

  // Handle toggle change
  const handleToggle = async (enabled: boolean) => {
    setLoading(true);
    try {
      const response = await fetch('/social-media/v3/toggle', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${getAuthToken()}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ enabled })
      });

      const data = await response.json();

      if (data.status) {
        setUseV3(enabled);

        // Show success message
        toast.success(
          enabled
            ? 'V3 Prompts Enabled! Next images will use enhanced prompts.'
            : 'Switched back to V2 prompts.'
        );
      } else {
        throw new Error(data.responseMessage);
      }
    } catch (error) {
      console.error('Failed to toggle V3:', error);
      toast.error('Failed to update setting. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="settings-section">
      <div className="setting-row">
        <div className="setting-info">
          <h3>Enhanced Image Prompts (V3) ✨</h3>
          <p>
            Use our new 10-block prompt system for richer, more detailed images.
            Includes better African representation, product preservation, and
            100+ safety rules.
          </p>
          {useV3 && (
            <span className="badge badge-success">
              Currently using V3
            </span>
          )}
        </div>

        <div className="setting-control">
          <label className="toggle-switch">
            <input
              type="checkbox"
              checked={useV3}
              onChange={(e) => handleToggle(e.target.checked)}
              disabled={loading}
            />
            <span className="slider"></span>
          </label>
        </div>
      </div>
    </div>
  );
};

export default V3Toggle;
```

---

### Option 2: In-App Beta Badge

Show beta badge when V3 is enabled:

```typescript
// ContentGenerator.tsx

const ContentGenerator = () => {
  const { useV3 } = useV3Status(); // Custom hook

  return (
    <div className="content-generator">
      <div className="header">
        <h2>Generate Content</h2>
        {useV3 && (
          <span className="beta-badge">
            ✨ V3 Enhanced Prompts
          </span>
        )}
      </div>

      {/* Rest of component */}
    </div>
  );
};
```

---

### Option 3: A/B Testing UI (Power Users)

For internal testing or advanced users:

```typescript
// CompareV2V3.tsx

const CompareGenerations = () => {
  const [comparison, setComparison] = useState(null);
  const [loading, setLoading] = useState(false);

  const generateComparison = async (seedContent: string) => {
    setLoading(true);
    try {
      const response = await fetch('/social-media/v3/test/compare-generation', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${getAuthToken()}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          seed_content: seedContent,
          platform: 'instagram'
        })
      });

      const data = await response.json();
      setComparison(data.responseData);
    } catch (error) {
      console.error('Comparison failed:', error);
    } finally {
      setLoading(false);
    }
  };

  const recordChoice = async (chosenVersion: 'v2' | 'v3') => {
    await fetch('/social-media/v3/test/record-choice', {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${getAuthToken()}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        comparison_id: comparison.comparison_id,
        chosen_version: chosenVersion
      })
    });
  };

  return (
    <div className="comparison-view">
      {comparison && (
        <div className="side-by-side">
          <div className="version-card">
            <h3>V2 (Current)</h3>
            <img src={comparison.v2_result.image_url} alt="V2 generation" />
            <button onClick={() => recordChoice('v2')}>
              Choose V2
            </button>
          </div>

          <div className="version-card">
            <h3>V3 (New) ✨</h3>
            <img src={comparison.v3_result.image_url} alt="V3 generation" />
            <div className="v3-metadata">
              <span>10 blocks used</span>
              <span>{comparison.v3_result.v3_metadata.prompt_length} chars</span>
            </div>
            <button onClick={() => recordChoice('v3')}>
              Choose V3
            </button>
          </div>
        </div>
      )}
    </div>
  );
};
```

---

## 🔄 How It Works (Backend Flow)

### Without Toggle (Current Production Flow)
```
User → Generate Content Endpoint
     → ImageContentService.generate_image_for_draft()
     → V2 6-section prompts
     → GPT-Image-2
     → Image returned
```

### With Toggle Enabled
```
User (V3 enabled) → Generate Content Endpoint
                 → Check brand_profile.use_v3_prompts
                 → TRUE → V3ImageContentService.generate_image_for_draft_v3()
                          → V3 10-block prompts
                          → GPT-Image-2
                          → Image returned
```

---

## 📊 PostHog Events

Track V3 usage:

```javascript
// When user toggles V3
posthog.capture('v3_toggle_changed', {
  enabled: true,
  action: 'enabled'
});

// When V3 generates an image (automatic)
posthog.capture('v3_generation_production', {
  platform: 'instagram',
  has_reference: false
});

// When user chooses V2 vs V3 in comparison
posthog.capture('v3_comparison_choice', {
  comparison_id: 'abc123',
  chosen_version: 'v3',
  v3_won: true
});
```

---

## 🎯 Recommended Rollout Strategy

### Phase 1: Internal Beta (Week 1)
1. Add toggle to settings (hidden by default)
2. Enable for internal team only
3. Track: Does V3 improve image quality? Any bugs?

### Phase 2: Opt-In Beta (Week 2)
1. Make toggle visible in settings with "Beta" badge
2. Add tooltip: "Try our enhanced prompt system"
3. Track: How many users enable it? Do they keep it on?

### Phase 3: Default for New Users (Week 3-4)
1. New signups get V3 by default
2. Existing users still opt-in
3. Track: V3 vs V2 approval rates

### Phase 4: Full Migration (Week 5-6)
1. Enable V3 for all users
2. Keep V2 available for 2 weeks as fallback
3. Deprecate V2 entirely

---

## 🛠️ Database Schema

### Brand Profile Update

```javascript
// brand_profiles collection
{
  _id: ObjectId,
  user_id: "user_uuid",
  brand_name: "My Brand",
  // ... existing fields ...

  // NEW V3 FIELDS
  use_v3_prompts: false,  // Boolean flag (default: false)
  v3_enabled_at: null,    // Timestamp when enabled (optional)
}
```

### Database Migration (Optional)

```javascript
// Add use_v3_prompts field to existing brand profiles
db.brand_profiles.updateMany(
  { use_v3_prompts: { $exists: false } },
  { $set: { use_v3_prompts: false } }
);

// Create index for V3 queries
db.brand_profiles.createIndex({ use_v3_prompts: 1 });
```

---

## 🚨 Error Handling

### If V3 Generation Fails

Backend automatically falls back to V2:

```python
try:
    # Try V3 generation
    result = await V3ImageContentService.generate_image_for_draft_v3(...)
except Exception as e:
    print(f"V3 generation failed: {e}. Falling back to V2.")
    result = await ImageContentService.generate_image_for_draft(...)
```

Frontend sees no difference - just gets the image.

---

## 📱 UI/UX Recommendations

### Settings Page

```
┌─────────────────────────────────────────┐
│ ⚙️  Image Generation Settings           │
├─────────────────────────────────────────┤
│                                         │
│ Enhanced Prompts (V3) ✨ BETA          │
│ Use our new 10-block prompt system     │
│ for richer, more detailed images.      │
│                                         │
│ ✓ Better African representation        │
│ ✓ Enhanced product preservation        │
│ ✓ 100+ safety rules                    │
│                                         │
│                          [Toggle: ON]   │
│                                         │
│ Currently using: V3 10-Block System    │
└─────────────────────────────────────────┘
```

### Tooltip Text

```
"V3 Enhanced Prompts uses a more sophisticated 10-block
architecture for better image quality. Includes expanded
aesthetic vocabulary, cultural sensitivity rules, and
improved product preservation. Try it out!"
```

### Success Messages

- **On Enable**: "✨ V3 Enabled! Your next images will use enhanced prompts."
- **On Disable**: "Switched back to standard prompts (V2)."

---

## 🧪 Testing Checklist

- [ ] Toggle API works (enable/disable)
- [ ] Status API returns correct state
- [ ] V3 generation works when enabled
- [ ] V2 generation works when disabled
- [ ] Toggle persists across sessions
- [ ] PostHog events fire correctly
- [ ] Comparison endpoint works (A/B testing)
- [ ] Error fallback works (V3 → V2)
- [ ] Mobile responsive
- [ ] Loading states work

---

## 📞 Support

### For Users
- **Where is the toggle?** Settings → Image Generation → Enhanced Prompts
- **What's the difference?** V3 uses richer prompts for better image quality
- **Can I switch back?** Yes, toggle off at any time

### For Developers
- **Backend issue?** Check PostHog for `v3_generation_production` events
- **Toggle not working?** Verify `brand_profiles.use_v3_prompts` field exists
- **V3 errors?** Check logs for `[V3 ROUTING]` and `[V3]` prefixed messages

---

## 🎓 Key Benefits of V3

1. **10-Block Architecture** vs 6-section (V2)
2. **400+ Aesthetic Terms** vs ~150 (V2)
3. **11-Dimensional Styles** vs flat fragments (V2)
4. **100+ Exclusion Rules** vs ~10 (V2)
5. **African Realism Vocabulary** (NEW - dignified representation)
6. **Product Preservation** (Enhanced - forensic analysis)

---

**Status**: ✅ Ready for frontend integration
**Backend Endpoints**: Live at `/social-media/v3/*`
**Database Field**: `brand_profiles.use_v3_prompts`
**Model Used**: GPT-Image-2 (same as V2)

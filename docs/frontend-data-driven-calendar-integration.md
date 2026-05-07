# Frontend Integration Guide — Data-Driven Content Calendar (Phase 1)

> **For:** Frontend team  
> **Backend branch:** `develop`  
> **Base URL (local):** `http://localhost:9003`  
> **Base URL (staging):** `https://api-staging.urisocial.com`  
> All endpoints require a `Bearer` JWT token in the `Authorization` header.

---

## Overview

Three new backend APIs power two existing UI pages:

| UI Page                         | New API                                             | Purpose                                       |
| ------------------------------- | --------------------------------------------------- | --------------------------------------------- |
| **Performance**                 | `GET /social-media/content-calendar/performance`    | Real engagement data per format & topic       |
| **Market Intel**                | `GET /social-media/content-calendar/trends`         | Trending industry keywords from Google Trends |
| **Content Calendar** (generate) | `POST /social-media/content-calendar/plan/generate` | 7-day plan now includes scores + explanation  |

---

## 1. Performance Page

### Current State

The page shows _"No published posts yet"_ even when the user has published content, because it relies only on the Outstand social connection data.

### What to Wire Up

Call the new performance endpoint to populate the page with real engagement intelligence.

---

### `GET /social-media/content-calendar/performance`

**Headers:**

```
Authorization: Bearer <token>
```

**Success Response `200`:**

```json
{
  "status": true,
  "responseCode": 200,
  "responseMessage": "performance successfully retrieved.",
  "responseData": {
    "avg_engagement_by_format": {
      "image": 5.2,
      "text": 2.1,
      "long_form": 3.0
    },
    "avg_engagement_by_topic": {
      "finance": 6.1,
      "education": 4.0,
      "marketing": 3.5
    },
    "best_posting_hour": 18,
    "top_formats": ["image", "long_form", "text"],
    "top_topics": ["finance", "education", "marketing"],
    "post_count": 15,
    "analytics_count": 12,
    "has_data": true
  }
}
```

**When `has_data` is `false`** (new user, no published posts):

```json
{
  "responseData": {
    "avg_engagement_by_format": {},
    "avg_engagement_by_topic": {},
    "best_posting_hour": 18,
    "top_formats": [],
    "top_topics": [],
    "post_count": 0,
    "analytics_count": 0,
    "has_data": false
  }
}
```

---

### How to Map to the Existing Performance UI

#### Top Stats Row

```
post_count          → "X posts published"
analytics_count     → "X posts with analytics"
best_posting_hour   → "Best time to post: 6pm"  (convert hour → 12h format)
```

#### "Top Formats" Chart / Bar List

Use `avg_engagement_by_format` — map format name to engagement rate:

```
image     → 5.2%  ████████████
long_form → 3.0%  ██████
text      → 2.1%  ████
```

Highlight the first item in `top_formats` as the recommended format.

#### "Top Topics" List

Use `avg_engagement_by_topic` — sorted already by engagement:

```
finance    → 6.1%  🏆
education  → 4.0%
marketing  → 3.5%
```

#### Empty State (when `has_data === false`)

Keep the existing _"No published posts yet"_ empty state but update the copy to:

> _"Publish your first post from the Workspace tab. Your performance insights will appear here once posts go live."_

---

### Example JS/TS Fetch

```ts
const getPerformance = async (token: string) => {
  const res = await fetch(
    `${BASE_URL}/social-media/content-calendar/performance`,
    {
      headers: { Authorization: `Bearer ${token}` },
    },
  );
  const body = await res.json();
  if (!body.status) return null;
  return body.responseData; // shape: PerformanceData
};

interface PerformanceData {
  avg_engagement_by_format: Record<string, number>;
  avg_engagement_by_topic: Record<string, number>;
  best_posting_hour: number;
  top_formats: string[];
  top_topics: string[];
  post_count: number;
  analytics_count: number;
  has_data: boolean;
}
```

---

## 2. Market Intel Page

### Current State

Shows _"Market intelligence will appear here once your brand profile is fully set up and social accounts are connected."_

### What to Wire Up

The trends endpoint already works independently of social connections — it only needs the user's **industry** from their brand profile (which is set up during onboarding). Wire it up now.

---

### `GET /social-media/content-calendar/trends`

**Headers:**

```
Authorization: Bearer <token>
```

**Success Response `200`:**

```json
{
  "status": true,
  "responseCode": 200,
  "responseMessage": "trends successfully retrieved.",
  "responseData": {
    "industry": "finance",
    "keywords": [
      {
        "keyword": "personal finance",
        "trend_score": 85.0,
        "growth_rate": 120.0,
        "source": "google_trends",
        "type": "rising"
      },
      {
        "keyword": "investment tips",
        "trend_score": 70.0,
        "growth_rate": 80.0,
        "source": "google_trends",
        "type": "rising"
      },
      {
        "keyword": "savings",
        "trend_score": 60.0,
        "growth_rate": 40.0,
        "source": "google_trends",
        "type": "top"
      }
    ],
    "count": 3
  }
}
```

**Keyword `type` values:**

| type     | Meaning                                               | Badge to show |
| -------- | ----------------------------------------------------- | ------------- |
| `rising` | Growing fast on Google Trends                         | 🔥 Rising     |
| `top`    | Consistently high search volume                       | 📈 Trending   |
| `seed`   | Fallback industry keyword (Google Trends unavailable) | — (no badge)  |

---

### How to Map to the Existing Market Intel UI

#### Header

```
industry  →  "Trending in Finance"  (capitalize industry name)
```

#### Keyword Cards / List

For each keyword in `keywords`:

```
keyword      → card title: "Personal Finance"
trend_score  → progress bar / score pill (0–100)
growth_rate  → "+120% on Google"  (only show if type === "rising")
type         → badge: 🔥 Rising | 📈 Trending
source       → if source === "fallback": show subtle grey pill "Industry keyword"
               if source === "google_trends": show "via Google Trends"
```

#### Empty/Loading State

Remove the _"brand profile not set up"_ gate. Show a loading spinner while fetching. Only show the empty state if the `keywords` array comes back empty.

#### Update the existing gate condition

```
OLD: show empty if (!brandProfileComplete || !socialAccountsConnected)
NEW: show empty if (keywords.length === 0)
```

---

### Example JS/TS Fetch

```ts
const getTrends = async (token: string) => {
  const res = await fetch(`${BASE_URL}/social-media/content-calendar/trends`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  const body = await res.json();
  if (!body.status) return null;
  return body.responseData; // shape: TrendsData
};

interface TrendKeyword {
  keyword: string;
  trend_score: number; // 0–100
  growth_rate: number; // percentage
  source: 'google_trends' | 'fallback';
  type: 'rising' | 'top' | 'seed';
}

interface TrendsData {
  industry: string;
  keywords: TrendKeyword[];
  count: number;
}
```

---

## 3. Content Calendar — Day Cards Now Include Scores

When the calendar is generated or fetched, each day object now contains extra intelligence fields the frontend can display.

### Existing endpoint (no change to URL):

`POST /social-media/content-calendar/plan/generate`

```json
{
  "platforms": ["instagram"],
  "force_regenerate": true
}
```

### Each day object — new fields added:

```json
{
  "day_index": 0,
  "date": "2026-05-04",
  "content_type": "educational",
  "title": "5 mistakes every entrepreneur makes with personal finance",
  "description": "...",
  "platforms": ["instagram"],
  "acted_on": false,

  "keyword": "personal finance",
  "format": "image",
  "trend_score": 85.0,
  "performance_score": 76.5,
  "format_score": 100.0,
  "final_score": 83.3,
  "reason": "\"personal finance\" is trending (+120% growth on Google) · Your finance posts perform 6.1% above average · Image posts have your best engagement (5.2%)"
}
```

Also on the plan root:

```json
{
  "generation_method": "data_driven",
  "data_signals": {
    "post_count": 15,
    "top_topics": ["finance", "education"],
    "top_formats": ["image", "long_form"]
  }
}
```

---

### Suggested UI Additions to Calendar Day Cards

#### Score Badge (top-right of card)

```
final_score >= 80  →  🟢  "Strong"
final_score 60–79  →  🟡  "Good"
final_score < 60   →  ⚪  (no badge)
```

#### Explanation Tooltip / Expand Row

Show `reason` on hover or in an expandable section:

> _"personal finance" is trending (+120% growth on Google) · Your finance posts perform 6.1% above average · Image posts have your best engagement (5.2%)_

#### Calendar Header — Generation Method Pill

```
generation_method === "data_driven"   →  "📊 Data-driven"  (green pill)
generation_method === "trend_driven"  →  "🔥 Trend-driven" (orange pill)
generation_method === "ai"            →  "✨ AI-generated"  (grey pill)
```

---

## Error Handling

All endpoints follow the same error shape:

```json
{
  "status": false,
  "responseCode": 401,
  "responseMessage": "You are Unauthorized, Please provide a valid access token"
}
```

| Status                 | Meaning                                        |
| ---------------------- | ---------------------------------------------- |
| `401` / `403`          | Token missing or expired — redirect to login   |
| `404` (on performance) | User has no published posts — show empty state |
| `500`                  | Server error — show generic retry message      |

---

## Quick Reference

| What                    | Endpoint                                       | Method |
| ----------------------- | ---------------------------------------------- | ------ |
| Performance insights    | `/social-media/content-calendar/performance`   | `GET`  |
| Industry trends         | `/social-media/content-calendar/trends`        | `GET`  |
| Generate 7-day calendar | `/social-media/content-calendar/plan/generate` | `POST` |
| Get current week's plan | `/social-media/content-calendar/plan`          | `GET`  |

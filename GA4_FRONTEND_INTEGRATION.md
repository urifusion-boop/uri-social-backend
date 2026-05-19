# PostHog Frontend Integration Guide

## Overview

PostHog is already tracking **server-side events** (signups, logins) from the backend. The frontend needs the PostHog JS snippet to capture **page views, session replays, button clicks, heatmaps, and user sessions**.

**Project API Key:** `phc_wCEFMsX7yQY4op5KkqVoYg3wGyMn56wWWghFYzBcFc4p`
**Host:** `https://us.i.posthog.com`

---

## Quickest Setup — Run the PostHog Wizard

In your frontend repo root, run:

```bash
npx -y @posthog/wizard@latest
```

This auto-detects your framework (Next.js, React, Vue, etc.) and wires everything up automatically.

---

## Manual Setup

### Install

```bash
npm install posthog-js
```

### Next.js (App Router) — `app/providers.tsx`

```tsx
'use client'
import posthog from 'posthog-js'
import { PostHogProvider } from 'posthog-js/react'
import { useEffect } from 'react'

export function PHProvider({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    posthog.init('phc_wCEFMsX7yQY4op5KkqVoYg3wGyMn56wWWghFYzBcFc4p', {
      api_host: 'https://us.i.posthog.com',
      capture_pageview: true,
      capture_pageleave: true,
      session_recording: { maskAllInputs: true },
    })
  }, [])
  return <PostHogProvider client={posthog}>{children}</PostHogProvider>
}
```

Then wrap your root layout in `app/layout.tsx`:

```tsx
import { PHProvider } from './providers'

export default function RootLayout({ children }) {
  return (
    <html>
      <body>
        <PHProvider>{children}</PHProvider>
      </body>
    </html>
  )
}
```

### React (Vite / CRA) — `main.tsx`

```tsx
import posthog from 'posthog-js'

posthog.init('phc_wCEFMsX7yQY4op5KkqVoYg3wGyMn56wWWghFYzBcFc4p', {
  api_host: 'https://us.i.posthog.com',
  capture_pageview: true,
  session_recording: { maskAllInputs: true },
})
```

---

## Identify Users After Login

After a successful login/signup API call, link the PostHog anonymous session to the real user:

```ts
import posthog from 'posthog-js'

// Call this right after your login/signup API response
posthog.identify(userId, {
  email: userEmail,
  name: `${firstName} ${lastName}`,
})
```

---

## Track Custom Events (Optional)

```ts
import posthog from 'posthog-js'

// Examples
posthog.capture('clicked upgrade', { plan: 'pro', source: 'pricing_page' })
posthog.capture('content generated', { type: 'linkedin_post' })
posthog.capture('onboarding step completed', { step: 2 })
```

---

## What the Backend Already Tracks (Do Not Duplicate)

| Event | Trigger |
|-------|---------|
| `user signed up` | New account created (email or Google) |
| `user logged in` | User logs in (email or Google) |

---

## Verify It's Working

Go to **PostHog → Activity** — events appear in real time within seconds of firing.

Session replays appear under **PostHog → Session replay** after users visit the site with the snippet installed.

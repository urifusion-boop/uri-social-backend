# LinkedIn Integration Guide — Frontend

**Staging:** `https://api-staging.urisocial.com`
**All authenticated endpoints require** `Authorization: Bearer <jwt>`

---

## Flow

```
1. POST /linkedin/connect        → get auth_url, open in popup
2. User authorises on LinkedIn   → backend saves tokens + admin pages
3. GET  /linkedin/pages          → list personal profile + company pages
4. POST /linkedin/pages/select   → pick posting target (optional, defaults to personal)
5. POST /linkedin/publish        → publish to selected target
6. DELETE /linkedin/connect      → disconnect
```

---

## Endpoints

### `POST /linkedin/connect`

Returns a LinkedIn OAuth URL. Open it in a popup or redirect.

**Response:**

```json
{
  "responseData": {
    "auth_url": "https://www.linkedin.com/oauth/v2/authorization?..."
  }
}
```

---

### `GET /linkedin/callback` — Backend only

LinkedIn redirects here automatically. Backend saves tokens and redirects to:

```
<frontend>/social-media/brand-setup?connected=true&platform=linkedin&username=<name>
# or on failure:
?connected=false&platform=linkedin&error=access_denied
```

---

### `GET /linkedin/status`

**Response (connected):**

```json
{
  "responseData": {
    "linked": true,
    "username": "user@example.com",
    "account_name": "Jane Doe",
    "connected_at": "2026-03-31T12:00:00Z",
    "active_author_urn": "urn:li:person:abc123",
    "pages": [
      {
        "id": "98765",
        "name": "Uri Social",
        "urn": "urn:li:organization:98765"
      }
    ]
  }
}
```

`pages` is empty if the user admins no LinkedIn company pages.

---

### `GET /linkedin/pages`

Returns personal profile + admin pages + current active posting target.

**Response:**

```json
{
  "responseData": {
    "personal_profile": {
      "urn": "urn:li:person:abc123",
      "name": "Jane Doe",
      "type": "personal"
    },
    "pages": [
      {
        "id": "98765",
        "name": "Uri Social",
        "urn": "urn:li:organization:98765"
      }
    ],
    "active_author_urn": "urn:li:person:abc123"
  }
}
```

---

### `POST /linkedin/pages/select`

Switch the active posting target. Must be the `personal_profile.urn` or one of the `pages[].urn` values.

**Request:**

```json
{ "author_urn": "urn:li:organization:98765" }
```

**Response:**

```json
{
  "responseMessage": "Active posting target updated.",
  "responseData": { "active_author_urn": "urn:li:organization:98765" }
}
```

**Errors:** `400` invalid URN or no connection · `401` bad JWT

---

### `POST /linkedin/publish`

Publishes to the currently selected target (person or company page).

**Request:**

```json
{ "content": "Your post text here (max 3000 chars, 1300 recommended)" }
```

**Response:**

```json
{ "responseData": { "post_id": "urn:li:share:7123456789", "content": "..." } }
```

**Errors:** `400` not connected · `401` bad JWT · `502` LinkedIn API error (usually expired token — prompt reconnect)

---

### `DELETE /linkedin/connect`

Removes the LinkedIn connection.

**Response:** `{ "responseMessage": "LinkedIn account disconnected." }`
**Error:** `400` not connected

---

## React/TypeScript Example

```tsx
import { useState, useEffect } from 'react';

const BASE_URL = 'https://api-staging.urisocial.com';

interface Page {
  id: string;
  name: string;
  urn: string;
}
interface PagesData {
  personal_profile: { urn: string; name: string; type: string };
  pages: Page[];
  active_author_urn: string;
}

export function LinkedInConnect({ jwt }: { jwt: string }) {
  const [linked, setLinked] = useState(false);
  const [accountName, setAccountName] = useState('');
  const [pagesData, setPagesData] = useState<PagesData | null>(null);
  const [loading, setLoading] = useState(false);
  const [postContent, setPostContent] = useState('');
  const [message, setMessage] = useState('');

  const headers = { Authorization: `Bearer ${jwt}` };

  useEffect(() => {
    fetchStatus();
    const params = new URLSearchParams(window.location.search);
    if (params.get('platform') === 'linkedin') {
      setMessage(
        params.get('connected') === 'true'
          ? `✅ Connected as ${params.get('username')}`
          : `❌ Failed: ${params.get('error')}`,
      );
      window.history.replaceState({}, '', window.location.pathname);
    }
  }, []);

  const fetchStatus = async () => {
    const res = await fetch(`${BASE_URL}/linkedin/status`, { headers });
    const data = await res.json();
    setLinked(data.responseData?.linked ?? false);
    setAccountName(data.responseData?.account_name ?? '');
    if (data.responseData?.linked) fetchPages();
  };

  const fetchPages = async () => {
    const res = await fetch(`${BASE_URL}/linkedin/pages`, { headers });
    const data = await res.json();
    setPagesData(data.responseData);
  };

  const handleConnect = async () => {
    setLoading(true);
    const res = await fetch(`${BASE_URL}/linkedin/connect`, {
      method: 'POST',
      headers,
    });
    const data = await res.json();
    const popup = window.open(
      data.responseData.auth_url,
      'linkedin-oauth',
      'width=600,height=700',
    );
    const timer = setInterval(() => {
      if (popup?.closed) {
        clearInterval(timer);
        fetchStatus();
      }
    }, 800);
    setLoading(false);
  };

  const handleSelectTarget = async (urn: string) => {
    await fetch(`${BASE_URL}/linkedin/pages/select`, {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ author_urn: urn }),
    });
    fetchPages();
  };

  const handleDisconnect = async () => {
    await fetch(`${BASE_URL}/linkedin/connect`, { method: 'DELETE', headers });
    setLinked(false);
    setPagesData(null);
    setMessage('LinkedIn disconnected.');
  };

  const handlePublish = async () => {
    setLoading(true);
    const res = await fetch(`${BASE_URL}/linkedin/publish`, {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: postContent }),
    });
    const data = await res.json();
    setMessage(
      data.status
        ? `✅ Published! ${data.responseData.post_id}`
        : `❌ ${data.detail}`,
    );
    if (data.status) setPostContent('');
    setLoading(false);
  };

  const targetOptions = pagesData
    ? [
        {
          label: `${pagesData.personal_profile.name} (Personal)`,
          value: pagesData.personal_profile.urn,
        },
        ...pagesData.pages.map((p) => ({ label: p.name, value: p.urn })),
      ]
    : [];

  return (
    <div>
      <h2>LinkedIn</h2>
      {message && <p>{message}</p>}
      {linked ? (
        <>
          <p>
            Connected as <strong>{accountName}</strong>
          </p>

          {targetOptions.length > 1 && (
            <select
              value={pagesData?.active_author_urn}
              onChange={(e) => handleSelectTarget(e.target.value)}
            >
              {targetOptions.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
          )}

          <textarea
            value={postContent}
            onChange={(e) => setPostContent(e.target.value)}
            placeholder="What do you want to post?"
            rows={4}
            maxLength={3000}
          />
          <button
            onClick={handlePublish}
            disabled={loading || !postContent.trim()}
          >
            {loading ? 'Posting…' : 'Publish'}
          </button>
          <button onClick={handleDisconnect}>Disconnect</button>
        </>
      ) : (
        <button onClick={handleConnect} disabled={loading}>
          {loading ? 'Loading…' : 'Connect LinkedIn'}
        </button>
      )}
    </div>
  );
}
```

---

## Notes

- **Default target** — personal profile. Call `POST /linkedin/pages/select` to switch to a company page.
- **Empty pages list** — user needs to reconnect LinkedIn if they recently became a page admin (so backend re-fetches with updated scopes).
- **Token expiry** — LinkedIn tokens last ~60 days. On `502`, prompt the user to reconnect.
- **Popup blocked** — fall back to `window.location.href = auth_url`.
- **Scopes** — `openid profile email w_member_social w_organization_social r_organization_social`.

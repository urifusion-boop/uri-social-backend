"""
Tests: Core Product — Content Creation
────────────────────────────────────────
✅ Content can be generated for a platform
✅ Generation requires auth
✅ Generation requires valid platform
✅ Generated content has expected fields
"""

import pytest


CONTENT_PAYLOAD = {
    "seed_content": "Uri Social helps Nigerian entrepreneurs grow their business using AI-powered social media tools.",
    "platforms": ["linkedin"],
    "seed_type": "text",
    "include_images": False,
}


class TestContentGeneration:
    def test_generate_requires_auth(self, client):
        """Content generation requires authentication."""
        r = client.post("/social-media/generate-content", json=CONTENT_PAYLOAD)
        assert r.status_code in (401, 403), f"Expected auth error, got {r.status_code}"

    def test_generate_content_success(self, client, auth_headers):
        """Authenticated user can generate content."""
        r = client.post(
            "/social-media/generate-content",
            json=CONTENT_PAYLOAD,
            headers=auth_headers,
            timeout=60,
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] is True

    def test_generated_content_has_drafts(self, client, auth_headers):
        """Generated content response contains drafts."""
        r = client.post(
            "/social-media/generate-content",
            json=CONTENT_PAYLOAD,
            headers=auth_headers,
            timeout=60,
        )
        assert r.status_code == 200, r.text
        resp_data = r.json().get("responseData", {})
        drafts = resp_data.get("drafts") or resp_data.get("content") or []
        assert len(drafts) > 0, f"No drafts returned: {r.json()}"

    def test_generate_invalid_platform(self, client, auth_headers):
        """Invalid platform is rejected with an error response."""
        r = client.post(
            "/social-media/generate-content",
            json={**CONTENT_PAYLOAD, "platforms": ["invalidplatform"]},
            headers=auth_headers,
            timeout=30,
        )
        if r.status_code == 200:
            assert r.json().get("status") is False, f"Expected error status, got: {r.text}"
        else:
            assert r.status_code in (400, 422), f"Expected validation error, got {r.status_code}: {r.text}"

    def test_generate_empty_seed_rejected(self, client, auth_headers):
        """Empty seed content is rejected."""
        r = client.post(
            "/social-media/generate-content",
            json={**CONTENT_PAYLOAD, "seed_content": ""},
            headers=auth_headers,
        )
        assert r.status_code == 422, f"Expected 422, got {r.status_code}"


class TestDrafts:
    def test_list_scheduled_requires_auth(self, client):
        """Scheduled posts listing requires authentication."""
        r = client.get("/social-media/scheduled")
        assert r.status_code in (401, 403)

    def test_list_scheduled(self, client, auth_headers):
        """Authenticated user can list scheduled posts."""
        r = client.get("/social-media/scheduled", headers=auth_headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] is True
        assert isinstance(data["responseData"], (list, dict))

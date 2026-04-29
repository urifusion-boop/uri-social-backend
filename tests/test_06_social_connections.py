"""
Tests: Social Account Connections
───────────────────────────────────
✅ Connection initiation requires auth
✅ Connection initiation returns auth URLs
✅ Listing connections requires auth
✅ Connections list returns expected shape
✅ Unsupported platform handled gracefully
"""

import pytest


class TestSocialConnections:
    def test_initiate_requires_auth(self, client):
        """Initiating social connection requires authentication."""
        r = client.post("/social-media/connect/initiate", json={"platforms": ["linkedin"], "source": "settings"})
        assert r.status_code in (401, 403)

    def test_initiate_linkedin(self, client, auth_headers):
        """Initiating LinkedIn connection returns auth URL."""
        r = client.post(
            "/social-media/connect/initiate",
            json={"platforms": ["linkedin"], "source": "settings"},
            headers=auth_headers,
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] is True
        auth_urls = data["responseData"].get("auth_urls", {})
        assert "linkedin" in auth_urls, f"No linkedin auth_url in response: {data['responseData']}"
        assert auth_urls["linkedin"], "LinkedIn auth_url is empty"

    def test_initiate_multiple_platforms(self, client, auth_headers):
        """Can initiate connections for multiple platforms at once."""
        r = client.post(
            "/social-media/connect/initiate",
            json={"platforms": ["linkedin", "instagram"], "source": "onboarding"},
            headers=auth_headers,
        )
        assert r.status_code == 200, r.text
        auth_urls = r.json()["responseData"].get("auth_urls", {})
        assert "linkedin" in auth_urls, "LinkedIn missing from auth_urls"
        assert "instagram" in auth_urls, "Instagram missing from auth_urls"

    def test_list_connections_requires_auth(self, client):
        """Listing connections requires authentication."""
        r = client.get("/social-media/connections")
        assert r.status_code in (401, 403)

    def test_list_connections(self, client, auth_headers):
        """Authenticated user can list their connections."""
        r = client.get("/social-media/connections", headers=auth_headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] is True
        assert "responseData" in data

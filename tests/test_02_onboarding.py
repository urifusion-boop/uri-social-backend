"""
Tests: Onboarding Flow
───────────────────────
✅ Brand profile can be saved
✅ Brand profile can be retrieved
✅ Onboarding marked complete
✅ Unauthenticated access rejected
"""

import pytest


BRAND_PAYLOAD = {
    "brand_name": "QA Test Brand",
    "industry": "Technology",
    "brand_voice": "professional",
    "target_audience": "Nigerian entrepreneurs",
    "business_description": "AI-powered social media management for Nigerian businesses.",
    "tagline": "Grow smarter.",
    "key_products_services": ["Social media management", "Content generation"],
    "website": "https://qatestbrand.com",
}


class TestBrandProfile:
    def test_save_brand_profile_requires_auth(self, client):
        """Unauthenticated brand profile save returns 403."""
        r = client.post("/social-media/brand-profile", json=BRAND_PAYLOAD)
        assert r.status_code in (401, 403), f"Expected auth error, got {r.status_code}"

    def test_save_brand_profile(self, client, auth_headers):
        """Authenticated user can save brand profile."""
        r = client.post(
            "/social-media/brand-profile",
            json=BRAND_PAYLOAD,
            headers=auth_headers,
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] is True

    def test_get_brand_profile(self, client, auth_headers):
        """Saved brand profile can be retrieved."""
        r = client.get("/social-media/brand-profile", headers=auth_headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] is True
        profile = data["responseData"]
        assert profile.get("brand_name") == BRAND_PAYLOAD["brand_name"]

    def test_onboarding_complete_flag(self, client, auth_headers):
        """Brand profile response includes onboarding_completed field."""
        r = client.get("/social-media/brand-profile", headers=auth_headers)
        assert r.status_code == 200
        profile = r.json()["responseData"]
        assert "onboarding_completed" in profile, "onboarding_completed field missing from brand profile"

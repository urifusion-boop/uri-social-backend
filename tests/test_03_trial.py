"""
Tests: Free Trial
──────────────────
✅ Trial status returns correct fields
✅ Trial is active after signup
✅ Trial credits are available
✅ Unauthenticated access rejected
"""

import pytest


class TestFreeTrial:
    def test_trial_status_requires_auth(self, client):
        """Unauthenticated trial status returns 403."""
        r = client.get("/social-media/billing/trial/status")
        assert r.status_code in (401, 403), f"Expected auth error, got {r.status_code}"

    def test_trial_status_active(self, client, auth_headers):
        """New user's trial is active."""
        r = client.get("/social-media/billing/trial/status", headers=auth_headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] is True
        trial = data["responseData"]
        assert trial.get("trial_active") is True, f"Trial not active: {trial}"

    def test_trial_has_credits(self, client, auth_headers):
        """Trial user has credits available."""
        r = client.get("/social-media/billing/trial/status", headers=auth_headers)
        assert r.status_code == 200, r.text
        trial = r.json()["responseData"]
        assert trial.get("trial_credits", 0) > 0, "Trial user has no trial credits"

    def test_trial_status_fields(self, client, auth_headers):
        """Trial status response contains all expected fields."""
        r = client.get("/social-media/billing/trial/status", headers=auth_headers)
        assert r.status_code == 200
        trial = r.json()["responseData"]
        for field in ("trial_active", "days_remaining", "credits_remaining"):
            assert field in trial, f"Missing field in trial status: {field}"

    def test_trial_days_remaining_positive(self, client, auth_headers):
        """New trial user has positive days remaining."""
        r = client.get("/social-media/billing/trial/status", headers=auth_headers)
        assert r.status_code == 200
        trial = r.json()["responseData"]
        assert trial.get("days_remaining", 0) > 0, "Days remaining should be positive"

"""
Tests: Billing & Credits
─────────────────────────
✅ Credit balance requires auth
✅ Balance returns correct shape
✅ Subscription tiers are accessible
✅ Payment history requires auth
✅ Transaction history requires auth
"""

import pytest


class TestCredits:
    def test_balance_requires_auth(self, client):
        r = client.get("/social-media/billing/credits/balance")
        assert r.status_code in (401, 403)

    def test_balance_returns_data(self, client, auth_headers):
        r = client.get("/social-media/billing/credits/balance", headers=auth_headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert "credits_remaining" in data, f"Missing credits_remaining: {data}"
        assert isinstance(data["credits_remaining"], (int, float))

    def test_transaction_history_requires_auth(self, client):
        r = client.get("/social-media/billing/credits/transactions")
        assert r.status_code in (401, 403)

    def test_transaction_history(self, client, auth_headers):
        r = client.get("/social-media/billing/credits/transactions", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] is True


class TestSubscription:
    def test_tiers_publicly_accessible(self, client):
        """Subscription tiers are public — no auth needed."""
        r = client.get("/social-media/billing/subscription/tiers")
        assert r.status_code == 200, r.text
        tiers = r.json()
        assert isinstance(tiers, list)
        assert len(tiers) > 0, "No subscription tiers returned"

    def test_current_subscription_requires_auth(self, client):
        r = client.get("/social-media/billing/subscription/current")
        assert r.status_code in (401, 403)

    def test_current_subscription(self, client, auth_headers):
        r = client.get("/social-media/billing/subscription/current", headers=auth_headers)
        # 200 for paid users, 404 for trial-only users with no active subscription
        assert r.status_code in (200, 404), r.text

    def test_payment_history_requires_auth(self, client):
        r = client.get("/social-media/billing/payments/history")
        assert r.status_code in (401, 403)

    def test_payment_history(self, client, auth_headers):
        r = client.get("/social-media/billing/payments/history", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] is True

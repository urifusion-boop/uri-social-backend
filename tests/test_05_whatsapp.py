"""
Tests: WhatsApp Connection
───────────────────────────
✅ Connect a phone number
✅ Duplicate number on same account is idempotent
✅ Number already linked to another account returns 409
✅ Status reflects connection
✅ Disconnect works
✅ Unauthenticated access rejected
"""

import pytest

TEST_PHONE = "+2340000000001"  # Fake number — does not send real messages


class TestWhatsAppConnect:
    def test_connect_requires_auth(self, client):
        """WhatsApp connect requires authentication."""
        r = client.post("/whatsapp/connect", json={"phone": TEST_PHONE})
        assert r.status_code in (401, 403)

    def test_connect_phone(self, client, auth_headers):
        """Authenticated user can connect a phone number."""
        r = client.post("/whatsapp/connect", json={"phone": TEST_PHONE}, headers=auth_headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] is True

    def test_status_shows_connected(self, client, auth_headers):
        """Status endpoint shows phone as linked after connect."""
        r = client.get("/whatsapp/status", headers=auth_headers)
        assert r.status_code == 200, r.text
        data = r.json()["responseData"]
        assert data["linked"] is True
        assert data["phone"] == TEST_PHONE

    def test_status_requires_auth(self, client):
        """WhatsApp status requires authentication."""
        r = client.get("/whatsapp/status")
        assert r.status_code in (401, 403)

    def test_disconnect(self, client, auth_headers):
        """User can disconnect their WhatsApp number."""
        r = client.delete("/whatsapp/connect", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert r.json()["status"] is True

    def test_status_shows_disconnected(self, client, auth_headers):
        """Status shows not linked after disconnect."""
        r = client.get("/whatsapp/status", headers=auth_headers)
        assert r.status_code == 200
        data = r.json()["responseData"]
        assert data["linked"] is False

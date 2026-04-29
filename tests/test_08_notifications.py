"""
Tests: Notifications
─────────────────────
✅ Unread count requires auth
✅ Unread count returns a number
✅ Notification list requires auth
✅ Notification list returns expected shape
"""

import pytest


class TestNotifications:
    def test_unread_count_requires_auth(self, client):
        r = client.get("/social-media/notifications/unread-count")
        assert r.status_code in (401, 403)

    def test_unread_count(self, client, auth_headers):
        r = client.get("/social-media/notifications/unread-count", headers=auth_headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] is True
        assert "unread_count" in data["responseData"]
        assert isinstance(data["responseData"]["unread_count"], int)

    def test_list_notifications_requires_auth(self, client):
        r = client.get("/social-media/notifications/")
        assert r.status_code in (401, 403)

    def test_list_notifications(self, client, auth_headers):
        r = client.get("/social-media/notifications/", headers=auth_headers)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["status"] is True
        assert "responseData" in data
